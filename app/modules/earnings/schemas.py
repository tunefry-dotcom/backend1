"""Request/response DTOs for the earnings + withdrawal endpoints."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class WithdrawalRequestBody(BaseModel):
    """Artist payout request. Amount is NEVER taken from the client — the server
    derives it from artist_balances (the full available balance)."""

    method: Literal["upi", "bank"]
    # UPI
    upi_id: Optional[str] = None
    # Bank
    account_holder: Optional[str] = None
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    ifsc: Optional[str] = None
