"""Profile router — read and update the signed-in user's basic details."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from app.modules.auth.dependencies import CurrentUser, get_current_user
from app.modules.profile import service
from app.modules.profile.schemas import ProfileResponse, UpdateProfileRequest

router = APIRouter(prefix="/profile", tags=["profile"])


def _response(row: Optional[dict[str, Any]]) -> ProfileResponse:
    row = row or {}
    missing = service.missing_required(row)
    return ProfileResponse(
        full_name=row.get("full_name"),
        artist_name=row.get("artist_name"),
        phone=row.get("phone"),
        date_of_birth=row.get("date_of_birth"),
        gender=row.get("gender"),
        city=row.get("city"),
        state=row.get("state"),
        bio=row.get("bio"),
        spotify_url=row.get("spotify_url"),
        apple_music_url=row.get("apple_music_url"),
        instagram=row.get("instagram"),
        youtube_url=row.get("youtube_url"),
        is_complete=not missing,
        missing_fields=missing,
    )


@router.get("/me", response_model=ProfileResponse)
async def get_my_profile(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProfileResponse:
    return _response(service.get_profile(current_user.id))


@router.put("/me", response_model=ProfileResponse)
async def update_my_profile(
    body: UpdateProfileRequest,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> ProfileResponse:
    fields = body.model_dump(exclude_none=True)
    try:
        row = service.upsert_profile(current_user.id, fields)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to save profile: {exc}",
        )
    return _response(row)
