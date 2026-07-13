"""Profile persistence + completeness logic.

The `public.profiles` table (migration 0002) is written only via the service-role
client. A blank row is auto-created for every user by a DB trigger, so reads/updates
operate on an existing row.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.supabase_client import get_service_client

_TABLE = "profiles"

# Basic details a user must fill before they may pay to upgrade a plan.
REQUIRED_FIELDS: tuple[str, ...] = (
    "full_name",
    "artist_name",
    "phone",
    "city",
    "state",
    "date_of_birth",
)

# Fields a client is allowed to write (mirrors UpdateProfileRequest / the table).
EDITABLE_FIELDS: tuple[str, ...] = REQUIRED_FIELDS + (
    "gender",
    "bio",
    "spotify_url",
    "apple_music_url",
    "instagram",
    "youtube_url",
)


def get_profile(user_id: str) -> Optional[dict[str, Any]]:
    """Return the user's profile row, or None if unavailable (e.g. migration not
    applied yet) so callers can degrade gracefully instead of 500ing."""
    try:
        service = get_service_client()
        res = (
            service.table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def upsert_profile(user_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Insert/update the user's profile with the provided (whitelisted) fields.

    Only known editable fields are persisted — unknown keys are ignored. Raises on
    failure so the caller can surface an error.
    """
    record: dict[str, Any] = {
        k: v for k, v in fields.items() if k in EDITABLE_FIELDS
    }
    record["user_id"] = user_id

    service = get_service_client()
    res = (
        service.table(_TABLE)
        .upsert(record, on_conflict="user_id")
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else record


def missing_required(row: Optional[dict[str, Any]]) -> list[str]:
    """Required fields that are empty/absent in the row."""
    if not row:
        return list(REQUIRED_FIELDS)
    missing: list[str] = []
    for field in REQUIRED_FIELDS:
        value = row.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)
    return missing


def is_complete(row: Optional[dict[str, Any]]) -> bool:
    return not missing_required(row)
