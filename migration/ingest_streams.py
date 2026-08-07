"""Ingest legacy MusicStreams -> per-song earnings + artist balances.

Populates the earnings tables created by ``supabase/migrations/0008_earnings.sql``:
  * public.song_stats       — per (song x platform x month) streams + revenue
  * public.artist_balances  — per-user total_earned / total_withdrawn / available

and, as a side effect, marks the matching migrated ``submissions`` row
``approved`` (any song with real stream data went live — this fixes the bug
where migrated songs display as "Declined").

Source of truth is the legacy SQL Server dump (dbo.MusicStreams). Run it again
every month with a fresh dump: the song_stats UNIQUE key makes every write an
**upsert**, so re-running never duplicates rows or double-counts revenue.

Semantics (user-confirmed):
  * IsDeleted = 1 rows are soft-deleted and EXCLUDED from earnings. Only
    IsDeleted = 0 or NULL rows contribute.
  * Revenue is summed at full precision from the Decimal(18,10) Revenue column
    (RedeemedAmount is ignored for totals). We do NOT re-apply plan royalty %
    — MusicStreams.Revenue is already the artist-payable net (INR).
  * The negative-revenue rows on the pseudo-platform ``tunefry`` are manual
    redemption/adjustments: excluded from song_stats and counted as prior
    withdrawn instead.
  * total_withdrawn also includes dbo.WithdrawalHistory rows marked Completed
    and any Supabase withdrawal_requests already ``paid``. Pending Supabase
    requests reduce available_balance so a re-run never resurrects a balance a
    user already asked to withdraw.

Safety: read-only against the dump; upsert/insert-only against Supabase; never
deletes. Always dry-run first:

    python migration/ingest_streams.py "<dump.sql>" --dry-run
    python migration/ingest_streams.py "<dump.sql>"
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

from app.core.supabase_client import get_service_client
# Reuse the battle-tested SQL-dump parser + helpers from the release migration.
from migration.migrate_releases import parse_table, strip_cast

DEFAULT_SQL_FILE = r"C:\Users\ViditVaibhav\Downloads\table with data.sql"
UPSERT_BATCH = 500
ID_CHUNK = 200
ADJUSTMENT_PLATFORM = "tunefry"  # negative rows = manual redemptions

# ---------------------------------------------------------------------------
# Platform normalization: collapse the messy case/spacing variants in the dump
# into a canonical name + a "group". Groups that aren't a recognised major
# distributor fall into "Other" so the UI can club low-stream platforms.
# ---------------------------------------------------------------------------
_PLATFORM_MAP: dict[str, tuple[str, str]] = {
    "spotify": ("Spotify", "Spotify"),
    "applemusic": ("Apple Music", "Apple Music"),
    "apple music": ("Apple Music", "Apple Music"),
    "yt-pdl": ("YouTube", "YouTube"),
    "youtube": ("YouTube", "YouTube"),
    "youtube music": ("YouTube", "YouTube"),
    "youtube ad supported": ("YouTube", "YouTube"),
    "youtube art track": ("YouTube", "YouTube"),
    "youtube ugc": ("YouTube", "YouTube"),
    "facebook": ("Facebook", "Facebook"),
    "facebook audio library": ("Facebook", "Facebook"),
    "facebook fingerprinting": ("Facebook", "Facebook"),
    "instagram": ("Instagram", "Facebook"),
    "amazon": ("Amazon", "Amazon"),
    "amazon prime": ("Amazon", "Amazon"),
    "jiosaavn": ("JioSaavn", "JioSaavn"),
    "gaana": ("Gaana", "Gaana"),
    "tiktok": ("TikTok", "TikTok"),
    "tiktok inc.": ("TikTok", "TikTok"),
    "snap": ("Snap", "Other"),
    "soundcloud": ("SoundCloud", "Other"),
}


def normalize_platform(raw: str | None) -> tuple[str, str]:
    """(canonical_name, group) for a raw platform string. Unknown -> Other."""
    key = re.sub(r"\s+", " ", (raw or "").strip().lower())
    if key in _PLATFORM_MAP:
        return _PLATFORM_MAP[key]
    pretty = re.sub(r"\s+", " ", (raw or "").strip()) or "Unknown"
    return (pretty.title(), "Other")


def norm(s: str | None) -> str:
    """Loose key for matching artist/song across the two legacy tables."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def to_decimal(v: str | None) -> Decimal:
    try:
        return Decimal(strip_cast(v) or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def to_int(v: str | None) -> int:
    try:
        return int(float(strip_cast(v) or "0"))
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Supabase read helpers
# ---------------------------------------------------------------------------
def load_migrated_submissions(svc) -> dict[str, tuple[str, str]]:
    """{ legacy_release_id : (submission_id, user_email) } for migrated rows."""
    out: dict[str, tuple[str, str]] = {}
    offset = 0
    while True:
        res = (svc.table("submissions")
               .select("id, user_email, data, status")
               .like("admin_note", "Migrated from legacy system%")
               .range(offset, offset + 999).execute())
        page = res.data or []
        for r in page:
            rid = str((r.get("data") or {}).get("legacy_release_id") or "").strip()
            if rid:
                out[rid] = (r["id"], (r.get("user_email") or "").lower())
        if len(page) < 1000:
            break
        offset += 1000
    return out


def load_reserved_withdrawals(svc) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """(paid_by_email, pending_by_email) from existing Supabase withdrawal_requests."""
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
            amt = to_decimal(str(r.get("amount")))
            if r.get("status") == "paid":
                paid[email] += amt
            elif r.get("status") == "pending":
                pending[email] += amt
        if len(page) < 1000:
            break
        offset += 1000
    return paid, pending


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest legacy MusicStreams into song_stats + artist_balances")
    parser.add_argument("sql_file", nargs="?", default=DEFAULT_SQL_FILE,
                        help="Path to the legacy SQL Server dump (UTF-16)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute + print everything without touching the DB")
    args = parser.parse_args()
    dry_run: bool = args.dry_run

    print(f"Reading SQL dump (UTF-16, ~200 MB — may take ~30 s) …")
    sql_text = open(args.sql_file, encoding="utf-16").read()

    streams = parse_table(sql_text, "dbo", "MusicStreams",
                          ["ArtistName", "Song", "Streams", "Revenue",
                           "Month", "Year", "Platform", "IsDeleted"])
    releases = parse_table(sql_text, "dbo", "ReleaseDetails",
                           ["ReleaseID", "Song", "SongTitle", "Artist"])
    users = parse_table(sql_text, "dbo", "Users",
                        ["UserID", "Email", "ArtistName"])
    withdrawals = parse_table(sql_text, "dbo", "WithdrawalHistory",
                              ["UserId", "Amount", "Status"])
    print(f"Parsed {len(streams)} stream rows, {len(releases)} releases, "
          f"{len(users)} users, {len(withdrawals)} withdrawals")

    svc = get_service_client()
    rid_to_sub = load_migrated_submissions(svc)     # legacy_release_id -> (sub_id, email)
    print(f"  {len(rid_to_sub)} migrated submissions loaded from Supabase")

    # ---- Build attribution maps -------------------------------------------
    # legacy UserID(int) -> email  and  normalized ArtistName -> email
    legacy_uid_email: dict[int, str] = {}
    artist_to_email: dict[str, str] = {}
    for u in users:
        email = (u.get("Email") or "").strip().lower()
        if not email:
            continue
        try:
            legacy_uid_email[int(float(u["UserID"]))] = email
        except (ValueError, TypeError, KeyError):
            pass
        an = norm(u.get("ArtistName"))
        if an and an not in artist_to_email:
            artist_to_email[an] = email

    # (norm artist, norm song|title) -> ReleaseID
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
        """(user_email, submission_id) — submission_id None on artist-only match."""
        rid = rd_key_to_rid.get((norm(artist), norm(song)))
        if rid and rid in rid_to_sub:
            sub_id, email = rid_to_sub[rid]
            return email, sub_id
        email = artist_to_email.get(norm(artist))
        return (email or None), None

    # ---- Aggregate stream rows --------------------------------------------
    # key -> mutable accumulator
    agg: dict[tuple, dict] = {}
    withdrawn_adj: dict[str, Decimal] = defaultdict(Decimal)  # tunefry negative adjustments
    matched = unmatched = excluded_deleted = adjustments = 0

    for row in streams:
        if row.get("IsDeleted") == "1":            # soft-deleted -> not earnings
            excluded_deleted += 1
            continue
        artist = row.get("ArtistName") or ""
        song = row.get("Song") or ""
        email, sub_id = attribute(artist, song)
        if not email:
            unmatched += 1
            continue
        matched += 1

        platform_raw = row.get("Platform") or ""
        revenue = to_decimal(row.get("Revenue"))

        # tunefry adjustment rows (redemptions) -> withdrawn, not song_stats
        if norm(platform_raw) == ADJUSTMENT_PLATFORM:
            withdrawn_adj[email] += -revenue if revenue < 0 else Decimal("0")
            adjustments += 1
            continue

        canonical, group = normalize_platform(platform_raw)
        month = (row.get("Month") or "").strip()
        year = to_int(row.get("Year"))
        key = (email, song.strip(), canonical, month, year)
        acc = agg.get(key)
        if acc is None:
            acc = {
                "user_email": email,
                "submission_id": sub_id,
                "artist_name": artist.strip(),
                "song_title": song.strip(),
                "platform": canonical,
                "platform_group": group,
                "period_month": month,
                "period_year": year,
                "streams": 0,
                "revenue": Decimal("0"),
            }
            agg[key] = acc
        if sub_id and not acc["submission_id"]:
            acc["submission_id"] = sub_id
        acc["streams"] += to_int(row.get("Streams"))
        acc["revenue"] += revenue

    print(f"  matched={matched}  unmatched={unmatched}  "
          f"excluded(IsDeleted=1)={excluded_deleted}  adjustments={adjustments}")
    print(f"  -> {len(agg)} song_stats rows after aggregation")

    # ---- Compute balances -------------------------------------------------
    total_earned: dict[str, Decimal] = defaultdict(Decimal)
    for acc in agg.values():
        total_earned[acc["user_email"]] += acc["revenue"]

    total_withdrawn: dict[str, Decimal] = defaultdict(Decimal)
    for email, amt in withdrawn_adj.items():
        total_withdrawn[email] += amt
    for w in withdrawals:
        if "complet" in (w.get("Status") or "").lower():
            try:
                email = legacy_uid_email.get(int(float(w["UserId"])))
            except (ValueError, TypeError, KeyError):
                email = None
            if email:
                total_withdrawn[email] += to_decimal(w.get("Amount"))

    paid, pending = load_reserved_withdrawals(svc)
    for email, amt in paid.items():
        total_withdrawn[email] += amt

    emails = set(total_earned) | set(total_withdrawn) | set(pending)
    balances: list[dict] = []
    for email in emails:
        earned = total_earned.get(email, Decimal("0"))
        withdrawn = total_withdrawn.get(email, Decimal("0"))
        avail = earned - withdrawn - pending.get(email, Decimal("0"))
        if avail < 0:
            avail = Decimal("0")
        balances.append({
            "user_email": email,
            "total_earned": str(earned),
            "total_withdrawn": str(withdrawn),
            "available_balance": str(avail),
        })

    # Submissions to flip to approved (have stream data, not already approved).
    approve_ids = {acc["submission_id"] for acc in agg.values() if acc["submission_id"]}

    # ---- Write (or print) -------------------------------------------------
    tag = "[DRY RUN] " if dry_run else ""
    print(f"\n{tag}Will upsert {len(agg)} song_stats, {len(balances)} balances, "
          f"re-approve up to {len(approve_ids)} submissions.")

    # sample for eyeballing
    top = sorted(balances, key=lambda b: Decimal(b["available_balance"]), reverse=True)[:8]
    print("  Top available balances:")
    for b in top:
        print(f"    {b['user_email']:<40} earned={b['total_earned']:<18} "
              f"withdrawn={b['total_withdrawn']:<14} available={b['available_balance']}")

    if dry_run:
        print("\nDRY RUN — nothing written.")
        return

    # song_stats upsert
    rows = []
    for acc in agg.values():
        rows.append({
            "user_email": acc["user_email"],
            "submission_id": acc["submission_id"],
            "artist_name": acc["artist_name"],
            "song_title": acc["song_title"],
            "platform": acc["platform"],
            "platform_group": acc["platform_group"],
            "period_month": acc["period_month"],
            "period_year": acc["period_year"],
            "streams": acc["streams"],
            "revenue": str(acc["revenue"]),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    for i in range(0, len(rows), UPSERT_BATCH):
        chunk = rows[i:i + UPSERT_BATCH]
        svc.table("song_stats").upsert(
            chunk,
            on_conflict="user_email,song_title,platform,period_month,period_year",
        ).execute()
        print(f"  upserted song_stats {i + len(chunk)}/{len(rows)}")

    # artist_balances upsert
    for b in balances:
        b["last_updated"] = datetime.now(timezone.utc).isoformat()
    for i in range(0, len(balances), UPSERT_BATCH):
        chunk = balances[i:i + UPSERT_BATCH]
        svc.table("artist_balances").upsert(chunk, on_conflict="user_email").execute()
    print(f"  upserted {len(balances)} artist_balances")

    # Flip matched submissions to approved (batched; never demotes).
    ids = list(approve_ids)
    flipped = 0
    for i in range(0, len(ids), ID_CHUNK):
        chunk = ids[i:i + ID_CHUNK]
        res = (svc.table("submissions")
               .update({"status": "approved"})
               .in_("id", chunk).neq("status", "approved").execute())
        flipped += len(res.data or [])
    print(f"  marked {flipped} submissions approved")

    print("\nDone.")


if __name__ == "__main__":
    main()
