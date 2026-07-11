"""Request/response models for the billing API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from app.modules.billing.plans import Plan


class PlanSummary(BaseModel):
    """Public description of a single plan (for the /billing/plans catalogue)."""

    id: Plan
    name: str
    price_inr: int
    royalty_pct: int
    max_releases: Optional[int]  # None => unlimited
    max_artists: int
    rank: int
    features: dict[str, bool]


class MyPlanResponse(BaseModel):
    """The signed-in user's plan + resolved entitlements + lifecycle."""

    plan: Plan
    name: str
    royalty_pct: int
    max_releases: Optional[int]
    max_artists: int
    entitlements: dict[str, bool]
    is_free: bool
    # Features the user does NOT have, each with the cheapest plan that unlocks it.
    upgrade_hints: dict[str, Optional[str]]
    # Lifecycle (from the subscriptions table; null when only the JWT is available).
    status: Optional[str] = None
    expires_at: Optional[str] = None
    days_remaining: Optional[int] = None


class ChangePlanRequest(BaseModel):
    plan: Plan


class CreateOrderRequest(BaseModel):
    plan: Plan


class OrderResponse(BaseModel):
    order_id: str
    amount: int          # paise
    currency: str
    key_id: str          # public Razorpay key for the checkout widget
    plan: Plan
    plan_name: str


class VerifyPaymentRequest(BaseModel):
    plan: Plan
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
