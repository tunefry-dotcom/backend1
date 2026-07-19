"""Subscription persistence + read helpers.

Source of truth
---------------
A user's plan lives in the **public.subscriptions** table (see
``supabase/migrations/0001_subscriptions_and_auth_hook.sql``), written only via the
service-role client. The table owns lifecycle too: ``status`` and ``expires_at``.

Two read paths, by design:
- **Gating (hot path)** reads the plan from the verified JWT claim
  (``app_metadata.plan``), which a Postgres access-token hook fills from this table
  at token-issue time — zero per-request DB calls. See ``plans.plan_from_claims``.
- **Display / detail** (``/billing/me``) reads this table directly for the full
  lifecycle (status, expiry, days remaining).

The DB trigger ``handle_new_user`` auto-creates a Free row for every new user, so
default assignment is a database invariant — no signup path can miss it. This module
only handles *changes* (upgrades) and detailed reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.supabase_client import get_service_client
from app.modules.billing.plans import DEFAULT_PLAN, Plan, coerce_plan

_TABLE = "subscriptions"
_PLAN_TERM_DAYS = 365  # paid plans are annual


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a timestamptz string from Supabase into an aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        # Supabase returns ISO 8601, sometimes with 'Z'.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def get_subscription(user_id: str) -> Optional[dict[str, Any]]:
    """Return the raw subscription row for a user, or None if unavailable.

    Degrades to None on any error (e.g. the migration hasn't been applied yet) so
    callers can fall back to the JWT claim / Free instead of 500ing.
    """
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


def effective_plan(row: Optional[dict[str, Any]]) -> Plan:
    """Resolve the plan a row actually grants right now.

    Mirrors the Postgres access-token hook: a cancelled/expired subscription
    resolves to Free so display and gating agree.
    """
    if not row:
        return DEFAULT_PLAN
    if row.get("status") != "active":
        return DEFAULT_PLAN
    expires = _parse_ts(row.get("expires_at"))
    if expires is not None and expires <= _now():
        return DEFAULT_PLAN
    return coerce_plan(row.get("plan"))


def lifecycle(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Display-friendly lifecycle fields derived from a subscription row.

    Returns ``status``, ``expires_at`` (ISO string) and ``days_remaining`` (>= 0),
    all None when there is no row / no expiry.
    """
    if not row:
        return {"status": None, "expires_at": None, "days_remaining": None}
    expires = _parse_ts(row.get("expires_at"))
    days_remaining: Optional[int] = None
    if expires is not None:
        days_remaining = max(0, (expires - _now()).days)
    return {
        "status": row.get("status"),
        "expires_at": expires.isoformat() if expires else None,
        "days_remaining": days_remaining,
    }


def assign_plan(
    user_id: str,
    plan: Plan,
    *,
    payment_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Upsert the user's subscription to ``plan`` and return the stored row.

    Free is a perpetual, non-expiring plan; paid plans get a one-year term from now.
    Raises on failure so the caller (change-plan) can surface a 400.
    """
    now = _now()
    is_free = plan is Plan.FREE
    record: dict[str, Any] = {
        "user_id": user_id,
        "plan": plan.value,
        "status": "active",
        "started_at": now.isoformat(),
        "expires_at": None if is_free else (now + timedelta(days=_PLAN_TERM_DAYS)).isoformat(),
        "payment_ref": payment_ref,
        "plan_confirmed": True,
    }

    service = get_service_client()
    res = (
        service.table(_TABLE)
        .upsert(record, on_conflict="user_id")
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else record
