"""Notifications router — admin broadcast announcements."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.supabase_client import get_service_client
from app.modules.auth.dependencies import CurrentUser, get_current_user

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/announcements")
async def list_announcements(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[dict]:
    """Return the 20 most recent notifications for this user: broadcasts
    (recipient_email IS NULL) merged with this user's own personal
    notifications (e.g. blog review outcomes), newest first.
    """
    svc = get_service_client()
    email = (current_user.email or "").lower()

    broadcasts = (
        svc.table("notifications")
        .select("id,title,body,created_at")
        .is_("recipient_email", "null")
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    ).data or []

    personal: list[dict] = []
    if email:
        personal = (
            svc.table("notifications")
            .select("id,title,body,created_at")
            .eq("recipient_email", email)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        ).data or []

    merged = sorted(broadcasts + personal, key=lambda r: r["created_at"], reverse=True)
    return merged[:20]
