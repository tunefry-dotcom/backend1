"""Submission router — artists submit songs, albums, and support requests.

All endpoints require an authenticated session (httpOnly cookie or Bearer).
Form data is stored as JSONB.  File fields (cover_art, audio_file, audio_N)
are uploaded to R2 when configured; their R2 keys are stored instead of
the raw bytes.  Files are ignored gracefully if R2 is not configured.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.config import settings
from app.core.r2_client import (
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_COVER_EXTENSIONS,
    build_key,
    upload_bytes,
)
from app.core.supabase_client import get_service_client
from app.modules.auth.dependencies import CurrentUser, get_current_user

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/submissions", tags=["submissions"])


@router.get("/my")
async def my_submissions(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    """Return the signed-in user's own submissions, newest first."""
    try:
        resp = (
            get_service_client()
            .table("submissions")
            .select("*")
            .eq("user_email", (current_user.email or "").lower())
            .order("created_at", desc=True)
            .execute()
        )
        return {"submissions": resp.data or []}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch submissions: {exc}",
        ) from exc


async def _parse_form(request: Request, user: Optional[CurrentUser] = None) -> dict:
    """Parse multipart/form-data into a plain dict, uploading files to R2.

    Two-pass approach:
      Pass 1 — collect all text fields and buffer UploadFile objects.
      Pass 2 — upload file fields to R2 with the now-known release name.

    File fields handled:
      cover_art     → validated (JPEG/PNG), uploaded, stored as cover_art_key
      audio_file    → validated (WAV/MP3/FLAC), uploaded, stored as audio_key
      audio_N       → album track N, uploaded, injected into songs[N-1].audio_key
      anything else → filename stored as {field}_name (e.g. evidence_name)

    Falls back to filename-only storage when R2 is not configured.
    """
    form = await request.form()
    data: dict = {}
    file_items: dict = {}  # field_key → UploadFile

    # ── Pass 1: text fields ────────────────────────────────────────────────
    for key, val in form.multi_items():
        if hasattr(val, "filename"):
            if val.filename:
                file_items[key] = val
        elif key not in data:
            data[key] = val

    # Decode JSON list fields
    for list_key in ("main_artists", "featured_artists", "songs"):
        if list_key in data and isinstance(data[list_key], str):
            try:
                data[list_key] = json.loads(data[list_key])
            except Exception:
                data[list_key] = []

    if not file_items:
        return data

    # ── Pass 2: file uploads ───────────────────────────────────────────────
    if not settings.r2_enabled or user is None:
        for key, uf in file_items.items():
            data[f"{key}_name"] = uf.filename
        return data

    artist_name = user.artist_name or (user.email or "unknown").split("@")[0]
    release_name = (data.get("song_title") or data.get("album_name") or "release").strip()

    for field_key, uf in file_items.items():
        ext = os.path.splitext(uf.filename.lower())[1]
        content = await uf.read()
        if not content:
            continue
        ct = uf.content_type or "application/octet-stream"

        if field_key == "cover_art":
            if ext not in ALLOWED_COVER_EXTENSIONS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cover art must be a JPEG or PNG file.",
                )
            r2_key = build_key(artist_name, release_name, "cover_art", ext)
            try:
                upload_bytes(r2_key, content, ct)
            except Exception as exc:
                _log.error("R2 cover_art upload failed: %s", exc)
                raise HTTPException(502, "Cover art upload failed. Please try again.") from exc
            data["cover_art_key"] = r2_key

        elif field_key == "audio_file":
            if ext not in ALLOWED_AUDIO_EXTENSIONS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Audio must be a WAV, MP3, or FLAC file.",
                )
            r2_key = build_key(artist_name, release_name, "audio", ext)
            try:
                upload_bytes(r2_key, content, ct)
            except Exception as exc:
                _log.error("R2 audio upload failed: %s", exc)
                raise HTTPException(502, "Audio upload failed. Please try again.") from exc
            data["audio_key"] = r2_key

        elif re.match(r"^audio_(\d+)$", field_key):
            # Album track — e.g. audio_1, audio_2
            track_num = int(re.match(r"^audio_(\d+)$", field_key).group(1))
            if ext not in ALLOWED_AUDIO_EXTENSIONS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Track {track_num} audio must be WAV, MP3, or FLAC.",
                )
            r2_key = build_key(artist_name, release_name, "audio", ext, track_number=track_num)
            try:
                upload_bytes(r2_key, content, ct)
            except Exception as exc:
                _log.error("R2 track %d upload failed: %s", track_num, exc)
                raise HTTPException(502, f"Track {track_num} upload failed.") from exc
            # Inject into songs list
            songs = data.get("songs")
            if isinstance(songs, list) and 0 < track_num <= len(songs):
                songs[track_num - 1]["audio_key"] = r2_key
            data[f"audio_{track_num}_key"] = r2_key

        else:
            # Unknown file field (e.g. evidence in claim-removal)
            data[f"{field_key}_name"] = uf.filename

    return data


def _save(user: CurrentUser, submission_type: str, data: dict) -> None:
    try:
        get_service_client().table("submissions").insert(
            {
                "user_email": (user.email or "").lower(),
                "user_plan": user.plan.value,
                "submission_type": submission_type,
                "data": data,
            }
        ).execute()
    except Exception as exc:
        _log.error("submission insert failed (%s): %s", submission_type, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to save submission. Please try again.",
        ) from exc


@router.post("/song", status_code=status.HTTP_201_CREATED)
async def submit_song(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    data = await _parse_form(request, user=current_user)
    sub_type = data.pop("submission_type", "new_song")
    if sub_type not in ("new_song", "transfer_song"):
        sub_type = "new_song"
    _save(current_user, sub_type, data)
    return {"ok": True}


@router.post("/album", status_code=status.HTTP_201_CREATED)
async def submit_album(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    data = await _parse_form(request, user=current_user)
    sub_type = data.pop("submission_type", "new_album")
    if sub_type not in ("new_album", "transfer_album"):
        sub_type = "new_album"
    _save(current_user, sub_type, data)
    return {"ok": True}


@router.post("/profile-mismatch", status_code=status.HTTP_201_CREATED)
async def submit_profile_mismatch(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    data = await _parse_form(request, user=current_user)
    _save(current_user, "profile_mismatch", data)
    return {"ok": True}


@router.post("/claim-removal", status_code=status.HTTP_201_CREATED)
async def submit_claim_removal(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    data = await _parse_form(request, user=current_user)
    _save(current_user, "claim_removal", data)
    return {"ok": True}


@router.post("/insta-link", status_code=status.HTTP_201_CREATED)
async def submit_insta_link(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    data = await _parse_form(request, user=current_user)
    _save(current_user, "insta_link", data)
    return {"ok": True}
