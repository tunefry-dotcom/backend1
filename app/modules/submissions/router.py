"""Submission router — artists submit songs, albums, and support requests.

All endpoints require an authenticated session (httpOnly cookie or Bearer).
Form data is stored as JSONB; files are noted by filename only (not stored).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.supabase_client import get_service_client
from app.modules.auth.dependencies import CurrentUser, get_current_user

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/submissions", tags=["submissions"])


async def _parse_form(request: Request) -> dict:
    """Parse multipart/form-data into a plain dict.

    Text fields are stored as strings. File fields are reduced to their
    filename (no binary content stored). JSON-encoded list fields
    (main_artists, featured_artists, songs) are decoded.
    """
    form = await request.form()
    data: dict = {}
    for key, val in form.multi_items():
        if hasattr(val, "filename"):
            if val.filename:
                data[f"{key}_name"] = val.filename
        elif key not in data:
            data[key] = val

    for list_key in ("main_artists", "featured_artists", "songs"):
        if list_key in data and isinstance(data[list_key], str):
            try:
                data[list_key] = json.loads(data[list_key])
            except Exception:
                data[list_key] = []

    return data


def _save(user: CurrentUser, submission_type: str, data: dict) -> None:
    try:
        get_service_client().table("submissions").insert(
            {
                "user_email": user.email or "",
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
    data = await _parse_form(request)
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
    data = await _parse_form(request)
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
    data = await _parse_form(request)
    _save(current_user, "profile_mismatch", data)
    return {"ok": True}


@router.post("/claim-removal", status_code=status.HTTP_201_CREATED)
async def submit_claim_removal(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    data = await _parse_form(request)
    _save(current_user, "claim_removal", data)
    return {"ok": True}


@router.post("/insta-link", status_code=status.HTTP_201_CREATED)
async def submit_insta_link(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    data = await _parse_form(request)
    _save(current_user, "insta_link", data)
    return {"ok": True}
