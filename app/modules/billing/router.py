"""Billing router — plan catalogue, current entitlements, and plan changes.

Endpoints
---------
- ``GET  /billing/plans``       Public catalogue (all tiers + their feature maps).
- ``GET  /billing/me``          The signed-in user's plan + resolved entitlements.
- ``POST /billing/change-plan`` Move the user to a plan and refresh their session.

Note on ``change-plan``: there is no payment integration yet, so this endpoint is
gated behind ``DEV_AUTH_ENABLED`` (404 when off) exactly like the dev signup route.
It exists so the plan-gating flow can be tested end-to-end today; when checkout is
built the plan change will be driven server-side by a verified payment webhook, and
this route can be removed or locked down.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status

_log = logging.getLogger(__name__)

from app.core.config import settings
from app.core.supabase_client import get_supabase
from app.modules.auth.cookies import set_session_cookies
from app.modules.auth.dependencies import CurrentUser, get_current_user
from app.modules.billing.plans import (
    PLAN_RANK,
    Feature,
    Plan,
    entitlements,
    get_spec,
    min_plan_for,
)
from app.modules.billing.payment import PaymentError, create_order, verify_payment
from app.modules.billing.schemas import (
    ChangePlanRequest,
    CreateOrderRequest,
    MyPlanResponse,
    OrderResponse,
    PlanSummary,
    VerifyPaymentRequest,
)
from app.modules.billing.service import (
    assign_plan,
    effective_plan,
    get_subscription,
    lifecycle,
)
from app.modules.profile import service as profile_service

router = APIRouter(prefix="/billing", tags=["billing"])


def _plan_summary(plan: Plan) -> PlanSummary:
    spec = get_spec(plan)
    return PlanSummary(
        id=plan,
        name=spec.name,
        price_inr=spec.price_inr,
        royalty_pct=spec.royalty_pct,
        max_releases=spec.max_releases,
        max_artists=spec.max_artists,
        rank=PLAN_RANK[plan],
        features={f.value: (f in spec.features) for f in Feature},
    )


def _my_plan_response(
    plan: Plan,
    row: dict | None = None,
) -> MyPlanResponse:
    """Build the entitlement + lifecycle payload (shared by /me and /change-plan)."""
    spec = get_spec(plan)
    life = lifecycle(row)
    return MyPlanResponse(
        plan=plan,
        name=spec.name,
        royalty_pct=spec.royalty_pct,
        max_releases=spec.max_releases,
        max_artists=spec.max_artists,
        entitlements={f.value: granted for f, granted in entitlements(plan).items()},
        is_free=(plan is Plan.FREE),
        upgrade_hints={
            f.value: (min_plan_for(f).value if min_plan_for(f) else None)
            for f in Feature
            if f not in spec.features
        },
        status=life["status"],
        expires_at=life["expires_at"],
        days_remaining=life["days_remaining"],
    )


@router.get("/plans", response_model=list[PlanSummary])
async def list_plans() -> list[PlanSummary]:
    """Return the full plan catalogue, ordered by rank (free → label).

    The frontend fetches this so the route-gate matrix has a single source of
    truth on the server instead of being hardcoded in two places.
    """
    return [_plan_summary(p) for p in sorted(Plan, key=lambda p: PLAN_RANK[p])]


@router.get("/me", response_model=MyPlanResponse)
async def my_plan(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> MyPlanResponse:
    """Resolve the current user's plan, entitlements and lifecycle.

    The subscriptions table is authoritative for lifecycle (status/expiry), so we
    read it here. If it's unavailable (e.g. migration not yet applied) we fall back
    to the plan carried in the verified JWT.
    """
    row = get_subscription(current_user.id)
    plan = effective_plan(row) if row else current_user.plan
    return _my_plan_response(plan, row)


@router.post("/change-plan", response_model=MyPlanResponse)
async def change_plan(
    body: ChangePlanRequest,
    response: Response,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    refresh_token: str | None = Cookie(default=None, alias=settings.refresh_cookie_name),
) -> MyPlanResponse:
    """Move the user to ``body.plan``, then refresh their session so the new plan
    is reflected in the JWT immediately.

    Placeholder for the post-payment flow — gated behind DEV_AUTH_ENABLED.
    """
    if not settings.dev_auth_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    try:
        row = assign_plan(current_user.id, body.plan)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to change plan: {exc}",
        )

    _refresh_session(response, refresh_token)
    return _my_plan_response(body.plan, row)


def _refresh_session(response: Response, refresh_token: str | None) -> None:
    """Reissue session cookies so the access-token hook re-stamps the new plan.

    Supabase's refresh regenerates the JWT from the live user row, so the hook
    re-reads the subscriptions table and the fresh token carries the new plan. If
    refresh fails, the plan is still persisted — it just takes effect on the next
    natural token refresh.
    """
    if not refresh_token:
        return
    try:
        result = get_supabase().auth.refresh_session(refresh_token)
        if result.session:
            set_session_cookies(
                response,
                result.session.access_token,
                result.session.refresh_token,
            )
    except Exception as exc:
        _log.warning("Session refresh failed after plan change (plan still persisted): %s", exc)


# ---------------------------------------------------------------------------
# Razorpay payment — upgrade a plan after a verified payment.
# ---------------------------------------------------------------------------


@router.post("/orders", response_model=OrderResponse)
async def create_payment_order(
    body: CreateOrderRequest,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> OrderResponse:
    """Create a Razorpay order for an upgrade.

    Guards, in order: profile must be complete (else 403 so the frontend can send
    the user to /profile), and the target must be a real upgrade over the current
    plan. The amount is derived server-side from the plan catalogue.
    """
    # 1. Profile completeness gate — only for Google OAuth users.
    # Email/password users already provided key details at signup.
    if current_user.provider == "google":
        missing = profile_service.missing_required(
            profile_service.get_profile(current_user.id)
        )
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "profile_incomplete", "missing_fields": missing},
            )

    # 2. Must be a paid upgrade over the current plan.
    if body.plan is Plan.FREE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Free plan is not purchasable.")
    if PLAN_RANK[body.plan] <= PLAN_RANK[current_user.plan]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected plan is not an upgrade over your current plan.",
        )

    try:
        order = create_order(current_user.id, body.plan)
    except PaymentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return OrderResponse(**order)


@router.post("/verify-payment", response_model=MyPlanResponse)
async def verify_payment_and_upgrade(
    body: VerifyPaymentRequest,
    response: Response,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    refresh_token: str | None = Cookie(default=None, alias=settings.refresh_cookie_name),
) -> MyPlanResponse:
    """Verify a completed Razorpay payment, then grant the plan.

    The signature is verified with the secret key and the order is bound to this
    user + plan before any upgrade is applied (see billing.payment.verify_payment).
    """
    try:
        verify_payment(
            user_id=current_user.id,
            plan=body.plan,
            razorpay_order_id=body.razorpay_order_id,
            razorpay_payment_id=body.razorpay_payment_id,
            razorpay_signature=body.razorpay_signature,
        )
    except PaymentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # Replay protection: reject if this payment ID was already used to grant a plan.
    existing = get_subscription(current_user.id)
    if existing and existing.get("payment_ref") == body.razorpay_payment_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This payment has already been applied.",
        )

    try:
        row = assign_plan(current_user.id, body.plan, payment_ref=body.razorpay_payment_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payment verified but plan assignment failed: {exc}",
        )

    _refresh_session(response, refresh_token)
    return _my_plan_response(body.plan, row)
