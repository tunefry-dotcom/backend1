"""One-time gap-fill: legacy dbo.MusicStreams revenue for February + March 2026
that was never ingested by anything (not the frozen ingest_streams.py, which
never covered these periods, and not ingest_royalty_report.py, which only
starts once the DSP-report pipeline took over).

January 2026 is deliberately EXCLUDED: a prior read-only diagnostic
(migration/_diag_jan2026_overlap.py) confirmed all 195 legacy-attributed
January combos already exist in song_stats with revenue matching to the
decimal (already correctly ingested via the DSP-report pipeline) — touching
January here would risk double-counting real money.

This is INSERT-ONLY. It never deletes or upserts-over an existing song_stats
row: if a (user_email, song_title, platform, period_month, period_year) key
somehow already exists live (defensive re-check at run time, in case state
changed since the diagnostic), that row is skipped and reported, not
overwritten. artist_balances is recomputed ONLY for users who receive new
rows here — no other user's balance is touched.

Dry-run by default. Pass --live to write. Every run (dry-run or live) writes
an unmatched-rows CSV next to this script — legacy rows whose artist/song
could not be attributed to exactly one Supabase email are never guessed.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

from app.core.supabase_client import get_service_client
from migration.migrate_releases import parse_table
from migration.ingest_streams import normalize_platform, norm, to_decimal, to_int

DEFAULT_SQL_FILE = Path(r"C:\Users\ViditVaibhav\Downloads\table with data.sql")
BASELINE_PATH = Path("migration/withdrawn_baseline.json")
TARGET_PERIODS = [("February", 2026), ("March", 2026)]
UPSERT_BATCH = 200


def load_baseline() -> dict[str, Decimal]:
    if not BASELINE_PATH.exists():
        print(f"ERROR: {BASELINE_PATH} missing.", file=sys.stderr)
        sys.exit(2)
    import json
    raw = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {k.lower(): Decimal(str(v)) for k, v in raw.items()}


def load_reserved_withdrawals(svc) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    paid: dict[str, Decimal] = defaultdict(Decimal)
    pending: dict[str, Decimal] = defaultdict(Decimal)
    offset = 0
    while True:
        res = (svc.table("withdrawal_requests")
               .select("user_email, amount, status")
               .range(offset, offset + 999).execute())
        page = res.data or []
        for r in page:
            email = (r.get("user_email") or "").lower()
            amt = to_decimal(str(r.get("amount")) if r.get("amount") is not None else None)
            if r.get("status") == "paid":
                paid[email] += amt
            elif r.get("status") == "pending":
                pending[email] += amt
        if len(page) < 1000:
            break
        offset += 1000
    return paid, pending


def fetch_song_stats_for_periods(svc, periods) -> list[dict]:
    out = []
    offset = 0
    while True:
        res = (svc.table("song_stats")
               .select("user_email, song_title, platform, period_month, period_year, revenue")
               .range(offset, offset + 999).execute())
        page = res.data or []
        out.extend(r for r in page if (r["period_month"], r["period_year"]) in periods)
        if len(page) < 1000:
            break
        offset += 1000
    return out


def fetch_song_stats_for_emails(svc, emails: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for i in range(0, len(emails), 200):
        chunk = emails[i:i + 200]
        offset = 0
        while True:
            res = (svc.table("song_stats")
                   .select("user_email, revenue")
                   .in_("user_email", chunk)
                   .range(offset, offset + 999).execute())
            page = res.data or []
            for r in page:
                out[(r.get("user_email") or "").lower()].append(r)
            if len(page) < 1000:
                break
            offset += 1000
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sql-file", type=Path, default=DEFAULT_SQL_FILE)
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    print(f"Reading SQL dump (UTF-16): {args.sql_file}")
    sql_text = args.sql_file.read_text(encoding="utf-16")

    streams = parse_table(sql_text, "dbo", "MusicStreams",
                           ["ArtistName", "Song", "Streams", "Revenue",
                            "Month", "Year", "Platform", "IsDeleted"])
    releases = parse_table(sql_text, "dbo", "ReleaseDetails",
                            ["ReleaseID", "Song", "SongTitle", "Artist"])
    users = parse_table(sql_text, "dbo", "Users",
                         ["UserID", "Email", "ArtistName", "FullName", "Username"])
    print(f"Parsed {len(streams)} MusicStreams rows, {len(releases)} releases, {len(users)} users")

    svc = get_service_client()

    print("Loading migrated-submissions legacy_release_id map...")
    rid_to_sub: dict[str, tuple[str, str]] = {}
    offset = 0
    while True:
        res = (svc.table("submissions")
               .select("id, user_email, data")
               .like("admin_note", "Migrated from legacy system%")
               .range(offset, offset + 999).execute())
        page = res.data or []
        for r in page:
            rid = str((r.get("data") or {}).get("legacy_release_id") or "").strip()
            if rid:
                rid_to_sub[rid] = (r["id"], (r.get("user_email") or "").lower())
        if len(page) < 1000:
            break
        offset += 1000
    print(f"  {len(rid_to_sub)} migrated submissions indexed")

    name_emails: dict[str, set[str]] = defaultdict(set)
    for u in users:
        email = (u.get("Email") or "").strip().lower()
        if not email:
            continue
        for field in ("ArtistName", "FullName", "Username"):
            n = norm(u.get(field))
            if n:
                name_emails[n].add(email)
    artist_to_email = {name: next(iter(e)) for name, e in name_emails.items() if len(e) == 1}

    rd_key_to_rid: dict[tuple[str, str], str] = {}
    for r in releases:
        rid = (r.get("ReleaseID") or "").strip()
        if not rid:
            continue
        artist = norm(r.get("Artist"))
        for title in {norm(r.get("Song")), norm(r.get("SongTitle"))}:
            if artist and title:
                rd_key_to_rid.setdefault((artist, title), rid)

    def attribute(artist: str, song: str) -> tuple[str | None, str | None]:
        rid = rd_key_to_rid.get((norm(artist), norm(song)))
        if rid and rid in rid_to_sub:
            return rid_to_sub[rid][1], rid_to_sub[rid][0]
        return artist_to_email.get(norm(artist)), None

    agg: dict[tuple, dict] = {}
    unmatched: dict[tuple[str, str], dict] = {}
    adjustment_skipped = 0
    deleted_skipped = 0

    for row in streams:
        if row.get("IsDeleted") == "1":
            deleted_skipped += 1
            continue
        month = (row.get("Month") or "").strip()
        year = to_int(row.get("Year"))
        if (month, year) not in TARGET_PERIODS:
            continue
        if norm(row.get("Platform") or "") == "tunefry":
            adjustment_skipped += 1
            continue
        artist_raw = row.get("ArtistName") or ""
        song_raw = row.get("Song") or ""
        email, sub_id = attribute(artist_raw, song_raw)
        streams_n = to_int(row.get("Streams"))
        revenue_n = to_decimal(row.get("Revenue"))
        if not email:
            key = (artist_raw.strip(), song_raw.strip())
            u = unmatched.setdefault(key, {"row_count": 0, "revenue": Decimal("0"), "streams": 0})
            u["row_count"] += 1
            u["revenue"] += revenue_n
            u["streams"] += streams_n
            continue
        canonical, group = normalize_platform(row.get("Platform") or "")
        key = (email, song_raw.strip(), canonical, month, year)
        acc = agg.setdefault(key, {
            "user_email": email, "submission_id": sub_id, "artist_name": artist_raw,
            "song_title": song_raw.strip(), "platform": canonical, "platform_group": group,
            "period_month": month, "period_year": year,
            "streams": 0, "revenue": Decimal("0"),
        })
        if sub_id and not acc["submission_id"]:
            acc["submission_id"] = sub_id
        acc["streams"] += streams_n
        acc["revenue"] += revenue_n

    print(f"\nParsed target periods {TARGET_PERIODS}: {len(agg)} attributed combos, "
          f"{len(unmatched)} unmatched (artist,song) pairs, "
          f"{deleted_skipped} IsDeleted rows skipped, {adjustment_skipped} 'tunefry' adjustment rows skipped")

    # ---- Defensive live re-check: never overwrite an existing row --------
    print("\nRe-checking live song_stats for the target periods (defensive)...")
    live_rows = fetch_song_stats_for_periods(svc, set(TARGET_PERIODS))
    live_keys = {
        (r["user_email"].lower(), r["song_title"].strip(), r["platform"],
         r["period_month"], r["period_year"])
        for r in live_rows
    }
    print(f"  {len(live_keys)} rows already live in target periods (expected 0)")

    skipped_collisions = [k for k in agg if k in live_keys]
    insert_keys = [k for k in agg if k not in live_keys]
    if skipped_collisions:
        print(f"  WARNING: {len(skipped_collisions)} keys already live — SKIPPING these, "
              f"not overwriting:")
        for k in skipped_collisions[:10]:
            print(f"    {k}")

    insert_rows = [agg[k] for k in insert_keys]
    insert_revenue = sum((r["revenue"] for r in insert_rows), Decimal("0"))
    emails_in_file = sorted({r["user_email"] for r in insert_rows})

    print(f"\nRows to insert: {len(insert_rows)}  revenue={insert_revenue}  "
          f"distinct users={len(emails_in_file)}")
    for month, year in TARGET_PERIODS:
        rows = [r for r in insert_rows if r["period_month"] == month and r["period_year"] == year]
        rev = sum((r["revenue"] for r in rows), Decimal("0"))
        print(f"  {month} {year}: {len(rows)} rows, revenue={rev}")

    # ---- Balance recompute, scoped ONLY to emails_in_file -----------------
    print("\nRecomputing artist_balances for affected users only...")
    baseline = load_baseline()
    paid, pending = load_reserved_withdrawals(svc)
    existing_by_email = fetch_song_stats_for_emails(svc, emails_in_file)

    new_revenue_by_email: dict[str, Decimal] = defaultdict(Decimal)
    for r in insert_rows:
        new_revenue_by_email[r["user_email"]] += r["revenue"]

    balances = []
    movers = []
    for email in emails_in_file:
        before = sum((to_decimal(str(r.get("revenue"))) for r in existing_by_email.get(email, [])), Decimal("0"))
        after = before + new_revenue_by_email[email]
        withdrawn = baseline.get(email, Decimal("0")) + paid.get(email, Decimal("0"))
        avail = after - withdrawn - pending.get(email, Decimal("0"))
        if avail < 0:
            avail = Decimal("0")
        balances.append({
            "user_email": email,
            "total_earned": str(after),
            "total_withdrawn": str(withdrawn),
            "available_balance": str(avail),
        })
        movers.append((email, before, after))

    movers.sort(key=lambda t: (t[2] - t[1]), reverse=True)
    print(f"\nTop 10 total_earned deltas (of {len(movers)} affected users):")
    print(f"  {'email':<40} {'before':>14} {'after':>14} {'delta':>14}")
    for email, before, after in movers[:10]:
        print(f"  {email:<40} {str(before):>14} {str(after):>14} {str(after - before):>14}")

    approve_ids = sorted({r["submission_id"] for r in insert_rows if r["submission_id"]})

    # ---- Unmatched CSV (always written) -----------------------------------
    csv_path = Path("migration/unmatched_legacy_feb_mar_2026.csv")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["artist", "song", "row_count", "total_revenue", "total_streams"])
        for (artist, song), u in sorted(unmatched.items(), key=lambda kv: kv[1]["revenue"], reverse=True):
            w.writerow([artist, song, u["row_count"], str(u["revenue"]), u["streams"]])
    unmatched_revenue = sum((u["revenue"] for u in unmatched.values()), Decimal("0"))
    print(f"\nUnmatched -> {csv_path}  ({len(unmatched)} distinct (artist,song) pairs, "
          f"revenue={unmatched_revenue})")

    print(f"\nPlan:")
    print(f"  * insert {len(insert_rows)} new song_stats rows ({insert_revenue} revenue)")
    print(f"  * upsert artist_balances for {len(balances)} users (ONLY these — no one else touched)")
    print(f"  * approve up to {len(approve_ids)} submissions (only if currently != approved)")
    print(f"  * January 2026 untouched (already correct); no deletes anywhere")

    if not args.live:
        print("\nDRY RUN -- nothing written. Pass --live to write.")
        return

    print("\n== LIVE WRITE PHASE ==")
    now = datetime.now(timezone.utc).isoformat()

    to_write = []
    for r in insert_rows:
        to_write.append({
            "user_email": r["user_email"],
            "submission_id": r["submission_id"],
            "artist_name": r["artist_name"],
            "song_title": r["song_title"],
            "platform": r["platform"],
            "platform_group": r["platform_group"],
            "period_month": r["period_month"],
            "period_year": r["period_year"],
            "streams": r["streams"],
            "revenue": str(r["revenue"]),
            "updated_at": now,
        })

    inserted = 0
    for i in range(0, len(to_write), UPSERT_BATCH):
        chunk = to_write[i:i + UPSERT_BATCH]
        svc.table("song_stats").insert(chunk).execute()
        inserted += len(chunk)
        print(f"  inserted song_stats {inserted}/{len(to_write)}")

    for b in balances:
        b["last_updated"] = now
    for i in range(0, len(balances), UPSERT_BATCH):
        chunk = balances[i:i + UPSERT_BATCH]
        svc.table("artist_balances").upsert(chunk, on_conflict="user_email").execute()
    print(f"  upserted {len(balances)} artist_balances")

    flipped = 0
    for i in range(0, len(approve_ids), UPSERT_BATCH):
        chunk = approve_ids[i:i + UPSERT_BATCH]
        res = (svc.table("submissions").update({"status": "approved"})
               .in_("id", chunk).neq("status", "approved").execute())
        flipped += len(res.data or [])
    print(f"  marked {flipped} submissions approved")

    print("\nDone.")


if __name__ == "__main__":
    main()
