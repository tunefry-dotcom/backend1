"""Earnings + withdrawal persistence and aggregation.

Reads the earnings tables from migration 0008 (song_stats, artist_balances,
withdrawal_requests) via the service-role client. Money is stored at full
precision (NUMERIC(20,10)); API responses round to 2 dp for display.

Balances are pre-rolled by ``migration/ingest_streams.py`` — these helpers never
run a live SUM over raw streams, only over a single artist's own rows.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.core.supabase_client import get_service_client

MIN_WITHDRAWAL = Decimal("1500")

# Chronological order for the monthly trend (legacy Month is a name string).
_MONTH_ORDER = {
    m: i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)
}


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v)) if v is not None else Decimal("0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _money(d: Decimal) -> float:
    """Round a Decimal to 2 dp for JSON display."""
    return float(d.quantize(Decimal("0.01")))


def _fetch_song_stats(email: str) -> list[dict[str, Any]]:
    """All song_stats rows for one artist (paginated; a single artist is small)."""
    svc = get_service_client()
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        resp = (
            svc.table("song_stats")
            .select("submission_id,song_title,artist_name,platform,platform_group,"
                    "period_month,period_year,streams,revenue")
            .eq("user_email", email)
            .range(start, start + 999)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
        start += 1000
    return rows


def _sum_referral_earnings(svc: Any, email: str) -> Decimal:
    """Sum public.referral_earnings for this email (0 if the table doesn't exist yet)."""
    try:
        res = (
            svc.table("referral_earnings")
            .select("amount")
            .eq("referrer_email", email)
            .execute()
        )
        return sum((_dec(r["amount"]) for r in (res.data or [])), Decimal("0"))
    except Exception:
        return Decimal("0")


def recompute_balance(email: str) -> dict[str, Any]:
    """Recompute artist_balances from scratch after any song_stats/referral mutation.

    Sums total_earned from all song_stats rows plus any referral commissions
    credited to this email (migration 0010), preserves total_withdrawn (encodes
    withdrawn_baseline.json + paid requests — cannot be re-derived here), then
    subtracts pending withdrawal requests for available_balance.
    """
    svc = get_service_client()

    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        batch = (
            svc.table("song_stats")
            .select("revenue")
            .eq("user_email", email)
            .range(start, start + 999)
            .execute()
        )
        data = batch.data or []
        rows.extend(data)
        if len(data) < 1000:
            break
        start += 1000

    total_earned = sum((_dec(r["revenue"]) for r in rows), Decimal("0")) + _sum_referral_earnings(svc, email)

    bal = (
        svc.table("artist_balances")
        .select("total_withdrawn")
        .eq("user_email", email)
        .maybe_single()
        .execute()
    )
    total_withdrawn = _dec(bal.data["total_withdrawn"]) if bal.data else Decimal("0")

    pending_res = (
        svc.table("withdrawal_requests")
        .select("amount")
        .eq("user_email", email)
        .eq("status", "pending")
        .execute()
    )
    pending = sum(_dec(r["amount"]) for r in (pending_res.data or []))

    avail = max(Decimal("0"), total_earned - total_withdrawn - pending)

    svc.table("artist_balances").upsert(
        {
            "user_email": email,
            "total_earned": str(total_earned),
            "total_withdrawn": str(total_withdrawn),
            "available_balance": str(avail),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="user_email",
    ).execute()

    return {
        "total_earned": _money(total_earned),
        "total_withdrawn": _money(total_withdrawn),
        "available_balance": _money(avail),
    }


def get_balance(email: str) -> dict[str, Any]:
    """Return the artist's balance row (defaults to zeros if none / table absent)."""
    default = {"total_earned": Decimal("0"), "total_withdrawn": Decimal("0"),
               "available_balance": Decimal("0")}
    try:
        svc = get_service_client()
        res = (svc.table("artist_balances").select("*")
               .eq("user_email", email).limit(1).execute())
        row = (res.data or [None])[0]
    except Exception:
        row = None
    if row:
        default = {
            "total_earned": _dec(row.get("total_earned")),
            "total_withdrawn": _dec(row.get("total_withdrawn")),
            "available_balance": _dec(row.get("available_balance")),
        }
    avail = default["available_balance"]
    return {
        "total_earned": _money(default["total_earned"]),
        "total_withdrawn": _money(default["total_withdrawn"]),
        "available_balance": _money(avail),
        "min_withdrawal": float(MIN_WITHDRAWAL),
        "eligible": avail >= MIN_WITHDRAWAL,
    }


def get_earnings_summary(email: str) -> dict[str, Any]:
    """Totals + per-song rollup for the Stats page."""
    try:
        rows = _fetch_song_stats(email)
    except Exception:
        rows = []

    songs: dict[tuple, dict] = {}
    total_streams = 0
    total_revenue = Decimal("0")
    by_month: dict[tuple, dict] = {}
    by_platform: dict[str, dict] = {}
    for r in rows:
        streams = int(r.get("streams") or 0)
        revenue = _dec(r.get("revenue"))
        total_streams += streams
        total_revenue += revenue

        key = (r.get("submission_id"), r.get("song_title"))
        s = songs.get(key)
        if s is None:
            s = {
                "submission_id": r.get("submission_id"),
                "song_title": r.get("song_title"),
                "artist_name": r.get("artist_name"),
                "streams": 0,
                "revenue": Decimal("0"),
                "_by_platform": defaultdict(int),
            }
            songs[key] = s
        s["streams"] += streams
        s["revenue"] += revenue
        s["_by_platform"][r.get("platform_group") or "Other"] += streams

        mk = (r.get("period_year") or 0, r.get("period_month") or "")
        mo = by_month.setdefault(mk, {
            "month": r.get("period_month"), "year": r.get("period_year"),
            "streams": 0, "revenue": Decimal("0"),
        })
        mo["streams"] += streams
        mo["revenue"] += revenue

        g = r.get("platform_group") or "Other"
        pg = by_platform.setdefault(g, {"platform_group": g, "streams": 0, "revenue": Decimal("0")})
        pg["streams"] += streams
        pg["revenue"] += revenue

    song_list = []
    for s in songs.values():
        top = max(s["_by_platform"].items(), key=lambda kv: kv[1])[0] if s["_by_platform"] else None
        song_list.append({
            "submission_id": s["submission_id"],
            "song_title": s["song_title"],
            "artist_name": s["artist_name"],
            "streams": s["streams"],
            "revenue": _money(s["revenue"]),
            "top_platform": top,
        })
    song_list.sort(key=lambda x: x["streams"], reverse=True)

    monthly = sorted(
        [{"month": m["month"], "year": m["year"],
          "streams": m["streams"], "revenue": _money(m["revenue"])}
         for m in by_month.values()],
        key=lambda x: (x["year"] or 0, _MONTH_ORDER.get(x["month"], 0)),
    )
    platforms = sorted(
        [{"platform_group": g["platform_group"], "streams": g["streams"],
          "revenue": _money(g["revenue"])}
         for g in by_platform.values()],
        key=lambda x: x["streams"], reverse=True,
    )

    balance = get_balance(email)
    return {
        "total_streams": total_streams,
        "total_revenue": _money(total_revenue),
        "available_balance": balance["available_balance"],
        "monthly": monthly,
        "platforms": platforms,
        "songs": song_list,
    }


def get_song_detail(email: str, submission_id: str) -> dict[str, Any]:
    """Platform-group breakdown + monthly trend for one release (by submission_id)."""
    svc = get_service_client()
    try:
        resp = (svc.table("song_stats")
                .select("platform_group,period_month,period_year,streams,revenue,song_title")
                .eq("user_email", email).eq("submission_id", submission_id)
                .execute())
        rows = resp.data or []
    except Exception:
        rows = []

    by_group: dict[str, dict] = {}
    by_month: dict[tuple, dict] = {}
    song_title: Optional[str] = None
    for r in rows:
        song_title = song_title or r.get("song_title")
        streams = int(r.get("streams") or 0)
        revenue = _dec(r.get("revenue"))

        g = r.get("platform_group") or "Other"
        grp = by_group.setdefault(g, {"platform_group": g, "streams": 0, "revenue": Decimal("0")})
        grp["streams"] += streams
        grp["revenue"] += revenue

        mk = (r.get("period_year") or 0, r.get("period_month") or "")
        mo = by_month.setdefault(mk, {
            "month": r.get("period_month"), "year": r.get("period_year"),
            "streams": 0, "revenue": Decimal("0")})
        mo["streams"] += streams
        mo["revenue"] += revenue

    platforms = sorted(
        ({"platform_group": g["platform_group"], "streams": g["streams"],
          "revenue": _money(g["revenue"])} for g in by_group.values()),
        key=lambda x: x["streams"], reverse=True)
    monthly = sorted(
        ({"month": m["month"], "year": m["year"], "streams": m["streams"],
          "revenue": _money(m["revenue"])} for m in by_month.values()),
        key=lambda x: (x["year"] or 0, _MONTH_ORDER.get(x["month"], 0)))

    return {"song_title": song_title, "platforms": platforms, "monthly": monthly}


def list_my_withdrawals(email: str) -> list[dict[str, Any]]:
    svc = get_service_client()
    try:
        resp = (svc.table("withdrawal_requests")
                .select("id,amount,status,method,requested_at,processed_at")
                .eq("user_email", email)
                .order("requested_at", desc=True).execute())
        return resp.data or []
    except Exception:
        return []


class WithdrawalError(ValueError):
    """Raised when a withdrawal cannot be created (e.g. below the minimum)."""


def create_withdrawal(
    *, email: str, user_id: str, method: str,
    payout_details: dict[str, Any], snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Insert a pending request for the FULL available balance, then zero it.

    The amount is re-read server-side from artist_balances (never trusted from
    the client) and must be >= MIN_WITHDRAWAL. Raises WithdrawalError otherwise.
    """
    svc = get_service_client()
    res0 = (svc.table("artist_balances").select("available_balance")
            .eq("user_email", email).limit(1).execute())
    amount = _dec((res0.data or [{}])[0].get("available_balance"))
    if amount < MIN_WITHDRAWAL:
        raise WithdrawalError(
            f"Available balance ₹{_money(amount)} is below the minimum "
            f"withdrawal of ₹{float(MIN_WITHDRAWAL)}.")

    row = {
        "user_id": user_id,
        "user_email": email,
        "amount": str(amount),
        "status": "pending",
        "method": method,
        "payout_details": payout_details,
        "snapshot": snapshot,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    res = svc.table("withdrawal_requests").insert(row).execute()
    created = (res.data or [row])[0]

    # Zero the available balance now that it is reserved by this request.
    svc.table("artist_balances").update(
        {"available_balance": "0", "last_updated": datetime.now(timezone.utc).isoformat()}
    ).eq("user_email", email).execute()
    return created
