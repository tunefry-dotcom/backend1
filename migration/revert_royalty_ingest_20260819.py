"""One-time revert of the 2026-08-19 ingest_royalty_report.py live run.

That run deleted 419 pre-existing song_stats rows for (February, March, April)
2026 and replaced them with 6,315 rows built from a flawed attribution pass.
This script undoes ONLY that run's writes:

  1. Delete every song_stats row for period in
     [(February, 2026), (March, 2026), (April, 2026)] -- regardless of user.
     (The original 419 pre-run rows were already destroyed with no backup,
     so this returns those three periods to "no data", which is the closest
     achievable revert.)
  2. Recompute artist_balances for every user currently in that table, using
     the exact formula ingest_royalty_report.py uses:
         total_earned      = sum(song_stats.revenue) over ALL remaining periods
         total_withdrawn   = withdrawn_baseline.json[email] + sum(paid withdrawals)
         available_balance = max(0, total_earned - total_withdrawn - sum(pending))

No other table is touched. submissions.status and withdrawal_requests are
left exactly as they are.

Dry-run is the default. Pass --live to write.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from dotenv import load_dotenv
load_dotenv()

from app.core.supabase_client import get_service_client
from migration.ingest_royalty_report import (
    load_baseline,
    load_reserved_withdrawals,
    list_existing_balance_emails,
    to_decimal,
)

BAD_PERIODS = [("February", 2026), ("March", 2026), ("April", 2026)]
UPSERT_BATCH = 500
DELETE_CHUNK = 200


def fetch_all_for_periods(svc, periods) -> list[dict]:
    rows: list[dict] = []
    for month, year in periods:
        start = 0
        page = 1000
        while True:
            res = (
                svc.table("song_stats")
                .select("id,user_email,revenue")
                .eq("period_month", month)
                .eq("period_year", year)
                .range(start, start + page - 1)
                .execute()
            )
            batch = res.data or []
            rows.extend(batch)
            if len(batch) < page:
                break
            start += page
    return rows


def fetch_remaining_totals(svc, emails: set[str], exclude_periods) -> dict[str, Decimal]:
    """sum(revenue) per email, excluding BAD_PERIODS, for the given emails."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    emails_list = sorted(emails)
    for i in range(0, len(emails_list), 200):
        chunk = emails_list[i:i + 200]
        offset = 0
        while True:
            res = (
                svc.table("song_stats")
                .select("user_email, period_month, period_year, revenue")
                .in_("user_email", chunk)
                .range(offset, offset + 999)
                .execute()
            )
            page = res.data or []
            for r in page:
                key = (r.get("period_month") or "", int(r.get("period_year") or 0))
                if key in exclude_periods:
                    continue
                email = (r.get("user_email") or "").lower()
                totals[email] += to_decimal(r.get("revenue"))
            if len(page) < 1000:
                break
            offset += 1000
    return totals


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Actually write. Default is dry-run.")
    args = ap.parse_args()
    dry_run = not args.live

    svc = get_service_client()
    baseline = load_baseline()

    print("== Step 1: song_stats rows to delete ==")
    bad_rows = fetch_all_for_periods(svc, BAD_PERIODS)
    total_rev = sum(to_decimal(r.get("revenue")) for r in bad_rows)
    affected_emails = {(r.get("user_email") or "").lower() for r in bad_rows}
    print(f"  periods: {BAD_PERIODS}")
    print(f"  rows to delete: {len(bad_rows)}")
    print(f"  revenue being removed: Rs.{total_rev:.2f}")
    print(f"  distinct users affected: {len(affected_emails)}")

    print("\n== Step 2: recompute artist_balances for ALL existing balance users ==")
    existing_emails = list_existing_balance_emails(svc)
    all_emails = existing_emails | affected_emails
    print(f"  existing artist_balances rows: {len(existing_emails)}")
    print(f"  total emails to recompute: {len(all_emails)}")

    print("  loading current paid/pending withdrawal_requests…")
    paid, pending = load_reserved_withdrawals(svc)

    print("  loading current artist_balances (for before/after diff)…")
    before_balances: dict[str, dict] = {}
    offset = 0
    while True:
        res = (
            svc.table("artist_balances")
            .select("user_email,total_earned,total_withdrawn,available_balance")
            .range(offset, offset + 999)
            .execute()
        )
        page = res.data or []
        for r in page:
            before_balances[(r.get("user_email") or "").lower()] = r
        if len(page) < 1000:
            break
        offset += 1000

    print("  loading remaining song_stats (post-delete) for all affected users…")
    exclude_set = set(BAD_PERIODS)
    remaining_totals = fetch_remaining_totals(svc, all_emails, exclude_set)

    new_balances: list[dict] = []
    deltas: list[tuple[str, Decimal, Decimal]] = []
    for email in sorted(all_emails):
        earned = remaining_totals.get(email, Decimal("0"))
        legacy = baseline.get(email, Decimal("0"))
        paid_now = paid.get(email, Decimal("0"))
        pending_now = pending.get(email, Decimal("0"))
        withdrawn = legacy + paid_now
        avail = earned - withdrawn - pending_now
        if avail < 0:
            avail = Decimal("0")
        new_balances.append({
            "user_email": email,
            "total_earned": str(earned),
            "total_withdrawn": str(withdrawn),
            "available_balance": str(avail),
        })
        before = before_balances.get(email)
        before_earned = to_decimal(before["total_earned"]) if before else Decimal("0")
        deltas.append((email, before_earned, earned))

    print(f"\n  {len(new_balances)} artist_balances rows will be upserted")
    movers = sorted(deltas, key=lambda t: (t[1] - t[2]), reverse=True)[:15]
    print("\n  Top 15 total_earned decreases (email, before, after, delta):")
    for email, before, after in movers:
        print(f"    {email:<40} {str(before):>14} {str(after):>14} {str(after - before):>14}")

    total_earned_before = sum(to_decimal(b.get("total_earned")) for b in before_balances.values())
    total_earned_after = sum(to_decimal(b["total_earned"]) for b in new_balances)
    print(f"\n  sum(total_earned) before: Rs.{total_earned_before:.2f}")
    print(f"  sum(total_earned) after:  Rs.{total_earned_after:.2f}")
    print(f"  delta:                    Rs.{(total_earned_after - total_earned_before):.2f}")

    if dry_run:
        print("\nDRY RUN -- nothing written. Pass --live to write.")
        return

    print("\n== LIVE WRITE PHASE ==")
    emails_list = sorted(affected_emails) if affected_emails else []
    total_deleted = 0
    for month, year in BAD_PERIODS:
        res = svc.table("song_stats").delete().eq("period_month", month).eq("period_year", year).execute()
        total_deleted += len(res.data or [])
    print(f"  deleted {total_deleted} song_stats rows for periods {BAD_PERIODS}")

    now = datetime.now(timezone.utc).isoformat()
    for b in new_balances:
        b["last_updated"] = now
    for i in range(0, len(new_balances), UPSERT_BATCH):
        chunk = new_balances[i:i + UPSERT_BATCH]
        svc.table("artist_balances").upsert(chunk, on_conflict="user_email").execute()
    print(f"  upserted {len(new_balances)} artist_balances rows")
    print("\nDone.")


if __name__ == "__main__":
    main()
