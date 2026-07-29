"""Notifications router — admin broadcast announcements."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.supabase_client import get_service_client
from app.modules.auth.dependencies import CurrentUser, get_current_user

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/announcements")
async def list_announcements(
    _current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[dict]:
    """Return the 20 most recent admin broadcast notifications."""
    svc = get_service_client()
    resp = (
        svc.table("notifications")
        .select("id,title,body,created_at")
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    )
    return resp.data or []
