"""Razorpay payment helpers for plan upgrades.

Server-authoritative: the amount is always derived from ``PLAN_SPECS`` here, never
taken from the client, and the payment signature is verified with the secret key
before any plan is granted. The order is tagged with ``notes.user_id`` / ``notes.plan``
so verification can bind the payment to the buyer and the tier they paid for.

``razorpay`` is imported lazily so the app still boots if the package or keys are
absent — the endpoints fail clearly at call time instead.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.config import settings
from app.modules.billing.plans import Plan, get_spec


class PaymentError(Exception):
    """Raised for any payment/config/verification failure."""


def get_razorpay_client() -> Any:
    if not settings.razorpay_enabled:
        raise PaymentError("Razorpay is not configured (missing key id/secret).")
    try:
        import razorpay  # lazy: keeps app import working without the package
    except ImportError as exc:  # pragma: no cover
        raise PaymentError("razorpay package is not installed.") from exc
    return razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))


def amount_paise(plan: Plan) -> int:
    """Charge amount in paise, applying the testing divisor.

    ₹1439 with divisor 100 → 143900 // 100 = 1439 paise = ₹14.39.
    With divisor 1 → 143900 paise = ₹1439 (production).
    """
    price_inr = get_spec(plan).price_inr
    divisor = max(1, settings.payment_amount_divisor)
    return (price_inr * 100) // divisor


def create_order(user_id: str, plan: Plan) -> dict[str, Any]:
    """Create a Razorpay order for ``plan`` and return checkout params."""
    client = get_razorpay_client()
    spec = get_spec(plan)
    amount = amount_paise(plan)

    try:
        order = client.order.create(
            {
                "amount": amount,
                "currency": "INR",
                "receipt": f"tf_{plan.value}_{uuid.uuid4().hex[:12]}",
                "notes": {"user_id": user_id, "plan": plan.value},
            }
        )
    except Exception as exc:
        raise PaymentError(f"Could not create order: {exc}") from exc

    return {
        "order_id": order["id"],
        "amount": amount,
        "currency": "INR",
        "key_id": settings.razorpay_key_id,
        "plan": plan.value,
        "plan_name": spec.name,
    }


def verify_payment(
    user_id: str,
    plan: Plan,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> None:
    """Verify the payment signature AND that the order belongs to this user/plan.

    Raises PaymentError on any mismatch; returns None on success.
    """
    client = get_razorpay_client()

    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_signature": razorpay_signature,
            }
        )
    except Exception as exc:
        raise PaymentError("Payment signature verification failed.") from exc

    # Re-fetch the order and confirm it was created for this user + plan. This stops
    # a caller from presenting a valid payment for a cheaper/other order.
    try:
        order = client.order.fetch(razorpay_order_id)
    except Exception as exc:
        raise PaymentError(f"Could not fetch order: {exc}") from exc

    notes = order.get("notes") or {}
    if notes.get("user_id") != user_id or notes.get("plan") != plan.value:
        raise PaymentError("Order does not match the authenticated user or plan.")
    if order.get("status") != "paid":
        raise PaymentError("Order is not marked paid.")
