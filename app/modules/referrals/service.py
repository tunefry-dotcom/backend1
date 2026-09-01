"""Referral program: codes, relationships, and commission crediting.

A user's referral code is a deterministic slice of their own UUID — no
randomness, no external calls, nothing to collide-check against. It is
persisted on ``public.profiles.referral_code`` (migration 0010) the first
time it's requested, so existing users get one lazily instead of needing a
backfill script.

Commission crediting writes an immutable row to ``public.referral_earnings``
(the audit trail) and then folds it into the referrer's *existing* wallet by
calling ``earnings.service.recompute_balance`` — referral money is ordinary
``artist_balances.available_balance``, withdrawable through the existing
``/withdrawals`` flow.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from app.core.supabase_client import get_service_client
from app.modules.billing.plans import Plan, get_spec
from app.modules.earnings.service import recompute_balance

_log = logging.getLogger(__name__)

_PROFILES_TABLE = "profiles"
_REFERRALS_TABLE = "referrals"
_EARNINGS_TABLE = "referral_earnings"
_COMMISSION_PCT = Decimal("0.10")


def generate_referral_code(user_id: str) -> str:
    """Pure derivation from the user's own UUID — no I/O, no randomness."""
    return "TF" + user_id.replace("-", "")[:8].upper()


def get_or_create_referral_code(user_id: str) -> str:
    """Return the user's referral code, generating + persisting it on first use."""
    service = get_service_client()
    res = (
        service.table(_PROFILES_TABLE)
        .select("referral_code")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    existing = rows[0].get("referral_code") if rows else None
    if existing:
        return existing

    code = generate_referral_code(user_id)
    service.table(_PROFILES_TABLE).upsert(
        {"user_id": user_id, "referral_code": code}, on_conflict="user_id"
    ).execute()
    return code


def resolve_referrer(code: str) -> Optional[str]:
    """Look up the referrer's user_id for a referral code, or None if unknown."""
    code = (code or "").strip().upper()
    if not code:
        return None
    service = get_service_client()
    try:
        res = (
            service.table(_PROFILES_TABLE)
            .select("user_id")
            .eq("referral_code", code)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0]["user_id"] if rows else None
    except Exception:
        return None


def record_referral(referrer_user_id: str, referred_user_id: str, code: str) -> None:
    """Insert the referral relationship. Best-effort — never raises.

    A bad code, self-referral already filtered by the caller, or a duplicate
    (UNIQUE on referred_user_id — a user can only be referred once) must never
    block signup.
    """
    try:
        service = get_service_client()
        service.table(_REFERRALS_TABLE).insert(
            {
                "referrer_user_id": referrer_user_id,
                "referred_user_id": referred_user_id,
                "referral_code_used": code.strip().upper(),
            }
        ).execute()
    except Exception as exc:
        _log.warning("Could not record referral (%s -> %s): %s", referrer_user_id, referred_user_id, exc)


def _get_referral_row(referred_user_id: str) -> Optional[dict[str, Any]]:
    service = get_service_client()
    try:
        res = (
            service.table(_REFERRALS_TABLE)
            .select("referrer_user_id")
            .eq("referred_user_id", referred_user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def credit_referral(
    *,
    referred_user_id: str,
    plan: Plan,
    source: str,
    payment_ref: Optional[str] = None,
) -> None:
    """Credit 10% of ``plan``'s price to the referrer's wallet, if any.

    No-op when the referred user wasn't referred, or when the plan is free
    (amount would be zero). Best-effort — a failure here must never break the
    plan-activation flow that triggered it, so all exceptions are swallowed
    and logged.
    """
    try:
        row = _get_referral_row(referred_user_id)
        if not row:
            return

        amount = get_spec(plan).price_inr * _COMMISSION_PCT
        if amount <= 0:
            return

        referrer_user_id = row["referrer_user_id"]
        service = get_service_client()
        referrer = service.auth.admin.get_user_by_id(referrer_user_id)
        referrer_email = (referrer.user.email or "").lower() if referrer and referrer.user else ""
        if not referrer_email:
            _log.warning("Could not resolve email for referrer %s; skipping commission", referrer_user_id)
            return

        service.table(_EARNINGS_TABLE).insert(
            {
                "referrer_user_id": referrer_user_id,
                "referrer_email": referrer_email,
                "referred_user_id": referred_user_id,
                "plan": plan.value,
                "amount": str(amount),
                "source": source,
                "payment_ref": payment_ref,
            }
        ).execute()

        recompute_balance(referrer_email)
    except Exception as exc:
        _log.warning(
            "Referral commission crediting failed for referred user %s (plan=%s): %s",
            referred_user_id, plan.value, exc,
        )


def list_my_referrals(user_id: str) -> dict[str, Any]:
    """Referral dashboard payload: own code, referred users, total earned."""
    referral_code = get_or_create_referral_code(user_id)

    service = get_service_client()
    referred_count = 0
    referrals: list[dict[str, Any]] = []
    try:
        res = (
            service.table(_REFERRALS_TABLE)
            .select("referred_user_id,created_at")
            .eq("referrer_user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        rows = res.data or []
        referred_count = len(rows)
        for r in rows:
            referred_id = r["referred_user_id"]
            email = ""
            plan = "free"
            try:
                u = service.auth.admin.get_user_by_id(referred_id)
                email = (u.user.email or "") if u and u.user else ""
            except Exception:
                pass
            try:
                sub = (
                    service.table("subscriptions")
                    .select("plan")
                    .eq("user_id", referred_id)
                    .limit(1)
                    .execute()
                )
                sub_rows = sub.data or []
                if sub_rows:
                    plan = sub_rows[0].get("plan") or "free"
            except Exception:
                pass
            referrals.append({
                "email": email,
                "plan": plan,
                "joined_at": r.get("created_at"),
            })
    except Exception:
        pass

    total_earned = Decimal("0")
    try:
        res = (
            service.table(_EARNINGS_TABLE)
            .select("amount")
            .eq("referrer_user_id", user_id)
            .execute()
        )
        for r in res.data or []:
            total_earned += Decimal(str(r.get("amount") or "0"))
    except Exception:
        pass

    return {
        "referral_code": referral_code,
        "referred_count": referred_count,
        "referrals": referrals,
        "total_referral_earned": float(total_earned.quantize(Decimal("0.01"))),
    }
