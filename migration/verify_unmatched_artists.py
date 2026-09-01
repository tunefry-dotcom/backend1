"""Re-verify, with progressively looser matching, which legacy
MusicStreams.ArtistName values genuinely correspond to NO Users account —
before reporting anything as "no matching account, in any way".

Why this exists
----------------
export_all_artist_stats.py's Unmatched_Names sheet is built from a single
exact (case-insensitive) comparison against Username/FullName/ArtistName.
That's the right default for *attributing revenue* (never guess), but it
will also flag a real user as "unmatched" purely because of a casing,
punctuation, or multi-artist-tag difference between the stream name and
their account fields (e.g. "Brook [feat. MC Sick]" vs a Users row for
"Brook"). Before telling anyone an artist has no account at all, re-check
with looser heuristics so those cases don't get wrongly reported.

Nothing here re-attributes revenue automatically — a name is only ever
downgraded from "no match" to "recovered"/"possible" for reporting purposes;
export_all_artist_stats.py's Summary sheet is untouched.

Tiers, loosest match wins, in this order:
  1. Exact case-insensitive match against Username/FullName/ArtistName
     (identical to export_all_artist_stats.py's resolve_all() — anything
     that matches here isn't in the input set at all).
  2. Normalized match — strip "[...]"/"(...)" suffixes, lowercase, strip all
     non-alphanumeric characters, compare the full remaining string.
  3. Multi-artist split — split on comma / "&" / "and" / "x" / "feat(uring)"/
     "ft" / two-or-more spaces (same separators ingest_royalty_report.py uses
     for multi-artist attribution) and re-run tier 2 on each part.
  4. Substring — a normalized candidate name (>=4 chars) is a substring of
     the normalized stream name or vice versa. Reported as "possible" only
     — never treated as confirmed, since short/common names produce noise.
     Candidates matching more than GENERIC_HIT_THRESHOLD distinct unmatched
     names are treated as prefix-collision noise and suppressed entirely.

Only names failing every tier go into the final "confirmed no match by any
method" report. That report is further split into real artist names vs
structured batch-upload codes (uppercase-alpha prefix + 4-digit suffix,
e.g. CLATCHA2414) since those require different follow-up action.

Read-only: parses the dump, writes only the output .csv files.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from legacy_artist_stats import DEFAULT_DUMP, aggregate, load_sql, parse_table  # noqa: E402
from export_all_artist_stats import resolve_all  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(__file__).parent

_SPLIT_RE = re.compile(
    r"\s{2,}|\s*(?:,|&|\band\b|\bx\b|\bfeat\.?\b|\bfeaturing\b|\bft\.?\b)\s*",
    re.IGNORECASE,
)
_BRACKET_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")

# A substring "hit" that recurs across many otherwise-unrelated unmatched
# names is a generic/placeholder prefix collision (e.g. one user's
# ArtistName="CLATCHA2413" sitting inside 234 distinct "CLATCHA24xx"-style
# batch codes), not 234 real matches to that one account. Anything matching
# more than this many distinct names is treated as noise, not a lead.
GENERIC_HIT_THRESHOLD = 3

# Structured batch-upload codes: uppercase-letter prefix + 4-or-more digit
# suffix, no spaces (e.g. CLATCHA2414). These are label batch-ingest codes,
# not individual artist names, so they're reported separately from real artists.
_BATCH_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*\d{4,}$")


def normalize(s: str | None) -> str:
    s = (s or "").lower()
    s = _BRACKET_RE.sub(" ", s)
    s = _NONALNUM_RE.sub("", s)
    return s


def split_parts(name: str) -> list[str]:
    return [p for p in _SPLIT_RE.split(name) if p.strip()]


def build_normalized_map(users: list[dict]) -> dict[str, set[str]]:
    m: dict[str, set[str]] = defaultdict(set)
    for u in users:
        uid = str(u.get("UserID"))
        for field in ("Username", "FullName", "ArtistName"):
            v = u.get(field)
            if v:
                norm = normalize(v)
                if norm:
                    m[norm].add(uid)
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dump", nargs="?", default=DEFAULT_DUMP,
                     help=f"Path to the legacy SQL dump (default: {DEFAULT_DUMP})")
    ap.add_argument("-o", "--output-dir", default=str(DEFAULT_OUTPUT_DIR),
                     help="Directory to write the report CSVs into")
    args = ap.parse_args()

    dump_path = Path(args.dump)
    if not dump_path.exists():
        ap.error(f"dump not found: {dump_path}")

    print(f"Loading {dump_path} ...")
    sql_text = load_sql(dump_path)

    print("Parsing dbo.Users ...")
    users = parse_table(sql_text, "dbo", "Users",
                         ["UserID", "Username", "FullName", "Email", "ArtistName"])
    users_by_id = {str(u.get("UserID")): u for u in users}

    print("Parsing dbo.MusicStreams ...")
    all_streams = parse_table(
        sql_text, "dbo", "MusicStreams",
        ["ArtistName", "Song", "Streams", "Revenue", "Month", "Year", "Platform",
         "IsDeleted", "RedeemedAmount"],
    )
    stream_rows = [r for r in all_streams if r.get("IsDeleted") != "1"]

    print("Tier 1: exact case-insensitive match ...")
    _matched, _ambiguous, unmatched = resolve_all(stream_rows, users)
    print(f"  {len(unmatched)} names unresolved by exact match — re-verifying these")

    normalized_map = build_normalized_map(users)

    # Pre-pass: find candidate normalized names that substring-match too many
    # distinct unmatched stream names. Such candidates are prefix-collision noise
    # (e.g. "clatcha2413" matching 234 "clatcha24xx" batch codes) rather than
    # real artist leads, and are excluded from tier-4 consideration.
    _cand_hit_count: dict[str, int] = defaultdict(int)
    for _uname in unmatched:
        _nn = normalize(_uname)
        if len(_nn) < 4:
            continue
        for _cn in normalized_map:
            if len(_cn) >= 4 and (_cn in _nn or _nn in _cn):
                _cand_hit_count[_cn] += 1
    generic_candidates = {cn for cn, cnt in _cand_hit_count.items()
                          if cnt > GENERIC_HIT_THRESHOLD}
    if generic_candidates:
        print(f"  Suppressing {len(generic_candidates)} generic candidate(s) from "
              f"tier-4 (each matched >{GENERIC_HIT_THRESHOLD} names): "
              + ", ".join(sorted(generic_candidates)))
    print()

    recovered: list[tuple] = []   # (name, tier, candidate_uids, agg)
    possible: list[tuple] = []    # (name, candidate_uids, agg)
    truly_unmatched: list[tuple] = []  # (name, agg)

    for name, rows in sorted(unmatched.items()):
        agg = aggregate(rows)
        norm_name = normalize(name)

        # Tier 2: normalized full-string match
        uids = normalized_map.get(norm_name, set()) if norm_name else set()
        if uids:
            recovered.append((name, "normalized", sorted(uids), agg))
            continue

        # Tier 3: multi-artist split, normalized match per part
        split_uids: set[str] = set()
        for part in split_parts(name):
            pn = normalize(part)
            if pn:
                split_uids |= normalized_map.get(pn, set())
        if split_uids:
            recovered.append((name, "split", sorted(split_uids), agg))
            continue

        # Tier 4: substring match (possible only, never confirmed).
        # Generic candidates — those matching >GENERIC_HIT_THRESHOLD names —
        # are excluded to avoid prefix-collision false positives.
        sub_uids: set[str] = set()
        if len(norm_name) >= 4:
            for cand_norm, cand_uids in normalized_map.items():
                if len(cand_norm) < 4 or cand_norm in generic_candidates:
                    continue
                if cand_norm in norm_name or norm_name in cand_norm:
                    sub_uids |= cand_uids
        if sub_uids:
            possible.append((name, sorted(sub_uids), agg))
            continue

        truly_unmatched.append((name, agg))

    # Split confirmed-unmatched into real artist names vs batch-upload codes
    # (e.g. CLATCHA2414) — these need different follow-up action.
    batch_codes = [(n, a) for n, a in truly_unmatched if _BATCH_CODE_RE.match(n)]
    real_unmatched = [(n, a) for n, a in truly_unmatched if not _BATCH_CODE_RE.match(n)]

    print(f"Recovered via normalization/split  : {len(recovered)}")
    print(f"Possible (substring, unconfirmed)  : {len(possible)}")
    print(f"Confirmed no match — real artists  : {len(real_unmatched)}")
    print(f"Confirmed no match — batch codes   : {len(batch_codes)}")
    print()

    out_dir = Path(args.output_dir)

    def user_label(uid: str) -> str:
        u = users_by_id.get(uid)
        if not u:
            return uid
        return f"{uid}:{u.get('Username')} ({u.get('Email')})"

    recovered_path = out_dir / "unmatched_recovered.csv"
    with open(recovered_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ArtistName", "Tier", "CandidateUsers", "Streams", "Revenue"])
        for name, tier, uids, agg in recovered:
            w.writerow([name, tier, "; ".join(user_label(u) for u in uids),
                        agg["total_streams"], agg["total_revenue"]])

    possible_path = out_dir / "unmatched_possible.csv"
    with open(possible_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ArtistName", "CandidateUsers (substring match — unconfirmed)",
                     "Streams", "Revenue"])
        for name, uids, agg in possible:
            w.writerow([name, "; ".join(user_label(u) for u in uids),
                        agg["total_streams"], agg["total_revenue"]])

    confirmed_path = out_dir / "unmatched_confirmed.csv"
    with open(confirmed_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ArtistName", "Streams", "Revenue", "DistinctSongs"])
        for name, agg in sorted(real_unmatched, key=lambda t: -t[1]["total_revenue"]):
            w.writerow([name, agg["total_streams"], agg["total_revenue"], len(agg["songs"])])

    batch_path = out_dir / "unmatched_batchcodes.csv"
    with open(batch_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ArtistName", "Streams", "Revenue", "DistinctSongs"])
        for name, agg in sorted(batch_codes, key=lambda t: t[0]):
            w.writerow([name, agg["total_streams"], agg["total_revenue"], len(agg["songs"])])

    print(f"Wrote {recovered_path}")
    print(f"Wrote {possible_path}")
    print(f"Wrote {confirmed_path}")
    print(f"Wrote {batch_path}")

    real_rev = sum(agg["total_revenue"] for _, agg in real_unmatched)
    real_str = sum(agg["total_streams"] for _, agg in real_unmatched)
    batch_rev = sum(agg["total_revenue"] for _, agg in batch_codes)
    batch_str = sum(agg["total_streams"] for _, agg in batch_codes)
    print()
    print(f"Confirmed no-match (artists): {real_str:,} streams, "
          f"{real_rev:.4f} revenue across {len(real_unmatched)} names")
    print(f"Confirmed no-match (batches): {batch_str:,} streams, "
          f"{batch_rev:.4f} revenue across {len(batch_codes)} codes")


if __name__ == "__main__":
    main()
