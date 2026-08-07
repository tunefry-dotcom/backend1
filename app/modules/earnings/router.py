"""Earnings + withdrawal endpoints (artist-facing).

Reads are scoped to the signed-in user's email. Withdrawals derive their amount
server-side from artist_balances and zero the balance on request.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.modules.auth.dependencies import CurrentUser, get_current_user
from app.modules.billing.plans import get_spec
from app.modules.earnings import service
from app.modules.earnings.schemas import WithdrawalRequestBody
from app.modules.profile import service as profile_service

router = APIRouter(tags=["earnings"])


def _email(user: CurrentUser) -> str:
    email = (user.email or "").lower()
    if not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No email on account")
    return email


def _age_from_dob(dob: Any) -> int | None:
    if not dob:
        return None
    try:
        d = dob if isinstance(dob, date) else datetime.fromisoformat(str(dob)[:10]).date()
    except (ValueError, TypeError):
        return None
    today = date.today()
    return today.year - d.year - ((today.month, today.day) < (d.month, d.day))


@router.get("/earnings/me")
async def my_earnings(user: Annotated[CurrentUser, Depends(get_current_user)]) -> dict:
    return service.get_earnings_summary(_email(user))


@router.get("/earnings/balance")
async def my_balance(user: Annotated[CurrentUser, Depends(get_current_user)]) -> dict:
    return service.get_balance(_email(user))


@router.get("/earnings/songs/{submission_id}")
async def song_earnings(
    submission_id: str, user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    return service.get_song_detail(_email(user), submission_id)


@router.get("/withdrawals/me")
async def my_withdrawals(user: Annotated[CurrentUser, Depends(get_current_user)]) -> list[dict]:
    return service.list_my_withdrawals(_email(user))


@router.post("/withdrawals", status_code=status.HTTP_201_CREATED)
async def request_withdrawal(
    body: WithdrawalRequestBody,
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    email = _email(user)

    # Validate payout details for the chosen method.
    if body.method == "upi":
        if not (body.upi_id and "@" in body.upi_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="A valid UPI ID is required.")
        payout_details = {"upi_id": body.upi_id.strip()}
    else:  # bank
        if not (body.account_holder and body.bank_name and body.account_number and body.ifsc):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="All bank details are required.")
        payout_details = {
            "account_holder": body.account_holder.strip(),
            "bank_name": body.bank_name.strip(),
            "account_number": body.account_number.strip(),
            "ifsc": body.ifsc.strip().upper(),
        }

    # Freeze an artist snapshot for the admin (plan / name / address / age).
    profile = profile_service.get_profile(user.id) or {}
    snapshot = {
        "plan": user.plan.value,
        "plan_name": get_spec(user.plan).name,
        "full_name": profile.get("full_name") or user.full_name,
        "artist_name": profile.get("artist_name") or user.artist_name,
        "phone": profile.get("phone") or user.phone,
        "city": profile.get("city"),
        "state": profile.get("state"),
        "age": _age_from_dob(profile.get("date_of_birth")),
    }

    try:
        created = service.create_withdrawal(
            email=email, user_id=user.id, method=body.method,
            payout_details=payout_details, snapshot=snapshot,
        )
    except service.WithdrawalError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {"ok": True, "request": created}
