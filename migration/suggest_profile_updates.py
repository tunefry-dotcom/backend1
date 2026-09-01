"""Suggest profiles.artist_name fixes for top unmatched artists.

Reads the latest unmatched_*.csv (from a recent ingest_royalty_report.py
dry-run) and fuzzy-matches each top artist against every existing Supabase
user's identifying names (profiles.artist_name / full_name +
auth.raw_user_meta_data.artist_name / full_name + email prefix). Writes
``suggested_profile_updates.csv`` next to the input CSV with the top 3
candidate matches per artist and the corresponding SQL UPDATE statement.

Read-only against Supabase. Writes only one local file.

Usage
-----
    python migration/suggest_profile_updates.py <unmatched_csv>  # explicit path
    python migration/suggest_profile_updates.py                  # newest one
        [--top N] [--min-usd F]
"""

from __future__ import annotations

import argparse
import csv
import difflib
import glob
import os
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

from app.core.supabase_client import get_service_client


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (str(s) if s is not None else "").strip().lower())


def _paginate(svc, table: str, columns: str, page: int = 1000) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        try:
            res = (svc.table(table).select(columns)
                   .range(offset, offset + page - 1).execute())
        except Exception:
            break
        rows = res.data or []
        out.extend(rows)
        if len(rows) < page:
            break
        offset += page
    return out


def build_user_registry(svc) -> list[dict]:
    """One record per user: id, email, list-of-name-variants (normalized)."""
    # profiles: user_id, artist_name, full_name
    profiles = _paginate(svc, "profiles", "user_id, artist_name, full_name")
    profile_by_uid: dict[str, dict] = {}
    for p in profiles:
        uid = p.get("user_id")
        if uid:
            profile_by_uid[str(uid)] = p

    # auth users: paginate via admin
    users: list[dict] = []
    page_num = 1
    while True:
        page = svc.auth.admin.list_users(page=page_num, per_page=1000)
        page_users = page if isinstance(page, list) else getattr(page, "users", []) or []
        if not page_users:
            break
        for u in page_users:
            uid = str(getattr(u, "id", None) or u.get("id"))
            email = getattr(u, "email", None) or u.get("email") or ""
            meta = getattr(u, "user_metadata", None)
            if meta is None and isinstance(u, dict):
                meta = u.get("user_metadata")
            meta = meta or {}
            prof = profile_by_uid.get(uid, {})
            names: list[str] = []
            for field in ("artist_name", "full_name"):
                if prof.get(field):
                    names.append(str(prof[field]))
                if isinstance(meta, dict) and meta.get(field):
                    names.append(str(meta[field]))
            # email prefix (before @) — often a nickname
            prefix = email.split("@", 1)[0] if "@" in email else email
            if prefix:
                names.append(prefix)
            users.append({
                "user_id": uid,
                "email": email.lower(),
                "current_artist_name": prof.get("artist_name") or "",
                "current_full_name": prof.get("full_name") or "",
                "name_variants": names,
                "norm_variants": [norm(n) for n in names if n],
            })
        if len(page_users) < 1000:
            break
        page_num += 1

    return users


def best_matches(query: str, users: list[dict], k: int = 3) -> list[tuple[float, dict, str]]:
    """Top-k (score, user_record, matching_variant) for a query name."""
    qn = norm(query)
    if not qn:
        return []
    scored: list[tuple[float, dict, str]] = []
    for u in users:
        best_score = 0.0
        best_variant = ""
        for variant in u["norm_variants"]:
            if not variant:
                continue
            # SequenceMatcher ratio — 1.0 = identical, 0 = no shared chars.
            score = difflib.SequenceMatcher(None, qn, variant).ratio()
            # Substring bonus — only when the shorter side is at least 50%
            # of the longer side. Prevents a 2-char variant like "ar" from
            # falsely matching every query it happens to be inside.
            if (qn in variant or variant in qn) and min(len(qn), len(variant)) >= 0.5 * max(len(qn), len(variant)):
                score = max(score, 0.85)
            if score > best_score:
                best_score = score
                best_variant = variant
        if best_score > 0.55:
            scored.append((best_score, u, best_variant))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[:k]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("unmatched_csv", nargs="?",
                    help="Path to unmatched_YYYYMMDD_HHMM.csv (defaults to newest)")
    ap.add_argument("--top", type=int, default=50,
                    help="Consider only the top-N unmatched by USD (default 50)")
    ap.add_argument("--min-usd", type=float, default=100.0,
                    help="Skip artists below this USD threshold (default 100)")
    args = ap.parse_args()

    # Locate the input CSV.
    if args.unmatched_csv:
        csv_path = Path(args.unmatched_csv)
    else:
        candidates = sorted(glob.glob("**/unmatched_*.csv", recursive=True),
                            key=os.path.getmtime, reverse=True)
        if not candidates:
            print("ERROR: no unmatched_*.csv found. Pass an explicit path.",
                  file=sys.stderr)
            sys.exit(2)
        csv_path = Path(candidates[0])
    print(f"Reading {csv_path}")

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Sort by INR if available (INR workbook), else by USD (USD workbook).
    def _sort_val(r: dict) -> Decimal:
        inr = Decimal(r.get("total_royalty_inr") or "0")
        return inr if inr > 0 else Decimal(r.get("total_royalty_usd") or "0")
    rows.sort(key=_sort_val, reverse=True)
    rows = [r for r in rows if _sort_val(r) >= Decimal(str(args.min_usd))]
    rows = rows[:args.top]
    print(f"  reviewing top {len(rows)} unmatched (>= {args.min_usd} threshold)")

    print("Fetching Supabase user registry (read-only)…")
    svc = get_service_client()
    users = build_user_registry(svc)
    print(f"  {len(users)} users indexed")

    out_path = csv_path.with_name("suggested_profile_updates.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "unmatched_artist", "sub_label", "usd", "inr", "streams", "rows",
            "candidate_email", "candidate_current_artist_name",
            "candidate_full_name", "match_score", "matched_variant",
            "sql_update",
        ])
        for r in rows:
            artist = r["artist"]
            inr_val = r.get("total_royalty_inr") or "0"
            candidates = best_matches(artist, users, k=3)
            if not candidates:
                w.writerow([artist, r["sub_label"], r["total_royalty_usd"],
                            inr_val, r["total_streams"], r["row_count"],
                            "", "", "", "", "",
                            "-- no candidate above threshold; likely external artist"])
                continue
            for score, u, variant in candidates:
                # SQL update — DOES NOT run automatically. User pastes into Supabase SQL editor.
                artist_sql = artist.replace("'", "''")
                sql = (f"UPDATE public.profiles SET artist_name = '{artist_sql}' "
                       f"WHERE user_id = '{u['user_id']}';")
                w.writerow([
                    artist, r["sub_label"], r["total_royalty_usd"],
                    inr_val, r["total_streams"], r["row_count"],
                    u["email"], u["current_artist_name"],
                    u["current_full_name"], f"{score:.3f}", variant, sql,
                ])
    print(f"\nWrote {out_path}")
    print("\nHow to use it:")
    print("  1. Open the CSV. Each unmatched artist has up to 3 candidate matches.")
    print("  2. For each artist you recognize as YOUR user: pick the correct row")
    print("     and run its `sql_update` in Supabase SQL editor.")
    print("  3. After running the updates, re-run ingest_royalty_report.py")
    print("     (default is dry-run) to confirm the match rate improved.")


if __name__ == "__main__":
    main()
