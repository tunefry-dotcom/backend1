"""Admin router — internal user management and submission review endpoints.

All routes require X-Admin-Secret header matching settings.admin_secret.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.email import send_email, submission_review_email_html
from app.core.r2_client import delete_keys, presign_get, upload_bytes
from app.core.supabase_client import get_service_client
from app.modules.billing.plans import Plan, get_spec
from app.modules.billing.service import assign_plan
from app.modules.home import service as home_service
from app.modules.home.schemas import HomeContent
from app.modules.profile import service as profile_service

_log = logging.getLogger(__name__)

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
    "single-song": 299,
    "starter": 999,
    "single-artist": 1599,
    "double-artist": 2999,
    "label": 6999,
    "single_song": 299,
    "single_artist": 1599,
    "double_artist": 2999,
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


def _fetch_all_rows(svc: Any, table: str, columns: str) -> list:
    """Paginate through a PostgREST table to bypass the default 1 000-row cap.

    PostgREST returns at most 1 000 rows by default.  With 2 300+ users each
    having a subscriptions/profiles row, any plain .execute() silently drops
    the tail — meaning those users always appear as free/blank in the admin
    panel.  This function pages through with .range() until all rows are in.
    """
    rows: list = []
    page_size = 1000
    start = 0
    while True:
        resp = (
            svc.table(table)
            .select(columns)
            .range(start, start + page_size - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


@router.get("/users", dependencies=[Depends(_require_admin)])
async def list_users(q: str = Query(default="")) -> dict:
    """Return all users with their plan and subscription status.

    Optional ?q= filters by email or full_name (case-insensitive).
    """
    try:
        svc = get_service_client()
        raw_users = _fetch_all_users(svc)
        all_subs = _fetch_all_rows(
            svc, "subscriptions", "user_id,plan,status,expires_at,started_at"
        )
        all_profiles = _fetch_all_rows(
            svc, "profiles",
            "user_id,full_name,artist_name,phone,spotify_url,apple_music_url,instagram,youtube_url,city,state,bio,gender,date_of_birth",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch users: {exc}",
        ) from exc

    sub_map: dict[str, dict] = {row["user_id"]: row for row in all_subs}
    profile_map: dict[str, dict] = {row["user_id"]: row for row in all_profiles}

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
        # Profiles table is authoritative; fall back to user_meta for legacy users
        # whose profiles row predates admin edits writing these fields there.
        full_name = prof.get("full_name") or full_name
        artist_name = prof.get("artist_name") or artist_name
        phone = prof.get("phone") or phone

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
    "new-songs":        ["new_song"],
    "transfer-songs":   ["transfer_song"],
    "new-albums":       ["new_album"],
    "transfer-albums":  ["transfer_album"],
    "profile-mismatch": ["profile_mismatch"],
    "claim-removal":    ["claim_removal"],
    "insta-link":       ["insta_link"],
}


class ReviewBody(BaseModel):
    status: str        # "approved" | "declined"
    admin_note: str = ""


class BroadcastBody(BaseModel):
    title: str
    body: str = ""


def _submission_title(data: dict) -> str:
    """Best-effort display title for a submission (mirrors the frontend subTitle)."""
    for key in ("song_title", "album_name", "section_name", "song_name", "instagram_url"):
        val = data.get(key)
        if val:
            return str(val)
    return ""


def _submission_keys(data: dict) -> set[str]:
    """All R2 object keys referenced by a submission's data payload."""
    keys = {data.get("cover_art_key"), data.get("audio_key")}
    for song in (data.get("songs") or []):
        keys.add(song.get("audio_key"))
    return {k for k in keys if k}


