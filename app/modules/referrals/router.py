"""Referrals router — the artist-facing Refer & Earn dashboard endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.modules.auth.dependencies import CurrentUser, get_current_user
from app.modules.referrals import service

router = APIRouter(prefix="/referrals", tags=["referrals"])


@router.get("/me")
async def my_referrals(current_user: Annotated[CurrentUser, Depends(get_current_user)]) -> dict:
    """Own referral code, referred users, and total referral commission earned."""
    return service.list_my_referrals(current_user.id)
