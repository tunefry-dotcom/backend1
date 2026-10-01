from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.r2_client import delete_keys, upload_bytes
from app.core.supabase_client import get_service_client

_log = logging.getLogger(__name__)

_TABLE = "blog_posts"

ALLOWED_IMAGE_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    base = _SLUG_RE.sub("-", title.lower()).strip("-")[:60] or "article"
    return f"{base}-{uuid4().hex[:6]}"


def _is_unique_violation(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "duplicate key" in msg or "unique" in msg or "23505" in msg


def _insert_with_slug(title: str, row_without_slug: dict[str, Any]) -> dict[str, Any]:
    svc = get_service_client()
    last_exc: Optional[Exception] = None
    for _ in range(3):
        row = {**row_without_slug, "slug": slugify(title)}
        try:
            res = svc.table(_TABLE).insert(row).execute()
            data = res.data or [row]
            return data[0]
        except Exception as exc:  # noqa: BLE001 - retried below, or re-raised
            last_exc = exc
            if not _is_unique_violation(exc):
                raise
    raise RuntimeError("Could not generate a unique slug after 3 attempts.") from last_exc


async def save_blog_image(file: UploadFile) -> str:
    """Validate + upload one cover image to R2, return its key.

    Shared by the artist (/blog/images) and admin (/admin/blog/images) upload
    endpoints so validation rules can't drift between the two trust
    boundaries, even though the routes themselves stay separate.
    """
    if not settings.r2_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="R2 storage is not configured.",
        )
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only JPEG, PNG, or WebP images are allowed.",
        )
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File size must not exceed 5 MB.",
        )
    ext = ALLOWED_IMAGE_TYPES[file.content_type]
    key = f"blog/{uuid4().hex}{ext}"
    try:
        await run_in_threadpool(upload_bytes, key, data, file.content_type)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Image upload to R2 failed: {exc}",
        ) from exc
    return key


# --- Row -> response mapping ------------------------------------------------

def _make_excerpt(text: str, limit: int = 140) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def to_summary(row: dict[str, Any]) -> dict[str, Any]:
    cover_keys = row.get("cover_image_keys") or []
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row.get("final_title") or row.get("original_title") or "",
        "category": row["category"],
        "author_type": row["author_type"],
        "author_name": row.get("author_name") or "",
        "cover_image_key": cover_keys[0] if cover_keys else None,
        "excerpt": _make_excerpt(row.get("final_body") or row.get("original_body") or ""),
        "is_featured": bool(row.get("is_featured")),
        "is_popular": bool(row.get("is_popular")),
        "published_at": row.get("published_at"),
    }


def to_detail(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **to_summary(row),
        "body": row.get("final_body") or row.get("original_body") or "",
        "cover_image_keys": row.get("cover_image_keys") or [],
    }


def to_admin(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "author_type": row["author_type"],
        "author_email": row.get("author_email"),
        "author_name": row.get("author_name") or "",
        "category": row["category"],
        "status": row["status"],
        "original_title": row.get("original_title") or "",
        "original_body": row.get("original_body") or "",
        "final_title": row.get("final_title"),
        "final_body": row.get("final_body"),
        "cover_image_keys": row.get("cover_image_keys") or [],
        "admin_note": row.get("admin_note") or "",
        "is_featured": bool(row.get("is_featured")),
        "is_popular": bool(row.get("is_popular")),
        "reviewed_at": row.get("reviewed_at"),
        "published_at": row.get("published_at"),
        "created_at": row.get("created_at"),
    }


# --- Writes ------------------------------------------------------------

def create_pending_post(
    *,
    author_email: str,
    author_name: str,
    title: str,
    body: str,
    category: str,
    cover_image_key: str,
) -> dict[str, Any]:
    row = _insert_with_slug(
        title,
        {
            "author_type": "artist",
            "author_email": author_email,
            "author_name": author_name,
            "category": category,
            "status": "pending",
            "original_title": title,
            "original_body": body,
            "cover_image_keys": [cover_image_key],
        },
    )
    return to_detail(row)