@router.get("/submissions/{category}", dependencies=[Depends(_require_admin)])
async def list_submissions(
    category: str,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=10, ge=1, le=50),
    q: str = Query(default=""),
    plan: str = Query(default=""),
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

    # Overwrite the stored user_plan snapshot with the user's *current* plan,
    # joined live from subscriptions (same source as the All Users tab). The
    # stored value is captured from the JWT at submission time and is stale
    # (or free) after upgrades or if the access-token hook isn't stamping it.
    try:
        raw_users = _fetch_all_users(svc)
        all_subs = _fetch_all_rows(
            svc, "subscriptions", "user_id,plan,status,expires_at,started_at"
        )
        sub_map: dict[str, dict] = {row["user_id"]: row for row in all_subs}
        email_plan: dict[str, str] = {}
        for u in raw_users:
            uid = str(getattr(u, "id", "") or "")
            email = (getattr(u, "email", "") or "").lower()
            if email:
                email_plan[email] = sub_map.get(uid, {}).get("plan") or "free"
        for item in all_items:
            key = (item.get("user_email") or "").lower()
            item["user_plan"] = email_plan.get(key, item.get("user_plan") or "free")
    except Exception:
        pass  # best-effort — fall back to the stored snapshot on any failure

    # Filter by the user's (live-joined) plan.
    if plan and plan.lower() != "all":
        all_items = [it for it in all_items if (it.get("user_plan") or "free") == plan]

    # Search by song/album title or user email (case-insensitive substring).
    query = q.strip().lower()
    if query:
        def _matches(it: dict) -> bool:
            title = _submission_title(it.get("data") or {}).lower()
            email = (it.get("user_email") or "").lower()
            return query in title or query in email
        all_items = [it for it in all_items if _matches(it)]

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

    # Fire-and-forget email to the artist (non-blocking — approval/decline is never held back).
    _user_email: str = submission.get("user_email") or ""
    if _user_email:
        _title = _submission_title(submission.get("data") or {}) or (
            submission.get("submission_type", "submission").replace("_", " ").title()
        )
        _subject = (
            "Your submission was approved 🎉"
            if body.status == "approved"
            else f"Submission update — {_title}"
        )
        try:
            asyncio.create_task(
                send_email(
                    to=_user_email,
                    subject=_subject,
                    html_body=submission_review_email_html(body.status, _title, body.admin_note),
                )
            )
        except Exception:
            _log.warning("Could not schedule submission review email", exc_info=True)

    return submission


@router.post("/notifications", dependencies=[Depends(_require_admin)])
async def create_notification(body: BroadcastBody) -> dict:
    """Push a broadcast announcement to all users' notification bell."""
    if not body.title.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="title is required")
    svc = get_service_client()
    try:
        resp = (
            svc.table("notifications")
            .insert({"title": body.title.strip(), "body": body.body.strip()})
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not create notification: {exc}",
        ) from exc
    row = resp.data[0] if resp.data else {}
    return {"ok": True, "id": row.get("id")}


