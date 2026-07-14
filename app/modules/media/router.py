"""Media presign endpoint — generates presigned R2 PUT URLs for direct browser uploads."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.r2_client import (
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_AUDIO_TYPES,
    ALLOWED_COVER_EXTENSIONS,
    ALLOWED_COVER_TYPES,
    build_key,
    presign_put,
)
from app.modules.auth.dependencies import CurrentUser, get_current_user

router = APIRouter(prefix="/media", tags=["media"])


class PresignRequest(BaseModel):
    artist_name: str
    release_name: str
    file_type: str          # "cover_art" | "audio"
    content_type: str
    file_name: str
    track_number: int | None = None  # 1-based index for album tracks


@router.post("/presign")
async def presign_upload(
    body: PresignRequest,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    """Return a presigned PUT URL so the browser can upload directly to R2.

    File type + extension are validated server-side even though the browser
    also validates, so a malicious client can't store arbitrary files.
    The artist folder is derived from the verified JWT (not the request body)
    to prevent path traversal.
    """
    if not settings.r2_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File storage not configured. Contact support.",
        )

    ext = os.path.splitext(body.file_name.lower())[1]

    if body.file_type == "cover_art":
        # Accept by MIME or extension (browser MIME detection is unreliable)
        if body.content_type not in ALLOWED_COVER_TYPES and ext not in ALLOWED_COVER_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cover art must be a JPEG or PNG file.",
            )
    elif body.file_type == "audio":
        if body.content_type not in ALLOWED_AUDIO_TYPES and ext not in ALLOWED_AUDIO_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Audio must be WAV, MP3, or FLAC.",
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="file_type must be 'cover_art' or 'audio'.",
        )

    if not body.release_name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="release_name is required.",
        )

    # Use artist_name from the verified JWT — prevents path manipulation
    artist_name = (
        current_user.artist_name
        or body.artist_name
        or (current_user.email or "unknown").split("@")[0]
    )

    key = build_key(
        artist_name=artist_name,
        release_name=body.release_name.strip(),
        file_type=body.file_type,
        ext=ext,
        track_number=body.track_number,
    )

    # Use the actual MIME from the file extension when browser sends octet-stream
    resolved_ct = body.content_type
    if body.content_type == "audio/octet-stream":
        _ext_map = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac"}
        resolved_ct = _ext_map.get(ext, body.content_type)

    try:
        upload_url = presign_put(key, resolved_ct)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not generate upload URL: {exc}",
        ) from exc

    return {"upload_url": upload_url, "key": key}
