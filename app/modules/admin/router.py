"""Admin router — internal user management and submission review endpoints.

All routes require X-Admin-Secret header matching settings.admin_secret.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.supabase_client import get_service_client
from app.modules.profile import service as profile_service

router = APIRouter(prefix="/admin", tags=["admin"])

_PLAN_NAMES: dict[str, str] = {
    "free": "Free",
    "starter": "Starter",
    "single_artist": "Single Artist",
    "double_artist": "Double Artist",
    "label": "Label",
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
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch users: {exc}",
        ) from exc

    sub_map: dict[str, dict] = {
        row["user_id"]: row for row in (subs_resp.data or [])
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
