"""Read-only stats extractor for one artist from the legacy SQL Server dump.

Why this exists
----------------
Ad-hoc "what did artist X earn / stream / still have to withdraw" questions
against the legacy SSMS export (``table with data.sql``, UTF-16, ~200MB)
otherwise mean hand-rolling the same parsing every time. This script answers
three things in one pass, read-only, never touching Supabase or the dump:

  1. Combined cross-platform totals (streams + revenue) for the artist.
  2. Song-wise breakdown (streams + revenue per song, as stored).
  3. Remaining available balance not yet allocated to a withdrawal request —
     optionally scoped to a single Month/Year.

Two non-obvious things this script accounts for:

- **The join key between ``dbo.Users`` and ``dbo.MusicStreams`` is not fixed.**
  ``MusicStreams.ArtistName`` is free text typed into the old submission form —
  in practice it usually matches ``Users.Username``, but ``Users.ArtistName``
  is frequently NULL, so ``FullName``/``ArtistName`` sometimes match instead.
  We resolve by trying each candidate against the distinct ``ArtistName``
  values that actually exist in ``MusicStreams``.
- **``MusicStreams.RedeemedAmount`` is a per-row withdrawal-allocation marker,
  not just ``round(Revenue, 2)`` restated.** Rows untouched by any withdrawal
  have ``RedeemedAmount = 0``; rows (partially) claimed by a withdrawal request
  carry that claimed portion. So ``SUM(Revenue - RedeemedAmount)`` for a row
  set IS that artist's remaining available balance for that scope — and
  scoping the row set to one Month/Year gives a month-specific remaining
  balance that isn't exposed anywhere else.

Usage
-----
    python migration/legacy_artist_stats.py --artist "Lucky Ben"
    python migration/legacy_artist_stats.py --artist "Lucky Ben" --month March --year 2026
    python migration/legacy_artist_stats.py "<dump.sql>" --artist "Lucky Ben" --json
    python migration/legacy_artist_stats.py --artist "Lucky" --user-id 16392

Read-only: never writes to the dump, Supabase, or anywhere else.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEFAULT_DUMP = r"C:\Users\ViditVaibhav\Downloads\table with data.sql"

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_INDEX = {m.lower(): i for i, m in enumerate(MONTHS)}


# ---------------------------------------------------------------------------
# SQL dump parsing
#
# Deliberately self-contained (not imported from migrate_releases.py) so this
# read-only tool never pulls in app.core.supabase_client / settings, which
# migrate_releases.py needs at import time. The tokeniser below is the same
# approach as migrate_releases.py's, plus a strip_cast() that (unlike that
# module's) correctly unwraps CAST(x AS Decimal(18, 10)) — the original regex
# only handles types with no internal parens (e.g. DateTime).
# ---------------------------------------------------------------------------

def _closing_paren(s: str) -> int:
    """Index of the ) that closes VALUES( — skips nested CAST/func parens."""
    in_str = False
    depth = 0
    i = 0
    while i < len(s):
        c = s[i]
        if not in_str:
            if c == "N" and i + 1 < len(s) and s[i + 1] == "'":
                i += 1
                c = s[i]
            if c == "'":
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    return i
                depth -= 1
        else:
            if c == "'" and i + 1 < len(s) and s[i + 1] == "'":
                i += 2
                continue
            if c == "'":
                in_str = False
        i += 1
    return len(s)


def parse_values(raw: str) -> list[str | None]:
    """Tokenise a SQL VALUES list (without outer parens)."""
    tokens: list[str | None] = []
    buf = ""
    in_str = False
    depth = 0
    i = 0
    while i < len(raw):
        c = raw[i]
        if not in_str:
            if c == "N" and i + 1 < len(raw) and raw[i + 1] == "'":
                i += 1
                c = raw[i]
            if c == "'":
                in_str = True
                i += 1
                continue
            if c == "(":
                depth += 1
            elif c == ")" and depth > 0:
                depth -= 1
            if c == "," and depth == 0:
                v = buf.strip()
                tokens.append(None if v.upper() == "NULL" else v)
                buf = ""
                i += 1
                continue
        else:
            if c == "'" and i + 1 < len(raw) and raw[i + 1] == "'":
                buf += "'"
                i += 2
                continue
            if c == "'":
                in_str = False
                i += 1
                continue
        buf += c
        i += 1

    v = buf.strip()
    tokens.append(None if v.upper() == "NULL" else v)
    return tokens


def iter_insert_statements(lines: list[str], prefix_upper: str):
    """Yield complete INSERT statements for a table prefix (case-insensitive).

    Reassembles rows whose nvarchar(max) fields hold literal newlines (e.g. a
    multi-line artist bio splits one logical row across several physical lines).
    """
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i].strip()
        if line.upper().startswith(prefix_upper):
            stmt = line
            while (stmt.count("'") % 2 != 0) or not stmt.rstrip().endswith(")"):
                i += 1
                if i >= n:
                    break
                stmt += "\n" + lines[i]
            yield stmt
        i += 1


def parse_table(sql_text: str, schema: str, table: str, wanted: list[str]) -> list[dict]:
    """Extract rows from INSERT statements for [schema].[table]."""
    prefix = f"INSERT [{schema}].[{table}]".upper()
    rows: list[dict] = []

    for line in iter_insert_statements(sql_text.splitlines(), prefix):
        try:
            up = line.upper()
            col_open = line.index("(") + 1
            val_kw = up.index(") VALUES")
            col_names = [c.strip().strip("[]") for c in line[col_open:val_kw].split(",")]

            val_open = up.index("VALUES (") + len("VALUES (")
            vals_raw = line[val_open:].rstrip()
            vals_str = vals_raw[:_closing_paren(vals_raw)]

            vals = parse_values(vals_str)
            if len(vals) != len(col_names):
                continue

            row = dict(zip(col_names, vals))
            rows.append({k: row.get(k) for k in wanted})
        except (ValueError, IndexError):
            continue

    return rows


def strip_cast(v: str | None) -> str:
    """Unwrap CAST(expr AS Type), including types with internal parens
    such as Decimal(18, 10) — the naive regex approach fails on those."""
    if not v:
        return ""
    v = v.strip()
    if v.upper().startswith("CAST(") and v.endswith(")"):
        inner = v[5:-1]
        idx = inner.upper().find(" AS ")
        if idx != -1:
            return inner[:idx].strip()
    return v


def money(v: str | None) -> Decimal:
    if not v:
        return Decimal("0")
    try:
        return Decimal(strip_cast(v))
    except InvalidOperation:
        return Decimal("0")


def load_sql(path: Path) -> str:
    return path.read_text(encoding="utf-16")


# ---------------------------------------------------------------------------
# Artist resolution
# ---------------------------------------------------------------------------

def find_users(sql_text: str, needle: str) -> list[dict]:
    """Case-insensitive substring match against Username / FullName / ArtistName."""
    needle_l = needle.lower()
    rows = parse_table(sql_text, "dbo", "Users",
                        ["UserID", "Username", "FullName", "Email", "ArtistName"])
    matches = []
    for r in rows:
        fields = [r.get("Username") or "", r.get("FullName") or "", r.get("ArtistName") or ""]
        if any(needle_l in f.lower() for f in fields):
            matches.append(r)
    return matches


def resolve_stream_artist_name(stream_rows: list[dict], user: dict) -> str | None:
    """Pick whichever of Username/FullName/ArtistName actually has MusicStreams rows."""
    candidates = [user.get("Username"), user.get("FullName"), user.get("ArtistName")]
    seen = []
    for c in candidates:
        if c and c not in seen:
            seen.append(c)

    lower_map: dict[str, str] = {}
    for r in stream_rows:
        name = r.get("ArtistName")
        if name:
            lower_map.setdefault(name.lower(), name)

    for c in seen:
        hit = lower_map.get(c.lower())
        if hit:
            return hit
    return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(rows: list[dict]) -> dict:
    total_streams = 0
    total_revenue = Decimal("0")
    total_redeemed = Decimal("0")
    platform_streams: dict[str, int] = defaultdict(int)
    platform_revenue: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    song_streams: dict[str, int] = defaultdict(int)
    song_revenue: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    song_redeemed: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    for r in rows:
        streams = int(r.get("Streams") or 0)
        revenue = money(r.get("Revenue"))
        redeemed = money(r.get("RedeemedAmount"))
        platform = (r.get("Platform") or "unknown").lower()
        song = r.get("Song") or "(untitled)"

        total_streams += streams
        total_revenue += revenue
        total_redeemed += redeemed
        platform_streams[platform] += streams
        platform_revenue[platform] += revenue
        song_streams[song] += streams
        song_revenue[song] += revenue
        song_redeemed[song] += redeemed

    return {
        "total_streams": total_streams,
        "total_revenue": total_revenue,
        "total_redeemed": total_redeemed,
        "remaining_balance": total_revenue - total_redeemed,
        "platforms": [
            {"platform": p, "streams": platform_streams[p], "revenue": platform_revenue[p]}
            for p in sorted(platform_streams, key=lambda p: -platform_revenue[p])
        ],
        "songs": [
            {
                "song": s,
                "streams": song_streams[s],
                "revenue": song_revenue[s],
                "redeemed": song_redeemed[s],
                "remaining": song_revenue[s] - song_redeemed[s],
            }
            for s in sorted(song_streams, key=lambda s: -song_revenue[s])
        ],
    }


def monthly_breakdown(rows: list[dict]) -> list[dict]:
    buckets: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for r in rows:
        month = r.get("Month") or ""
        year = r.get("Year") or ""
        buckets[(year, month)].append(r)

    out = []
    for (year, month), bucket_rows in buckets.items():
        agg = aggregate(bucket_rows)
        out.append({
            "year": year,
            "month": month,
            "streams": agg["total_streams"],
            "revenue": agg["total_revenue"],
            "redeemed": agg["total_redeemed"],
            "remaining": agg["remaining_balance"],
        })

    def sort_key(item):
        try:
            year_i = int(item["year"])
        except (TypeError, ValueError):
            year_i = 0
        month_i = _MONTH_INDEX.get((item["month"] or "").lower(), -1)
        return (year_i, month_i)

    return sorted(out, key=sort_key)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dump", nargs="?", default=DEFAULT_DUMP,
                     help=f"Path to the legacy SQL dump (default: {DEFAULT_DUMP})")
    ap.add_argument("--artist", required=True,
                     help="Name/username/artist-name substring to search for (case-insensitive)")
    ap.add_argument("--month", help="Full month name (e.g. March) or 1-12, to scope balance/stats to")
    ap.add_argument("--year", type=int, help="Year to pair with --month")
    ap.add_argument("--user-id", type=int, help="Disambiguate: exact UserID if --artist matches multiple users")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a text report")
    args = ap.parse_args()

    if bool(args.month) != bool(args.year):
        ap.error("--month and --year must be given together")

    month_name = None
    if args.month:
        if args.month.isdigit():
            idx = int(args.month) - 1
            if not (0 <= idx < 12):
                ap.error(f"--month numeric value out of range: {args.month}")
            month_name = MONTHS[idx]
        else:
            if args.month.lower() not in _MONTH_INDEX:
                ap.error(f"--month must be a full month name or 1-12, got {args.month!r}")
            month_name = MONTHS[_MONTH_INDEX[args.month.lower()]]

    dump_path = Path(args.dump)
    if not dump_path.exists():
        ap.error(f"dump not found: {dump_path}")

    sql_text = load_sql(dump_path)

    users = find_users(sql_text, args.artist)
    if not users:
        ap.error(f"no Users row matched {args.artist!r}")

    if args.user_id:
        user = next((u for u in users if str(u.get("UserID")) == str(args.user_id)), None)
        if user is None:
            ap.error(f"--user-id {args.user_id} not among Users rows matching {args.artist!r}")
    elif len(users) > 1:
        print(f"Multiple users match {args.artist!r} — pass --user-id to disambiguate:", file=sys.stderr)
        for u in users:
            print(f"  UserID={u.get('UserID')}  Username={u.get('Username')!r}  "
                  f"FullName={u.get('FullName')!r}  Email={u.get('Email')}", file=sys.stderr)
        sys.exit(1)
    else:
        user = users[0]

    all_streams = parse_table(
        sql_text, "dbo", "MusicStreams",
        ["ArtistName", "Song", "Streams", "Revenue", "Month", "Year", "Platform",
         "IsDeleted", "RedeemedAmount"],
    )

    stream_artist_name = resolve_stream_artist_name(all_streams, user)
    rows = [
        r for r in all_streams
        if stream_artist_name and r.get("ArtistName") == stream_artist_name and r.get("IsDeleted") != "1"
    ]

    scoped_rows = rows
    if month_name:
        scoped_rows = [
            r for r in rows
            if (r.get("Month") or "").lower() == month_name.lower()
            and str(r.get("Year")) == str(args.year)
        ]

    all_time = aggregate(rows)
    scoped = aggregate(scoped_rows) if month_name else None
    months = monthly_breakdown(rows)

    withdrawals_raw = parse_table(
        sql_text, "dbo", "WithdrawalHistory",
        ["Id", "UserId", "Amount", "Status", "CreatedDate", "ProcessedDate", "Description"],
    )
    user_withdrawals = [
        {
            "id": w.get("Id"),
            "amount": money(w.get("Amount")),
            "status": w.get("Status"),
            "created_date": strip_cast(w.get("CreatedDate") or ""),
            "processed_date": strip_cast(w.get("ProcessedDate") or "") or None,
        }
        for w in withdrawals_raw if str(w.get("UserId")) == str(user.get("UserID"))
    ]

    result = {
        "user": {
            "user_id": user.get("UserID"),
            "username": user.get("Username"),
            "full_name": user.get("FullName"),
            "email": user.get("Email"),
            "matched_stream_artist_name": stream_artist_name,
        },
        "scope": {"month": month_name, "year": args.year} if month_name else {"month": None, "year": None},
        "all_time": all_time,
        "scoped": scoped,
        "monthly_breakdown": months,
        "withdrawals": user_withdrawals,
    }

    if args.json:
        print(json.dumps(result, default=str, indent=2))
        return

    print_report(result)


def print_report(result: dict) -> None:
    u = result["user"]
    print(f"UserID={u['user_id']}  Username={u['username']!r}  FullName={u['full_name']!r}  Email={u['email']}")

    def dump_withdrawals() -> None:
        print("--- Withdrawal requests ---")
        if not result["withdrawals"]:
            print("  (none)")
        for w in result["withdrawals"]:
            print(f"  Id={w['id']} amount={w['amount']} status={w['status']} "
                  f"created={w['created_date']} processed={w['processed_date']}")

    if not u["matched_stream_artist_name"]:
        print("WARNING: no MusicStreams.ArtistName matched this user — stream/song stats are 0.")
        print()
        dump_withdrawals()
        return
    print(f"Matched MusicStreams.ArtistName = {u['matched_stream_artist_name']!r}")
    print()

    def dump_agg(label: str, agg: dict) -> None:
        print(f"--- {label} ---")
        print(f"Total streams: {agg['total_streams']}")
        print(f"Total revenue: {agg['total_revenue']}")
        print(f"Total redeemed (allocated to withdrawals): {agg['total_redeemed']}")
        print(f"Remaining available balance: {agg['remaining_balance']}")
        print("Platforms:")
        for p in agg["platforms"]:
            print(f"  {p['platform']:12s} streams={p['streams']:>10d}  revenue={p['revenue']}")
        print("Songs:")
        for s in agg["songs"]:
            print(f"  {s['song']:30s} streams={s['streams']:>10d}  revenue={s['revenue']:>12}  remaining={s['remaining']}")
        print()

    dump_agg("All-time", result["all_time"])
    if result["scoped"] is not None:
        scope = result["scope"]
        dump_agg(f"Scoped: {scope['month']} {scope['year']}", result["scoped"])

    print("--- Monthly breakdown ---")
    for m in result["monthly_breakdown"]:
        print(f"  {m['month']} {m['year']}: streams={m['streams']} revenue={m['revenue']} "
              f"redeemed={m['redeemed']} remaining={m['remaining']}")
    print()

    dump_withdrawals()


if __name__ == "__main__":
    main()
