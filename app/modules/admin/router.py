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
        sub = sub_map.get(uid, {})
        plan: str = sub.get("plan") or app_meta.get("plan", "free") or "free"

        users.append(
            {
                "id": uid,
                "email": email,
                "full_name": full_name,
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

    return resp.data[0]