# ---------------------------------------------------------------------------
# Withdrawal requests
# ---------------------------------------------------------------------------
def _to_decimal(v: Any) -> Decimal:
    try:
        return Decimal(str(v)) if v is not None else Decimal("0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _age_from_dob(dob: Any) -> int | None:
    if not dob:
        return None
    try:
        d = dob if isinstance(dob, date) else datetime.fromisoformat(str(dob)[:10]).date()
    except (ValueError, TypeError):
        return None
    today = date.today()
    return today.year - d.year - ((today.month, today.day) < (d.month, d.day))


@router.get("/withdrawals", dependencies=[Depends(_require_admin)])
async def list_withdrawals() -> dict:
    """All withdrawal requests, pending first then newest — with the artist
    snapshot (plan / name / address / age) and payout details for payout."""
    svc = get_service_client()
    try:
        rows = _fetch_all_rows(
            svc, "withdrawal_requests",
            "id,user_id,user_email,amount,status,method,payout_details,"
            "snapshot,admin_note,requested_at,processed_at",
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Could not fetch withdrawals: {exc}") from exc
    # pending first; within each group, most-recently requested first
    rows.sort(key=lambda r: _fmt(r.get("requested_at")) or "", reverse=True)
    rows.sort(key=lambda r: r.get("status") != "pending")
    # Enrich snapshot with live profile data for any null fields (graceful — never blocks).
    try:
        uids = list({r["user_id"] for r in rows if r.get("user_id")})
        if uids:
            profiles_raw = (
                svc.table("profiles")
                .select("id,phone,city,state,date_of_birth")
                .in_("id", uids)
                .execute()
                .data or []
            )
            prof_map = {p["id"]: p for p in profiles_raw}
            for row in rows:
                prof = prof_map.get(row.get("user_id"), {})
                snap = row.get("snapshot") or {}
                if not snap.get("phone"):
                    snap["phone"] = prof.get("phone")
                if not snap.get("city"):
                    snap["city"] = prof.get("city")
                if not snap.get("state"):
                    snap["state"] = prof.get("state")
                if snap.get("age") is None:
                    snap["age"] = _age_from_dob(prof.get("date_of_birth"))
                row["snapshot"] = snap
    except Exception:
        pass  # enrichment failure must never break the admin list
    total_pending = sum(_to_decimal(r["amount"]) for r in rows if r.get("status") == "pending")
    return {"requests": rows, "total_pending": float(total_pending)}


class WithdrawalReviewBody(BaseModel):
    status: str  # only "paid" is accepted (admin marks a request as paid)
    admin_note: str | None = None


@router.patch("/withdrawals/{request_id}", dependencies=[Depends(_require_admin)])
async def review_withdrawal(request_id: str, body: WithdrawalReviewBody) -> dict:
    """Mark a request as paid. Balance was already zeroed at request time, so no
    balance change is needed here (the amount stays counted as withdrawn)."""
    if body.status != "paid":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="status must be 'paid'")
    svc = get_service_client()
    try:
        resp = (svc.table("withdrawal_requests").update({
            "status": "paid",
            "admin_note": body.admin_note,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", request_id).execute())
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Could not update withdrawal: {exc}") from exc
    if not resp.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return {"ok": True, "request": resp.data[0]}


@router.delete("/withdrawals/{request_id}", dependencies=[Depends(_require_admin)])
async def delete_withdrawal(request_id: str) -> dict:
    """Delete a request. If it was NOT paid, credit the reserved amount back to
    the artist's available_balance so no earnings are lost (the request had
    zeroed it). Deleting a paid request never credits back."""
    svc = get_service_client()
    try:
        res = (svc.table("withdrawal_requests")
               .select("user_email,amount,status").eq("id", request_id).limit(1).execute())
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Could not fetch withdrawal: {exc}") from exc
    row = (res.data or [None])[0]
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")

    if row.get("status") != "paid":
        email = (row.get("user_email") or "").lower()
        bal = (svc.table("artist_balances").select("available_balance")
               .eq("user_email", email).limit(1).execute())
        current = _to_decimal((bal.data or [{}])[0].get("available_balance"))
        new_balance = current + _to_decimal(row.get("amount"))
        svc.table("artist_balances").update({
            "available_balance": str(new_balance),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }).eq("user_email", email).execute()

    svc.table("withdrawal_requests").delete().eq("id", request_id).execute()
    return {"ok": True, "deleted": request_id}


class DeleteBody(BaseModel):
    ids: list[str]


@router.delete("/submissions", dependencies=[Depends(_require_admin)])
async def delete_submissions(body: DeleteBody) -> dict:
    """Delete one or more submissions (DB rows) and their orphaned R2 files.

    R2 object keys are derived from {artist}/{release} and are NOT unique per
    submission, so a key is only removed from R2 if no *surviving* submission
    still references it (otherwise deleting one row would break another's files).
    """
    ids = [i for i in body.ids if i]
    if not ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No ids provided")

    svc = get_service_client()

    # 1. Collect candidate R2 keys from the rows being deleted.
    try:
        rows = svc.table("submissions").select("id,data").in_("id", ids).execute().data or []
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch submissions: {exc}",
        ) from exc

    candidate_keys: set[str] = set()
    for row in rows:
        candidate_keys |= _submission_keys(row.get("data") or {})

    # 2. Delete the DB rows.
    try:
        svc.table("submissions").delete().in_("id", ids).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not delete submissions: {exc}",
        ) from exc

    # 3. Best-effort R2 cleanup — only keys no surviving submission references.
    if settings.r2_enabled and candidate_keys:
        try:
            survivors = svc.table("submissions").select("data").execute().data or []
            still_referenced: set[str] = set()
            for s in survivors:
                still_referenced |= _submission_keys(s.get("data") or {})
            delete_keys(list(candidate_keys - still_referenced))
        except Exception:
            pass  # best-effort — never fail the delete on R2 cleanup

    return {"deleted": len(rows)}


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

        # 2. Upsert ALL profile fields into the profiles table.
        # This includes full_name/artist_name/phone — profiles is the authoritative
        # store; auth.user_metadata is synced separately as a best-effort step.
        # Skip None and "" — Postgres date columns reject empty strings,
        # and we don't want to clear fields the admin left blank.
        profile_fields = {
            k: v for k, v in body.model_dump().items()
            if k != "plan"
            and v is not None
            and v != ""
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

    # 3. Best-effort sync of name fields to auth.user_metadata so JWT claims
    # stay current. Failure here is non-fatal — profile table is already saved.
    meta = {k: v for k, v in {
        "full_name": body.full_name,
        "artist_name": body.artist_name,
        "phone": body.phone,
    }.items() if v is not None and v != ""}
    if meta:
        try:
            svc.auth.admin.update_user_by_id(user_id, {"user_metadata": meta})
        except Exception as exc:
            _log.warning("Could not sync user_metadata for %s: %s", user_id, exc)

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
