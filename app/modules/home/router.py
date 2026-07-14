from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.core.r2_client import presign_get
from app.modules.home import service as home_service
from app.modules.home.schemas import HomeContent

router = APIRouter(prefix="/home", tags=["home"])


@router.get("/content")
async def get_home_content(response: Response) -> HomeContent:
    response.headers["Cache-Control"] = "public, max-age=300"
    return home_service.get_home_content()


@router.get("/assets/{key:path}", include_in_schema=False)
async def proxy_home_asset(key: str) -> RedirectResponse:
    """307-redirect to a fresh 1-hour R2 presigned URL (home/* keys only)."""
    if not key.startswith("home/"):
        raise HTTPException(status_code=404, detail="Not found.")
    if not settings.r2_enabled:
        raise HTTPException(status_code=404, detail="Storage not configured.")
    url = presign_get(key, expires_in=3600)
    return RedirectResponse(url=url, status_code=307)
