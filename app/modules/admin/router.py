"""Admin router — internal user management and submission review endpoints.

All routes require X-Admin-Secret header matching settings.admin_secret.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.r2_client import presign_get, upload_bytes
from app.core.supabase_client import get_service_client
from app.modules.billing.plans import Plan, get_spec
from app.modules.billing.service import assign_plan
from app.modules.home import service as home_service
from app.modules.home.schemas import HomeContent
from app.modules.profile import service as profile_service

router = APIRouter(prefix="/admin", tags=["admin"])

_PLAN_NAMES: dict[str, str] = {
    "free": "Free",
    "single-song": "Single Song",
    "starter": "Starter",
    "single-artist": "Single Artist",
    "double-artist": "Double Artist",
    "label": "Label",
    # legacy underscore variants (kept for backwards compat)
    "single_song": "Single Song",
    "single_artist": "Single Artist",
    "double_artist": "Double Artist",
}

_PLAN_PRICES_INR: dict[str, int] = {
    "single-song": 269,
    "starter": 899,
    "single-artist": 1439,
    "double-artist": 2699,
    "label": 6300,
    "single_song": 269,
    "single_artist": 1439,
    "double_artist": 2699,
}


def _require_admin(x_admin_secret: Annotated[str, Header()] = "") -> None:
    if not settings.admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _fmt(val: Any) -> str | None:
    if val is None:
        return None
    return val.isoformat() if hasattr(val, "isoformat") else str(val)


def _fetch_all_users(svc: Any) -> list:
    """Paginate through auth.admin.list_users() to get every user."""
    users: list = []
    page = 1
    per_page = 1000
    while True:
        batch = svc.auth.admin.list_users(page=page, per_page=per_page)
        if not batch:
            break
        users.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return users


@router.get("/users", dependencies=[Depends(_require_admin)])
async def list_users(q: str = Query(default="")) -> dict:
    """Return all users with their plan and subscription status.

    Optional ?q= filters by email or full_name (case-insensitive).
    """
    try:
        svc = get_service_client()
        raw_users = _fetch_all_users(svc)
        subs_resp = (
            svc.table("subscriptions")
            .select("user_id,plan,status,expires_at,started_at")
            .execute()
        )
        profiles_resp = (
            svc.table("profiles")
            .select("user_id,spotify_url,apple_music_url,instagram,youtube_url,city,state,bio,gender,date_of_birth")
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch users: {exc}",
        ) from exc

    sub_map: dict[str, dict] = {
        row["user_id"]: row for row in (subs_resp.data or [])
    }
    profile_map: dict[str, dict] = {
        row["user_id"]: row for row in (profiles_resp.data or [])
    }

    users = []
    for u in raw_users:
        uid = str(getattr(u, "id", "") or "")
        email = getattr(u, "email", "") or ""
        app_meta: dict = getattr(u, "app_metadata", None) or {}
        user_meta: dict = getattr(u, "user_metadata", None) or {}
        full_name: str = user_meta.get("full_name", "") or ""
        artist_name: str = user_meta.get("artist_name", "") or ""
        phone: str = user_meta.get("phone", "") or ""
        sub = sub_map.get(uid, {})
        plan: str = sub.get("plan") or app_meta.get("plan", "free") or "free"
        prof = profile_map.get(uid, {})

        users.append(
            {
                "id": uid,
                "email": email,
                "full_name": full_name,
                "artist_name": artist_name,
                "phone": phone,
                "plan": plan,
                "plan_name": _PLAN_NAMES.get(plan, plan.replace("_", " ").title()),
                "status": sub.get("status", "active"),
                "expires_at": sub.get("expires_at"),
                "created_at": _fmt(getattr(u, "created_at", None)),
                "last_sign_in_at": _fmt(getattr(u, "last_sign_in_at", None)),
                "spotify_url": prof.get("spotify_url") or "",
                "apple_music_url": prof.get("apple_music_url") or "",
                "instagram": prof.get("instagram") or "",
                "youtube_url": prof.get("youtube_url") or "",
                "city": prof.get("city") or "",
                "state": prof.get("state") or "",
                "bio": prof.get("bio") or "",
                "gender": prof.get("gender") or "",
                "date_of_birth": prof.get("date_of_birth") or "",
            }
        )

    if q:
        q_lower = q.lower()
        users = [
            u for u in users
            if q_lower in u["email"].lower() or q_lower in u["full_name"].lower()
        ]

    return {"users": users, "total": len(users)}


# ---------------------------------------------------------------------------
# Submission review
# ---------------------------------------------------------------------------

_CATEGORY_TYPES: dict[str, list[str]] = {
    "songs":           ["new_song", "transfer_song"],
    "albums":          ["new_album", "transfer_album"],
    "profile-mismatch": ["profile_mismatch"],
    "claim-removal":   ["claim_removal"],
    "insta-link":      ["insta_link"],
}


class ReviewBody(BaseModel):
    status: str        # "approved" | "declined"
    admin_note: str = ""


@router.get("/submissions/{category}", dependencies=[Depends(_require_admin)])
async def list_submissions(
    category: str,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=10, ge=1, le=50),
) -> dict:
    """Paginated submissions for a category.

    Sorted: pending first (status DESC: p > d > a), then created_at DESC.
    """
    types = _CATEGORY_TYPES.get(category)
    if not types:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown category")

    svc = get_service_client()

    try:
        resp = (
            svc.table("submissions")
            .select("*")
            .in_("submission_type", types)
            .order("status", desc=True)          # pending > declined > approved
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch submissions: {exc}",
        ) from exc

    all_items = resp.data or []
    total = len(all_items)
    offset = (page - 1) * per_page
    total_pages = max(1, -(-total // per_page))
    return {
        "submissions": all_items[offset: offset + per_page],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


@router.patch("/submissions/{submission_id}", dependencies=[Depends(_require_admin)])
async def review_submission(submission_id: str, body: ReviewBody) -> dict:
    """Approve or decline a submission."""
    if body.status not in ("approved", "declined"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="status must be 'approved' or 'declined'",
        )

    svc = get_service_client()
    try:
        resp = (
            svc.table("submissions")
            .update(
                {
                    "status": body.status,
                    "admin_note": body.admin_note,
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", submission_id)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not update submission: {exc}",
        ) from exc

    if not resp.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    submission = resp.data[0]

    # If approving a new-artist submission, add to the new-artist queue.
    if body.status == "approved":
        data: dict = submission.get("data") or {}
        if str(data.get("new_artist", "")).lower() == "true":
            main_artists = data.get("main_artists") or []
            if not main_artists:
                songs = data.get("songs") or []
                if songs:
                    main_artists = songs[0].get("main_artists") or []
            artist_name = (main_artists[0].get("name", "") if main_artists else "") or ""
            try:
                svc.table("new_artist_queue").insert({
                    "user_email": submission.get("user_email", ""),
                    "artist_name": artist_name,
                    "submission_id": submission_id,
                }).execute()
            except Exception:
                pass  # best-effort — don't block the approval

    return submission


# ---------------------------------------------------------------------------
# New-artist queue
# ---------------------------------------------------------------------------


class NewArtistUpdateBody(BaseModel):
    spotify_url: str = ""
    apple_music_url: str = ""


class AdminUserUpdate(BaseModel):
    full_name: str | None = None
    artist_name: str | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    date_of_birth: str | None = None
    gender: str | None = None
    bio: str | None = None
    spotify_url: str | None = None
    apple_music_url: str | None = None
    instagram: str | None = None
    youtube_url: str | None = None
    plan: str | None = None


@router.get("/new-artist-queue", dependencies=[Depends(_require_admin)])
async def list_new_artist_queue() -> dict:
    """Return all new-artist queue entries — pending first."""
    svc = get_service_client()
    try:
        resp = (
            svc.table("new_artist_queue")
            .select("*")
            .order("status", desc=False)   # pending before updated
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        # Table may not exist yet — return empty list gracefully instead of 502
        if "PGRST205" in str(exc) or "schema cache" in str(exc).lower() or "does not exist" in str(exc).lower():
            return {"entries": [], "hint": "Run migration 0004_apple_music_and_new_artist_queue.sql in Supabase SQL Editor to enable this feature."}
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not fetch queue: {exc}")
    return {"entries": resp.data or []}


@router.patch("/new-artist-queue/{entry_id}", dependencies=[Depends(_require_admin)])
async def update_new_artist(entry_id: str, body: NewArtistUpdateBody) -> dict:
    """Save Spotify + Apple Music links for a queued new artist and update their profile."""
    svc = get_service_client()

    try:
        entry_resp = svc.table("new_artist_queue").select("*").eq("id", entry_id).limit(1).execute()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"DB error: {exc}")
    if not entry_resp.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")

    entry = entry_resp.data[0]
    user_email = entry.get("user_email", "")

    # Look up user UUID by email so we can update their profile.
    user_id: str | None = None
    try:
        all_users = svc.auth.admin.list_users()
        match = next((u for u in all_users if getattr(u, "email", "") == user_email), None)
        if match:
            user_id = str(match.id)
    except Exception:
        pass

    if user_id:
        try:
            profile_service.upsert_profile(user_id, {
                "spotify_url": body.spotify_url,
                "apple_music_url": body.apple_music_url,
            })
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Profile update failed: {exc}")

    try:
        upd = (
            svc.table("new_artist_queue")
            .update({
                "spotify_url": body.spotify_url,
                "apple_music_url": body.apple_music_url,
                "status": "updated",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", entry_id)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Queue update failed: {exc}")

    return upd.data[0] if upd.data else {}


# ---------------------------------------------------------------------------
# User edit / delete
# ---------------------------------------------------------------------------


@router.patch("/users/{user_id}", dependencies=[Depends(_require_admin)])
async def update_user(user_id: str, body: AdminUserUpdate) -> dict:
    svc = get_service_client()
    plan_changed: Plan | None = None
    try:
        # 1. Validate + apply plan change first (before touching profile/meta)
        if body.plan is not None:
            try:
                plan_changed = Plan(body.plan)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid plan '{body.plan}'. Valid values: {[p.value for p in Plan]}",
                )
            assign_plan(user_id, plan_changed)

        # 2. Update auth user_metadata (full_name, artist_name, phone)
        meta = {k: v for k, v in {
            "full_name": body.full_name,
            "artist_name": body.artist_name,
            "phone": body.phone,
        }.items() if v is not None}
        if meta:
            svc.auth.admin.update_user_by_id(user_id, {"user_metadata": meta})

        # 3. Upsert profile fields (exclude plan — not a profile column)
        profile_fields = {
            k: v for k, v in body.model_dump().items()
            if k not in ("full_name", "artist_name", "phone", "plan") and v is not None
        }
        if profile_fields:
            profile_service.upsert_profile(user_id, profile_fields)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not update user: {exc}",
        ) from exc

    result: dict = {"updated": True, "user_id": user_id}
    if plan_changed is not None:
        spec = get_spec(plan_changed)
        result["plan"] = plan_changed.value
        result["plan_name"] = spec.name
    return result


@router.delete("/users/{user_id}", dependencies=[Depends(_require_admin)])
async def delete_user_endpoint(user_id: str) -> dict:
    svc = get_service_client()
    try:
        svc.auth.admin.delete_user(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not delete user: {exc}",
        ) from exc
    return {"deleted": True, "user_id": user_id}


# ---------------------------------------------------------------------------
# Plan purchases
# ---------------------------------------------------------------------------


@router.get("/purchases", dependencies=[Depends(_require_admin)])
async def list_purchases() -> dict:
    """Return all confirmed (paid) plan purchases with user details.

    Fetches every subscription where plan != 'free', joins with auth user data,
    and computes total active revenue for the stats panel.
    """
    svc = get_service_client()
    try:
        subs_resp = (
            svc.table("subscriptions")
            .select("*")
            .neq("plan", "free")
            .order("started_at", desc=True)
            .execute()
        )
        subs = subs_resp.data or []
        raw_users = _fetch_all_users(svc)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch purchases: {exc}",
        ) from exc

    user_map: dict[str, dict] = {}
    for u in raw_users:
        uid = str(getattr(u, "id", "") or "")
        user_meta: dict = getattr(u, "user_metadata", None) or {}
        user_map[uid] = {
            "email": getattr(u, "email", "") or "",
            "full_name": user_meta.get("full_name", "") or "",
            "artist_name": user_meta.get("artist_name", "") or "",
        }

    purchases = []
    plan_counts: dict[str, int] = {}
    total_revenue = 0

    for sub in subs:
        uid = sub.get("user_id", "")
        user = user_map.get(uid, {})
        plan_key = sub.get("plan") or ""
        if not plan_key or plan_key == "free":
            continue
        plan_name = _PLAN_NAMES.get(plan_key, plan_key.replace("-", " ").replace("_", " ").title())
        plan_price = _PLAN_PRICES_INR.get(plan_key, 0)

        if sub.get("status") == "active":
            plan_counts[plan_key] = plan_counts.get(plan_key, 0) + 1
            total_revenue += plan_price

        purchases.append({
            "id": sub.get("id"),
            "user_id": uid,
            "email": user.get("email", ""),
            "full_name": user.get("full_name", ""),
            "artist_name": user.get("artist_name", ""),
            "plan": plan_key,
            "plan_name": plan_name,
            "plan_price_inr": plan_price,
            "status": sub.get("status", ""),
            "payment_ref": sub.get("payment_ref"),
            "started_at": sub.get("started_at"),
            "expires_at": sub.get("expires_at"),
        })

    return {
        "purchases": purchases,
        "total": len(purchases),
        "plan_counts": plan_counts,
        "total_revenue_inr": total_revenue,
    }


# ---------------------------------------------------------------------------
# Home content management
# ---------------------------------------------------------------------------

_ALLOWED_IMAGE_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB


@router.get("/home", dependencies=[Depends(_require_admin)])
async def admin_get_home() -> HomeContent:
    try:
        return home_service.get_home_content()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch home content: {exc}",
        ) from exc


@router.put("/home", dependencies=[Depends(_require_admin)])
async def admin_update_home(body: HomeContent) -> HomeContent:
    try:
        return home_service.upsert_home_content(body)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not save home content: {exc}",
        ) from exc


@router.post("/home/artist-image", dependencies=[Depends(_require_admin)])
async def upload_artist_image(file: UploadFile = File(...)) -> dict[str, str]:
    if not settings.r2_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="R2 storage is not configured. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and R2_BUCKET_NAME.",
        )
    if file.content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only JPEG, PNG, or WebP images are allowed.",
        )
    data = await file.read()
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File size must not exceed 5 MB.",
        )
    ext = _ALLOWED_IMAGE_TYPES[file.content_type]
    key = f"home/artists/{uuid4().hex}{ext}"
    content_type = file.content_type
    try:
        await run_in_threadpool(upload_bytes, key, data, content_type)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Image upload to R2 failed: {exc}",
        ) from exc
    base = settings.oauth_callback_base_url.rstrip("/")
    return {"url": f"{base}/home/assets/{key}"}


# ---------------------------------------------------------------------------
# Media download (R2 presigned GET)
# ---------------------------------------------------------------------------


@router.get("/media/download-url", dependencies=[Depends(_require_admin)])
async def get_download_url(key: str = Query(...)) -> dict:
    """Generate a 15-minute presigned GET URL so the admin can download an R2 file."""
    if not settings.r2_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="R2 storage not configured.",
        )
    if not key.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="key is required.")
    try:
        url = presign_get(key.strip(), expires_in=900)
        return {"url": url, "expires_in": 900}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not generate download URL: {exc}",
        ) from exc
