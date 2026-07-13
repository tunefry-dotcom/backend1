"""Request/response models for the profile API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class UpdateProfileRequest(BaseModel):
    """Partial update — only the fields the client sends are changed."""

    full_name: Optional[str] = None
    artist_name: Optional[str] = None
    phone: Optional[str] = None
    date_of_birth: Optional[str] = None  # ISO date, e.g. "2000-01-31"
    gender: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    bio: Optional[str] = None
    spotify_url: Optional[str] = None
    apple_music_url: Optional[str] = None
    instagram: Optional[str] = None
    youtube_url: Optional[str] = None


class ProfileResponse(BaseModel):
    full_name: Optional[str] = None
    artist_name: Optional[str] = None
    phone: Optional[str] = None
    date_of_birth: Optional[str] = None
    gender: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    bio: Optional[str] = None
    spotify_url: Optional[str] = None
    apple_music_url: Optional[str] = None
    instagram: Optional[str] = None
    youtube_url: Optional[str] = None
    # Whether the required basic details are filled (gates plan payment).
    is_complete: bool = False
    missing_fields: list[str] = []
