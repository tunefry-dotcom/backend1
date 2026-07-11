"""Admin router — internal user management endpoints.

All routes require X-Admin-Secret header matching settings.admin_secret.
Uses the service-role client to read auth.users + subscriptions.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

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
        plan: str = app_meta.get("plan", "free") or "free"
        sub = sub_map.get(uid, {})

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
