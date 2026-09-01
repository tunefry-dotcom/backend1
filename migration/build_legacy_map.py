"""Build the legacy artist/ISRC -> email map from the SQL Server dump.

The DSP-consolidated Excel report identifies artists by DISPLAY name only.
Our Supabase profiles/auth_users tables don't always have the same display
name (e.g., an artist registered as "iamtenk9" but reported as "I Am Ten K").
The legacy SQL Server dump has this mapping directly in ``dbo.Users``
(Email + ArtistName + FullName + Username) and ``dbo.ReleaseDetails``
(Isrc + Upc + Artist + SongTitle) — it's the AUTHORITATIVE source for
"who owns which artist name / ISRC / UPC".

This script:
  1. Parses ``dbo.Users`` and ``dbo.ReleaseDetails`` from the SQL dump
  2. Cross-references with Supabase ``auth.users`` (case-insensitive email)
     to filter down to accounts that actually exist in Tunefry today
  3. Writes ``migration/legacy_map.json`` with normalized-name-to-email
     and ISRC-to-email + UPC-to-email lookups

Re-run whenever you receive a fresh SQL dump. The JSON is committed to git
as the audit trail of "at time T, these names/ISRCs pointed to these
emails". Never edited by hand.

Safety: read-only against Supabase (only `auth.admin.list_users`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

from app.core.supabase_client import get_service_client
from migration.migrate_releases import parse_table


LEGACY_MAP_PATH = Path("migration/legacy_map.json")
DEFAULT_SQL_FILE = r"C:\Users\ViditVaibhav\Downloads\table with data.sql"


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (str(s) if s is not None else "").strip().lower())


def _load_supabase_emails(svc) -> set[str]:
    """All emails currently in auth.users (lowercased)."""
    out: set[str] = set()
    page_num = 1
    while True:
        page = svc.auth.admin.list_users(page=page_num, per_page=1000)
        users = page if isinstance(page, list) else getattr(page, "users", []) or []
        if not users:
            break
        for u in users:
            email = getattr(u, "email", None) or (u.get("email") if isinstance(u, dict) else None)
            if email:
                out.add(email.lower())
        if len(users) < 1000:
            break
        page_num += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sql_file", nargs="?", default=DEFAULT_SQL_FILE,
                    help="Path to legacy SQL dump (UTF-16)")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing legacy_map.json")
    args = ap.parse_args()

    if LEGACY_MAP_PATH.exists() and not args.force:
        print(f"ERROR: {LEGACY_MAP_PATH} exists. Pass --force to overwrite.",
              file=sys.stderr)
        sys.exit(2)

    print(f"Reading SQL dump ({args.sql_file}) — may take ~30 s …")
    sql_text = Path(args.sql_file).read_text(encoding="utf-16")

    users = parse_table(sql_text, "dbo", "Users",
                        ["UserID", "Email", "ArtistName", "FullName", "Username"])
    releases = parse_table(sql_text, "dbo", "ReleaseDetails", [
        "ReleaseID", "UserID", "SongTitle", "Artist",
        "Isrc", "Upc", "IsActive",
    ])
    print(f"  Parsed {len(users)} users, {len(releases)} releases")

    print("Fetching Supabase auth.users emails (read-only)…")
    svc = get_service_client()
    supabase_emails = _load_supabase_emails(svc)
    print(f"  {len(supabase_emails)} Supabase user emails")

    # legacy UserID -> email
    uid_to_email: dict[int, str] = {}
    # name-variant sets: name -> set(emails)
    artist_name_emails: dict[str, set[str]] = defaultdict(set)
    full_name_emails: dict[str, set[str]] = defaultdict(set)
    username_emails: dict[str, set[str]] = defaultdict(set)

    for u in users:
        email_raw = (u.get("Email") or "").strip().lower()
        if not email_raw or email_raw not in supabase_emails:
            continue                     # skip legacy users not in Supabase
        try:
            uid = int(float(u["UserID"]))
        except (ValueError, TypeError, KeyError):
            continue
        uid_to_email[uid] = email_raw
        for field, bucket in (
            ("ArtistName", artist_name_emails),
            ("FullName", full_name_emails),
            ("Username", username_emails),
        ):
            n = norm(u.get(field))
            if n:
                bucket[n].add(email_raw)

    def _dedupe(m: dict[str, set[str]]) -> tuple[dict[str, str], int]:
        clean: dict[str, str] = {}
        dropped = 0
        for name, emails in m.items():
            if len(emails) == 1:
                clean[name] = next(iter(emails))
            else:
                dropped += 1
        return clean, dropped

    artist_map, a_drop = _dedupe(artist_name_emails)
    full_map, f_drop = _dedupe(full_name_emails)
    user_map, u_drop = _dedupe(username_emails)

    print(f"  artist_name : {len(artist_map)} unique -> email ({a_drop} ambiguous)")
    print(f"  full_name   : {len(full_map)} unique -> email ({f_drop} ambiguous)")
    print(f"  username    : {len(user_map)} unique -> email ({u_drop} ambiguous)")

    # ISRC / UPC lookup from ReleaseDetails.
    isrc_emails: dict[str, set[str]] = defaultdict(set)
    upc_emails: dict[str, set[str]] = defaultdict(set)
    # And artist-title -> email for ISRC-less fallback.
    at_emails: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in releases:
        try:
            uid = int(float(r["UserID"]))
        except (ValueError, TypeError, KeyError):
            continue
        email = uid_to_email.get(uid)
        if not email:
            continue
        isrc = (r.get("Isrc") or "").strip().upper()
        upc = (r.get("Upc") or "").strip().upper()
        if isrc:
            isrc_emails[isrc].add(email)
        if upc:
            upc_emails[upc].add(email)
        artist = norm(r.get("Artist"))
        title = norm(r.get("SongTitle"))
        if artist and title:
            at_emails[(artist, title)].add(email)

    isrc_map, i_drop = _dedupe(isrc_emails)
    upc_map, up_drop = _dedupe(upc_emails)
    at_map: dict[str, str] = {}
    at_drop = 0
    for (a, t), emails in at_emails.items():
        if len(emails) == 1:
            at_map[f"{a}||{t}"] = next(iter(emails))
        else:
            at_drop += 1

    print(f"  ISRC        : {len(isrc_map)} unique -> email ({i_drop} ambiguous)")
    print(f"  UPC         : {len(upc_map)} unique -> email ({up_drop} ambiguous)")
    print(f"  artist||title: {len(at_map)} unique -> email ({at_drop} ambiguous)")

    payload = {
        "_notes": (
            "Auto-generated by build_legacy_map.py from the legacy SQL dump. "
            "Filters to emails that exist in Supabase auth.users. Ambiguous "
            "names (mapping to more than one email) are DROPPED (never guessed). "
            "Do not edit by hand — regenerate from the SQL dump instead."
        ),
        "artist_name": artist_map,
        "full_name": full_map,
        "username": user_map,
        "isrc": isrc_map,
        "upc": upc_map,
        "artist_title": at_map,
    }
    LEGACY_MAP_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {LEGACY_MAP_PATH}")


if __name__ == "__main__":
    main()