def create_tunefry_post(
    *,
    final_title: str,
    final_body: str,
    category: str,
    cover_image_keys: list[str],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    row = _insert_with_slug(
        final_title,
        {
            "author_type": "tunefry",
            "author_email": None,
            "author_name": "Tunefry",
            "category": category,
            "status": "approved",
            "original_title": final_title,
            "original_body": final_body,
            "final_title": final_title,
            "final_body": final_body,
            "cover_image_keys": cover_image_keys,
            "reviewed_at": now,
            "published_at": now,
        },
    )
    return to_admin(row)


class ReviewError(ValueError):
    """Raised when an admin review request violates a required invariant."""


def _get_post_row(post_id: str) -> Optional[dict[str, Any]]:
    svc = get_service_client()
    res = svc.table(_TABLE).select("*").eq("id", post_id).limit(1).execute()
    rows = res.data or []
    return rows[0] if rows else None


def review_post(
    post_id: str,
    *,
    status_value: str,
    admin_note: str = "",
    final_title: Optional[str] = None,
    final_body: Optional[str] = None,
) -> dict[str, Any]:
    row = _get_post_row(post_id)
    if not row:
        raise ReviewError("not_found")

    update: dict[str, Any] = {
        "status": status_value,
        "admin_note": admin_note,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    if status_value == "approved":
        if not (final_title and final_title.strip()) or not (final_body and final_body.strip()):
            raise ReviewError("final_copy_required")
        update["final_title"] = final_title.strip()
        update["final_body"] = final_body.strip()
        update["published_at"] = datetime.now(timezone.utc).isoformat()
    elif status_value == "declined":
        if not admin_note.strip():
            raise ReviewError("admin_note_required")
        if final_title is not None:
            update["final_title"] = final_title.strip() or None
        if final_body is not None:
            update["final_body"] = final_body.strip() or None

    svc = get_service_client()
    res = svc.table(_TABLE).update(update).eq("id", post_id).execute()
    updated = (res.data or [{**row, **update}])[0]
    return to_admin(updated)


def delete_post(post_id: str) -> None:
    row = _get_post_row(post_id)
    if not row:
        raise ReviewError("not_found")
    svc = get_service_client()
    svc.table(_TABLE).delete().eq("id", post_id).execute()
    if settings.r2_enabled:
        keys = row.get("cover_image_keys") or []
        if keys:
            try:
                delete_keys(keys)
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                _log.warning("Could not delete R2 keys for blog post %s: %s", post_id, exc)


def set_flags(post_id: str, *, is_featured: Optional[bool], is_popular: Optional[bool]) -> dict[str, Any]:
    row = _get_post_row(post_id)
    if not row:
        raise ReviewError("not_found")
    if row.get("status") != "approved":
        raise ReviewError("not_approved")

    update: dict[str, Any] = {}
    if is_featured is not None:
        update["is_featured"] = is_featured
    if is_popular is not None:
        update["is_popular"] = is_popular
    if not update:
        return to_admin(row)

    svc = get_service_client()
    res = svc.table(_TABLE).update(update).eq("id", post_id).execute()
    updated = (res.data or [{**row, **update}])[0]
    return to_admin(updated)


# --- Reads ------------------------------------------------------------

def list_public_posts(*, category: Optional[str], page: int, per_page: int) -> dict[str, Any]:
    svc = get_service_client()
    per_page = max(1, min(per_page, 15))
    page = max(1, page)

    q = svc.table(_TABLE).select("*").eq("status", "approved")
    if category:
        q = q.eq("category", category)
    res = q.order("published_at", desc=True).execute()
    rows = res.data or []

    total = len(rows)
    total_pages = max(1, -(-total // per_page))
    offset = (page - 1) * per_page
    page_rows = rows[offset : offset + per_page]

    return {
        "posts": [to_summary(r) for r in page_rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


def get_post_by_slug(slug: str) -> Optional[dict[str, Any]]:
    svc = get_service_client()
    res = svc.table(_TABLE).select("*").eq("slug", slug).eq("status", "approved").limit(1).execute()
    rows = res.data or []
    return to_detail(rows[0]) if rows else None


def list_related_posts(slug: str, *, limit: int = 6) -> list[dict[str, Any]]:
    svc = get_service_client()
    current_res = svc.table(_TABLE).select("id,category").eq("slug", slug).limit(1).execute()
    current_rows = current_res.data or []
    if not current_rows:
        return []
    category = current_rows[0]["category"]
    current_id = current_rows[0]["id"]

    res = (
        svc.table(_TABLE)
        .select("*")
        .eq("status", "approved")
        .eq("category", category)
        .order("published_at", desc=True)
        .execute()
    )
    related = [r for r in (res.data or []) if r.get("id") != current_id]
    return [to_summary(r) for r in related[:limit]]


def list_my_posts(email: str, status_filter: Optional[str] = None) -> list[dict[str, Any]]:
    svc = get_service_client()
    q = svc.table(_TABLE).select("*").eq("author_email", email)
    if status_filter:
        q = q.eq("status", status_filter)
    res = q.order("created_at", desc=True).execute()
    return [to_summary(r) for r in (res.data or [])]


def list_admin_posts(author_type: str, status_filter: Optional[str] = None) -> list[dict[str, Any]]:
    svc = get_service_client()
    q = svc.table(_TABLE).select("*").eq("author_type", author_type)
    if status_filter:
        q = q.eq("status", status_filter)
    res = q.order("created_at", desc=True).execute()
    return [to_admin(r) for r in (res.data or [])]
