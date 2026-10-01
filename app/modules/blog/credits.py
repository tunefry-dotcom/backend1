"""Publish-credit ledger for Tunefry Daily.

One free article per user for life, then paid credit packs. Read-modify-write
against a single row per user — no distributed locking, same low-concurrency-
tolerant convention already used by earnings.service.recompute_balance.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException, status

from app.core.supabase_client import get_service_client

_log = logging.getLogger(__name__)

_CREDITS_TABLE = "blog_publish_credits"
_PURCHASES_TABLE = "blog_credit_purchases"


def _get_row(email: str) -> Optional[dict[str, Any]]:
    svc = get_service_client()
    res = svc.table(_CREDITS_TABLE).select("*").eq("user_email", email).limit(1).execute()
    rows = res.data or []
    return rows[0] if rows else None


def get_credit_status(email: str) -> dict[str, Any]:
    row = _get_row(email)
    free_used = bool(row.get("free_article_used")) if row else False
    remaining = int(row.get("credits_remaining") or 0) if row else 0
    return {
        "free_article_used": free_used,
        "credits_remaining": remaining,
        "can_publish": (not free_used) or remaining > 0,
    }


def consume_publish_credit(email: str) -> None:
    """Consume one credit — the free article first, then paid credits.

    Raises HTTPException(402) if none are available.
    """
    svc = get_service_client()
    row = _get_row(email)
    free_used = bool(row.get("free_article_used")) if row else False
    remaining = int(row.get("credits_remaining") or 0) if row else 0

    if not free_used:
        svc.table(_CREDITS_TABLE).upsert(
            {
                "user_email": email,
                "free_article_used": True,
                "credits_remaining": remaining,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="user_email",
        ).execute()
        return

    if remaining > 0:
        svc.table(_CREDITS_TABLE).upsert(
            {
                "user_email": email,
                "free_article_used": True,
                "credits_remaining": remaining - 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="user_email",
        ).execute()
        return

    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={"error": "no_publish_credits"},
    )


def grant_credits(email: str, count: int) -> dict[str, Any]:
    svc = get_service_client()
    row = _get_row(email)
    free_used = bool(row.get("free_article_used")) if row else False
    remaining = int(row.get("credits_remaining") or 0) if row else 0
    new_remaining = remaining + count
    svc.table(_CREDITS_TABLE).upsert(
        {
            "user_email": email,
            "free_article_used": free_used,
            "credits_remaining": new_remaining,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="user_email",
    ).execute()
    return {"free_article_used": free_used, "credits_remaining": new_remaining}


def record_purchase_and_grant(
    *,
    email: str,
    pack: str,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    amount_inr: Decimal,
    credits: int,
) -> dict[str, Any]:
    """Insert the immutable purchase row, then grant credits.

    razorpay_payment_id is UNIQUE — a genuine replay hits a unique violation
    here and is rejected before any credit is granted twice.

    If grant_credits fails AFTER the purchase row is committed, the user paid
    but got nothing — logged at ERROR for manual reconciliation (this codebase
    has no ops-alerting integration) and surfaced to the caller as
    credited=False rather than a 5xx, so the frontend can show a
    "contact support" message instead of retrying a payment already made.
    """
    svc = get_service_client()
    try:
        svc.table(_PURCHASES_TABLE).insert(
            {
                "user_email": email,
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "pack": pack,
                "amount_inr": str(amount_inr),
                "credits_granted": credits,
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "duplicate key" in msg or "unique" in msg or "23505" in msg:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This payment has already been applied.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not record purchase: {exc}",
        ) from exc

    try:
        new_status = grant_credits(email, credits)
        return {"credited": True, **new_status}
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "Blog credit purchase recorded but grant_credits failed for %s "
            "(payment_id=%s, pack=%s, credits=%s): %s — payment received, "
            "credits NOT applied, needs manual reconciliation.",
            email, razorpay_payment_id, pack, credits, exc,
        )
        return {
            "credited": False,
            "support_note": (
                "Payment received but credits could not be applied automatically. "
                "Please contact support with your payment ID."
            ),
        }
