"""One-time capture of the legacy component of every artist's total_withdrawn.

Why this exists
---------------
Before the DSP-consolidated Excel report existed, `migration/ingest_streams.py`
computed each artist's ``total_withdrawn`` from three sources:

  (a) ``tunefry``-pseudo-platform adjustments in the legacy SQL dump
  (b) ``dbo.WithdrawalHistory`` rows marked ``Completed``
  (c) Supabase ``withdrawal_requests`` with ``status='paid'`` at that time

The new monthly ingest (``ingest_royalty_report.py``) has access to (c) only.
To preserve (a) + (b) forever, we take a single snapshot::

    baseline[email] = artist_balances.total_withdrawn
                    - SUM(paid requests already reflected in the balance)

and commit it to git as ``withdrawn_baseline.json``. Every future ingest reads
this file and uses::

    total_withdrawn = baseline[email] + SUM(paid requests today)

Correctness — the "already reflected" set
------------------------------------------
A paid withdrawal request is already reflected in
``artist_balances.total_withdrawn`` iff it was ``status='paid'`` at the time
``artist_balances.last_updated`` was set (the last ``ingest_streams.py`` run).
We approximate this with::

    processed_at <= last_updated + 1 minute

The 1-minute cushion absorbs the tiny gap between ``processed_at`` being set
by admin and the subsequent ``ingest_streams.py`` write; it's much smaller
than the days-to-months gap between "just paid" and "counted in a later run".

If a paid request has ``processed_at IS NULL`` (legacy admin flow used to skip
setting it) we assume it's already counted — same behaviour as the old script,
which loaded every paid row unconditionally.

Safety
------
Read-only against Supabase. Writes only one local file. Refuses to overwrite
an existing baseline (that would silently mutate the audit trail) — pass
``--force`` if you have a specific reason.

Usage
-----
    python migration/compute_withdrawn_baseline.py                  # writes json
    python migration/compute_withdrawn_baseline.py --dry-run        # prints only
    python migration/compute_withdrawn_baseline.py --force          # overwrite
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

from app.core.supabase_client import get_service_client


BASELINE_PATH = Path("migration/withdrawn_baseline.json")
CUSHION = timedelta(minutes=1)


def _to_decimal(v) -> Decimal:
    try:
        return Decimal(str(v or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parse_ts(v: str | None) -> datetime | None:
    """Parse ISO-8601 timestamp; return None on missing/bad input."""
    if not v:
        return None
    try:
        # Supabase serializes with a trailing 'Z' or '+00:00' — fromisoformat
        # accepts both once we normalize.
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _paginate(svc, table: str, columns: str, page: int = 1000) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        try:
            res = svc.table(table).select(columns).range(offset, offset + page - 1).execute()
        except Exception as e:
            print(f"  {table}: {e} — treating as empty", file=sys.stderr)
            break
        rows = res.data or []
        out.extend(rows)
        if len(rows) < page:
            break
        offset += page
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the computed baseline; do not write the JSON file")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite an existing withdrawn_baseline.json")
    args = ap.parse_args()

    if BASELINE_PATH.exists() and not args.force and not args.dry_run:
        print(f"ERROR: {BASELINE_PATH} already exists. Pass --force to overwrite.",
              file=sys.stderr)
        sys.exit(2)

    svc = get_service_client()

    print("Reading artist_balances (read-only)…")
    balances = _paginate(svc, "artist_balances",
                         "user_email, total_withdrawn, last_updated")
    print(f"  {len(balances)} artist rows")

    print("Reading withdrawal_requests (read-only)…")
    reqs = _paginate(svc, "withdrawal_requests",
                     "user_email, amount, status, processed_at")
    print(f"  {len(reqs)} withdrawal-request rows")

    # Group paid requests by email, preserving processed_at for cutoff.
    paid_by_email: dict[str, list[tuple[Decimal, datetime | None]]] = defaultdict(list)
    for r in reqs:
        if (r.get("status") or "").lower() != "paid":
            continue
        email = (r.get("user_email") or "").lower()
        if not email:
            continue
        paid_by_email[email].append(
            (_to_decimal(r.get("amount")), _parse_ts(r.get("processed_at")))
        )

    baseline: dict[str, str] = {}
    zero_rows = 0
    deficits: list[tuple[str, Decimal]] = []
    for b in balances:
        email = (b.get("user_email") or "").lower()
        if not email:
            continue
        total_wd = _to_decimal(b.get("total_withdrawn"))
        last_updated = _parse_ts(b.get("last_updated"))
        cutoff = (last_updated + CUSHION) if last_updated else None

        # Only subtract paid requests that were already reflected in total_wd:
        #   processed_at IS NULL      -> assume counted (legacy admin flow)
        #   processed_at <= cutoff    -> counted
        #   processed_at >  cutoff    -> paid AFTER the balance was last computed,
        #                                so it's NOT part of the legacy portion.
        already_counted = Decimal("0")
        for amt, processed_at in paid_by_email.get(email, []):
            if processed_at is None or cutoff is None or processed_at <= cutoff:
                already_counted += amt

        legacy = total_wd - already_counted
        if legacy < 0:
            # Anomaly: total_withdrawn is smaller than the paid we thought was
            # already counted. Should never happen in normal operation. Clamp
            # to 0 and report so a human can investigate.
            deficits.append((email, legacy))
            legacy = Decimal("0")
        if legacy == 0:
            zero_rows += 1
        baseline[email] = str(legacy)

    print("\nSummary")
    print(f"  Users with legacy_withdrawn > 0 : {len(baseline) - zero_rows}")
    print(f"  Users with legacy_withdrawn = 0 : {zero_rows}")
    print(f"  Sum of legacy_withdrawn         : "
          f"{sum(Decimal(v) for v in baseline.values())}")
    if deficits:
        print(f"  WARNING: {len(deficits)} users had already_counted > total_withdrawn")
        print(f"           (clamped to 0 -- review before writing):")
        for email, val in deficits[:10]:
            print(f"     {email:<40} deficit={val}")

    # Top 8 for eyeballing
    top = sorted(baseline.items(), key=lambda kv: Decimal(kv[1]), reverse=True)[:8]
    print("  Top 8 legacy_withdrawn:")
    for email, val in top:
        print(f"    {email:<40} {val}")

    if args.dry_run:
        print("\nDRY RUN — no file written.")
        return

    if deficits and not args.force:
        print(f"\nERROR: {len(deficits)} users show a deficit (see above). "
              f"Investigate manually before writing the baseline. Pass --force "
              f"to write anyway (clamped to 0 for deficit rows).", file=sys.stderr)
        sys.exit(3)

    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps(baseline, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Also write a sidecar snapshot with the source data for audit / debugging.
    audit_path = BASELINE_PATH.with_suffix(".audit.json")
    audit_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "balances_count": len(balances),
                "paid_requests_count": sum(len(v) for v in paid_by_email.values()),
                "deficits": [{"email": e, "deficit": str(v)} for e, v in deficits],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {BASELINE_PATH}  ({len(baseline)} users)")
    print(f"Wrote {audit_path}  (audit sidecar)")


if __name__ == "__main__":
    main()
