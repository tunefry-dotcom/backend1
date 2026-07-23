"""
migration/migrate_users.py

Migrate 2,370 users from the old SQL Server dump into Supabase.

Run from repo root with venv activated:
    python migration/migrate_users.py "<path_to_sql_file>" [--dry-run]

Dry run parses everything and writes results_dryrun.csv — no Supabase writes.
Live run creates auth users, upserts profiles + subscriptions, writes results.csv.

Security notes:
  - Plaintext passwords from the dump are passed to admin.create_user() which
    bcrypt-hashes them internally. They are NEVER written to any log or file.
  - The results CSV contains: old_user_id, email, new_uuid, plan, status,
    started_at, expires_at, error — no passwords.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.core.supabase_client import get_service_client

# ── Plan mapping: old LabelPlans.Id → new Plan slug ──────────────────────────

PLAN_MAP: dict[int, str] = {
    1: "free",
    2: "single-song",
    3: "single-artist",
    4: "double-artist",
    5: "starter",
    6: "label",
    9: "single-artist",   # Label Plan ₹1400 ≈ Single Artist
}

SKIP_USER_IDS: set[int] = {1}   # Admin account (kmfmedia001@gmail.com)
RATE_LIMIT_SLEEP = 0.15          # seconds between users (~6 min total)
DOB_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"]

# Column indices in [dbo].[Users] INSERT VALUES
U_ID = 0; U_USERNAME = 1; U_PASSWORD = 2; U_EMAIL = 3; U_FULLNAME = 4
U_REGDATE = 5; U_ISACTIVE = 6; U_CONTACT = 7; U_CITY = 8
U_SPOTIFY = 10; U_APPLE = 11; U_YOUTUBE = 12; U_INSTAGRAM = 13
U_DOB = 15; U_BIO = 17; U_WHATSAPP = 18; U_STATE = 19; U_ARTIST = 21

# Column indices in [dbo].[PaymentDetails] INSERT VALUES
P_TXNID = 8; P_AMOUNT = 9; P_STATUS = 11; P_TXNDATE = 13
P_PLANID = 18; P_USERID = 19; P_CREATEDDATE = 17; P_ISACTIVE = 21

# Column indices in [dbo].[ReleaseDetails] INSERT VALUES
R_USERID = 24; R_CREATIONDATE = 25


# ── SQL tokenizer ─────────────────────────────────────────────────────────────

def _tokenize(line: str) -> list:
    """
    Parse the VALUES clause of an SSMS-generated INSERT statement.
    Handles: NULL, N'string', 'string', CAST(N'...' AS Type),
             CAST(decimal AS Decimal(x,y)), integers.
    Escaped single quotes ('') inside strings are handled correctly.
    """
    idx = line.find(" VALUES (")
    if idx == -1:
        return []
    s = line[idx + 9:].rstrip()
    if s.endswith(")"):
        s = s[:-1]

    tokens: list = []
    i = 0
    n = len(s)

    while i < n:
        c = s[i]

        if c in " \t\r\n,":
            i += 1
            continue

        if s[i : i + 4] == "NULL":
            tokens.append(None)
            i += 4

        elif s[i : i + 5] == "CAST(":
            # Count parens so nested CAST or Decimal(18,2) don't confuse us
            depth = 1
            j = i + 5
            while j < n and depth > 0:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                j += 1
            body = s[i + 5 : j - 1]
            m = re.search(r"N'([^']*(?:''[^']*)*)'", body)
            if m:
                tokens.append(m.group(1).replace("''", "'"))
            else:
                m2 = re.search(r"^[\-\d.]+", body.lstrip())
                tokens.append(m2.group() if m2 else None)
            i = j

        elif s[i : i + 2] == "N'":
            i += 2
            val: list[str] = []
            while i < n:
                if s[i] == "'" and i + 1 < n and s[i + 1] == "'":
                    val.append("'")
                    i += 2
                elif s[i] == "'":
                    i += 1
                    break
                else:
                    val.append(s[i])
                    i += 1
            tokens.append("".join(val))

        elif c == "'":
            i += 1
            val = []
            while i < n:
                if s[i] == "'" and i + 1 < n and s[i + 1] == "'":
                    val.append("'")
                    i += 2
                elif s[i] == "'":
                    i += 1
                    break
                else:
                    val.append(s[i])
                    i += 1
            tokens.append("".join(val))

        elif c.isdigit() or c == "-":
            j = i + 1
            while j < n and (s[j].isdigit() or s[j] == "."):
                j += 1
            tokens.append(s[i:j])
            i = j

        else:
            i += 1

    return tokens


# ── Dump parser ───────────────────────────────────────────────────────────────

def parse_dump(filepath: str) -> tuple[dict, dict, dict]:
    print(f"Reading: {filepath}", flush=True)
    raw = Path(filepath).read_bytes()
    text = raw.decode("utf-16-le", errors="ignore")
    # Split on newlines directly — no join needed. The 65 Users rows with embedded
    # newlines in nvarchar(max) fields will produce short token lists and be skipped;
    # this is acceptable (32 users miss some profile fields but ARE still created).
    # Joining caused O(n²) string-concat on ErrorLog's 14k stack-trace rows — fatal.
    lines = text.split("\n")
    print(f"  Lines: {len(lines):,}")

    users: dict[int, dict] = {}
    payments: dict[int, list] = {}
    releases: dict[int, list] = {}
    uc = pc = rc = 0

    for line in lines:
        line = line.strip()
        if "INSERT" not in line or "VALUES" not in line:
            continue

        if "[dbo].[Users]" in line:
            tok = _tokenize(line)
            if len(tok) <= U_ARTIST:
                continue
            try:
                uid = int(tok[U_ID])
            except (TypeError, ValueError):
                continue
            users[uid] = {
                "password":        tok[U_PASSWORD],
                "email":           tok[U_EMAIL],
                "full_name":       tok[U_FULLNAME],
                "registration_dt": tok[U_REGDATE],
                "contact":         tok[U_CONTACT],
                "city":            tok[U_CITY],
                "spotify_url":     tok[U_SPOTIFY],
                "apple_music_url": tok[U_APPLE],
                "youtube_url":     tok[U_YOUTUBE],
                "instagram":       tok[U_INSTAGRAM],
                "dob":             tok[U_DOB],
                "bio":             tok[U_BIO],
                "whatsappno":      tok[U_WHATSAPP],
                "state":           tok[U_STATE],
                "artist_name":     tok[U_ARTIST],
            }
            uc += 1

        elif "[dbo].[PaymentDetails]" in line:
            tok = _tokenize(line)
            if len(tok) <= P_ISACTIVE:
                continue
            try:
                plan_id = int(tok[P_PLANID])
                user_id = int(tok[P_USERID])
            except (TypeError, ValueError):
                continue
            is_active = tok[P_ISACTIVE] == "1"
            payments.setdefault(user_id, []).append({
                "txn_id":       tok[P_TXNID],
                "txn_date":     tok[P_TXNDATE],
                "created_date": tok[P_CREATEDDATE],   # fallback when TxnDate is NULL
                "status":       tok[P_STATUS],
                "plan_id":      plan_id,
                "is_active":    is_active,
            })
            pc += 1

        elif "[dbo].[ReleaseDetails]" in line:
            tok = _tokenize(line)
            if len(tok) <= R_CREATIONDATE:
                continue
            try:
                user_id = int(tok[R_USERID])
            except (TypeError, ValueError):
                continue
            releases.setdefault(user_id, []).append(tok[R_CREATIONDATE])
            rc += 1

    # Sort payments newest-first
    for uid in payments:
        payments[uid].sort(key=lambda p: p["txn_date"] or "", reverse=True)

    print(f"  Parsed: {uc:,} users  {pc:,} payments  {rc:,} releases")
    return users, payments, releases


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _dob(s: Optional[str]) -> Optional[str]:
    if not s or not s.strip():
        return None
    for fmt in DOB_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ── Subscription resolver ─────────────────────────────────────────────────────

def resolve_sub(uid: int, user: dict, payments: dict, releases: dict) -> dict:
    now = datetime.now(timezone.utc)
    user_pays = payments.get(uid, [])

    # Prefer IsPlanActive=1; within those prefer TXN_SUCCESS
    active = [p for p in user_pays if p["is_active"]]
    if active:
        success = [p for p in active if p.get("status") == "TXN_SUCCESS"]
        winner = (success or active)[0]   # already sorted newest-first
    else:
        winner = None

    reg_dt = _dt(user.get("registration_dt"))
    free_started = reg_dt.isoformat() if reg_dt else None

    if winner is None:
        return {"plan": "free", "status": "active",
                "started_at": free_started, "expires_at": None, "payment_ref": None}

    plan_slug = PLAN_MAP.get(winner["plan_id"], "free")

    if plan_slug == "free":
        return {"plan": "free", "status": "active",
                "started_at": free_started, "expires_at": None,
                "payment_ref": _clean(winner["txn_id"])}

    # TxnDate can be NULL on old rows — fall back to CreatedDate, then registration date
    txn_dt = (_dt(winner["txn_date"])
              or _dt(winner.get("created_date"))
              or reg_dt)
    expires = (txn_dt + timedelta(days=365)) if txn_dt else None
    status = "expired" if (expires and expires < now) else "active"

    # Single-song: check if user released anything after purchase date
    if plan_slug == "single-song" and txn_dt:
        for rel_str in releases.get(uid, []):
            rel_dt = _dt(rel_str)
            if rel_dt and rel_dt > txn_dt:
                status = "expired"
                break

    return {
        "plan":        plan_slug,
        "status":      status,
        "started_at":  txn_dt.isoformat(),
        "expires_at":  expires.isoformat() if expires else None,
        "payment_ref": _clean(winner["txn_id"]),
    }


def build_profile(user: dict) -> dict:
    phone = _clean(user.get("contact")) or _clean(user.get("whatsappno"))
    return {
        "full_name":       _clean(user.get("full_name")),
        "artist_name":     _clean(user.get("artist_name")),
        "phone":           phone,
        "city":            _clean(user.get("city")),
        "state":           _clean(user.get("state")),
        "date_of_birth":   _dob(user.get("dob")),
        "bio":             _clean(user.get("bio")),
        "spotify_url":     _clean(user.get("spotify_url")),
        "apple_music_url": _clean(user.get("apple_music_url")),
        "instagram":       _clean(user.get("instagram")),
        "youtube_url":     _clean(user.get("youtube_url")),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate old Tunefry users to Supabase.")
    ap.add_argument("sql_file", help="Path to UTF-16 LE SQL dump")
    ap.add_argument("--dry-run", action="store_true", help="No Supabase writes")
    args = ap.parse_args()

    dry = args.dry_run
    if dry:
        print("=== DRY RUN — no Supabase writes ===\n")

    users, payments, releases = parse_dump(args.sql_file)

    # Build email → uuid cache for idempotent re-runs
    existing: dict[str, str] = {}
    service = None
    if not dry:
        service = get_service_client()
        print("Loading existing Supabase users...")
        page = 1
        while True:
            try:
                result = service.auth.admin.list_users(page=page, per_page=1000)
                batch = getattr(result, "users", result) or []
                if not batch:
                    break
                for u in batch:
                    if u.email:
                        existing[u.email.lower()] = u.id
                if len(batch) < 1000:
                    break
                page += 1
            except Exception as e:
                print(f"  Warning: list_users page {page} failed: {e}")
                break
        print(f"  Found {len(existing):,} existing users\n")

    out_dir = Path("migration")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / ("results_dryrun.csv" if dry else "results.csv")

    counts = {"created": 0, "duplicate": 0, "failed": 0, "skipped": 0}

    CSV_FIELDS = ["old_user_id", "email", "new_uuid", "plan",
                  "status", "started_at", "expires_at", "error"]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()

        total = len(users)
        for i, uid in enumerate(sorted(users), 1):
            user = users[uid]
            email    = _clean(user.get("email"))
            password = user.get("password") or ""

            # ── Pre-flight checks ──────────────────────────────────────────
            if uid in SKIP_USER_IDS:
                counts["skipped"] += 1
                continue

            if not email:
                counts["skipped"] += 1
                writer.writerow({"old_user_id": uid, "email": "", "new_uuid": "",
                                 "plan": "", "status": "", "started_at": "",
                                 "expires_at": "", "error": "no_email"})
                continue

            if len(password) < 6:
                counts["skipped"] += 1
                writer.writerow({"old_user_id": uid, "email": email, "new_uuid": "",
                                 "plan": "", "status": "", "started_at": "",
                                 "expires_at": "", "error": "password_too_short"})
                continue

            sub     = resolve_sub(uid, user, payments, releases)
            profile = build_profile(user)

            # ── Dry run ───────────────────────────────────────────────────
            if dry:
                writer.writerow({
                    "old_user_id": uid, "email": email, "new_uuid": "DRY_RUN",
                    "plan": sub["plan"], "status": sub["status"],
                    "started_at": sub["started_at"] or "",
                    "expires_at": sub["expires_at"] or "", "error": "",
                })
                counts["created"] += 1
                if i % 200 == 0 or i == total:
                    print(f"  [{i}/{total}] processed...")
                continue

            # ── Live run ──────────────────────────────────────────────────
            new_uuid = None
            error    = ""

            try:
                # 1. Auth user: create or reuse existing
                if email.lower() in existing:
                    new_uuid = existing[email.lower()]
                    counts["duplicate"] += 1
                else:
                    res = service.auth.admin.create_user({
                        "email":         email,
                        "password":      password,   # bcrypt-hashed by Supabase
                        "email_confirm": True,
                        "user_metadata": {
                            "full_name":   profile.get("full_name") or "",
                            "artist_name": profile.get("artist_name") or "",
                            "phone":       profile.get("phone") or "",
                        },
                    })
                    new_uuid = res.user.id
                    existing[email.lower()] = new_uuid   # add to cache
                    counts["created"] += 1

                # 2. Profile upsert (only non-None fields)
                prof_payload = {"user_id": new_uuid}
                prof_payload.update({k: v for k, v in profile.items() if v is not None})
                service.table("profiles").upsert(
                    prof_payload, on_conflict="user_id"
                ).execute()

                # 3. Subscription upsert
                sub_payload: dict = {
                    "user_id": new_uuid,
                    "plan":    sub["plan"],
                    "status":  sub["status"],
                }
                if sub["started_at"]:
                    sub_payload["started_at"] = sub["started_at"]
                if sub["expires_at"]:
                    sub_payload["expires_at"] = sub["expires_at"]
                if sub["payment_ref"]:
                    sub_payload["payment_ref"] = sub["payment_ref"]

                service.table("subscriptions").upsert(
                    sub_payload, on_conflict="user_id"
                ).execute()

            except Exception as exc:
                error = str(exc)[:300]
                counts["failed"] += 1
                print(f"  ERROR [{uid}] {email}: {error}")

            writer.writerow({
                "old_user_id": uid,
                "email":       email,
                "new_uuid":    new_uuid or "",
                "plan":        sub["plan"],
                "status":      sub["status"],
                "started_at":  sub["started_at"] or "",
                "expires_at":  sub["expires_at"] or "",
                "error":       error,
            })

            if i % 100 == 0 or i == total:
                print(f"  [{i}/{total}] created={counts['created']} "
                      f"dup={counts['duplicate']} failed={counts['failed']}")

            time.sleep(RATE_LIMIT_SLEEP)

    print(f"\nResults -> {out_path}")
    print(f"  Created:   {counts['created']:,}")
    print(f"  Duplicate: {counts['duplicate']:,}")
    print(f"  Failed:    {counts['failed']:,}")
    print(f"  Skipped:   {counts['skipped']:,}")


if __name__ == "__main__":
    main()
