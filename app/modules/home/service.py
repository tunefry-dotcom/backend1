from __future__ import annotations

from datetime import datetime, timezone

from app.core.supabase_client import get_service_client
from app.modules.home.schemas import HomeContent

_TABLE = "home_content"
_ROW_ID = 1


def get_home_content() -> HomeContent:
    svc = get_service_client()
    try:
        resp = (
            svc.table(_TABLE)
            .select("*")
            .eq("id", _ROW_ID)
            .limit(1)
            .execute()
        )
        if resp.data:
            return HomeContent(**resp.data[0])
    except Exception as exc:
        # Table not yet created — return safe defaults rather than 500
        msg = str(exc)
        if (
            "PGRST205" in msg
            or "does not exist" in msg.lower()
            or "schema cache" in msg.lower()
        ):
            return HomeContent()
        raise
    return HomeContent()


def upsert_home_content(data: HomeContent) -> HomeContent:
    svc = get_service_client()
    payload = {
        "id": _ROW_ID,
        **data.model_dump(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    resp = svc.table(_TABLE).upsert(payload, on_conflict="id").execute()
    if resp.data:
        return HomeContent(**resp.data[0])
    return data
