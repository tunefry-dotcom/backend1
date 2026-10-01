"""Razorpay payment helpers for Tunefry Daily publish-credit packs.

Deliberately NOT shared with billing/payment.py — bound to (user_id, pack)
notes rather than (user_id, Plan), and billing's money-critical code is left
untouched. Same httpx + HMAC-SHA256 verification pattern.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any

import httpx

from app.core.config import settings

_RAZORPAY_BASE = "https://api.razorpay.com/v1"

# Fixed INR price + credits granted per pack.
_PACKS: dict[str, dict[str, int]] = {
    "single": {"price_inr": 49, "credits": 1},
    "bundle_20": {"price_inr": 799, "credits": 20},
}


class PaymentError(Exception):
    """Raised for any payment/config/verification failure."""


def _check_enabled() -> tuple[str, str]:
    if not settings.razorpay_enabled:
        raise PaymentError("Razorpay is not configured (missing key id/secret).")
    return (settings.razorpay_key_id, settings.razorpay_key_secret)


def pack_credits(pack: str) -> int:
    return _PACKS[pack]["credits"]


def amount_paise(pack: str) -> int:
    price_inr = _PACKS[pack]["price_inr"]
    divisor = max(1, settings.payment_amount_divisor)
    return (price_inr * 100) // divisor


def create_order(user_id: str, pack: str) -> dict[str, Any]:
    auth = _check_enabled()
    amount = amount_paise(pack)

    try:
        resp = httpx.post(
            f"{_RAZORPAY_BASE}/orders",
            auth=auth,
            json={
                "amount": amount,
                "currency": "INR",
                "receipt": f"tfblog_{pack}_{uuid.uuid4().hex[:12]}",
                "notes": {"user_id": user_id, "pack": pack},
            },
            timeout=10,
        )
        resp.raise_for_status()
        order = resp.json()
    except httpx.HTTPStatusError as exc:
        raise PaymentError(f"Could not create order: {exc.response.text}") from exc
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(f"Could not create order: {exc}") from exc

    return {
        "order_id": order["id"],
        "amount": amount,
        "currency": "INR",
        "key_id": settings.razorpay_key_id,
        "pack": pack,
        "credits": pack_credits(pack),
    }


def verify_payment(
    *,
    user_id: str,
    pack: str,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> None:
    auth = _check_enabled()

    body = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected = hmac.new(auth[1].encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, razorpay_signature):
        raise PaymentError("Payment signature verification failed.")

    try:
        resp = httpx.get(f"{_RAZORPAY_BASE}/orders/{razorpay_order_id}", auth=auth, timeout=10)
        resp.raise_for_status()
        order = resp.json()
    except httpx.HTTPStatusError as exc:
        raise PaymentError(f"Could not fetch order: {exc.response.text}") from exc
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(f"Could not fetch order: {exc}") from exc

    notes = order.get("notes") or {}
    if notes.get("user_id") != user_id or notes.get("pack") != pack:
        raise PaymentError("Order does not match the authenticated user or pack.")
    if order.get("status") != "paid":
        raise PaymentError("Order is not marked paid.")
