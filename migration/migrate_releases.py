from __future__ import annotations
import sys, re, argparse
from datetime import datetime, timezone

sys.path.insert(0, ".")
from app.core.supabase_client import get_service_client

SQL_FILE = r"C:\Users\ViditVaibhav\Downloads\table with data.sql"
BATCH_SIZE = 500   # rows per Supabase insert call

_CAST_RE = re.compile(r"^CAST\((.+?) AS \w+\)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# SQL parsing helpers
# ---------------------------------------------------------------------------

def _closing_paren(s: str) -> int:
    """Return index of the ) that closes VALUES( — skips nested CAST/func parens."""
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
    """Tokenise a SQL VALUES list (without outer parens).

    Handles: NULL, N'unicode', '' escaped quotes, integers, dates, and
    CAST(N'...' AS Type) — including types that themselves contain a comma
    such as Decimal(18, 10). Parenthesis depth is tracked so a comma inside
    a CAST type does NOT start a new token; only commas at depth 0 (and
    outside strings) split.
    """
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

    SSMS exports put each INSERT on one line, BUT nvarchar(max) fields can hold
    user-entered newlines (e.g. a multi-line bio), which split a single row
    across several physical lines. We reassemble those: once a line matches the
    prefix, keep appending following lines until the buffer has balanced single
    quotes (escaped '' stay even) AND ends with ')'. Only matching rows ever
    accumulate, so unrelated huge tables (ErrorLog stack traces) are never
    joined — avoiding the O(n²) concat the original single-line parser dodged.
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


def parse_table(sql_text: str, schema: str, table: str,
                wanted: list[str]) -> list[dict]:
    """Extract rows from INSERT statements for [schema].[table].

    SQL Server SSMS exports use  INSERT [schema].[table] (cols) VALUES (vals)
    — no INTO keyword. Rows with embedded newlines are reassembled via
    iter_insert_statements so they aren't silently dropped.
    """
    prefix = f"INSERT [{schema}].[{table}]".upper()
    rows: list[dict] = []

    for line in iter_insert_statements(sql_text.splitlines(), prefix):
        try:
            up = line.upper()
            col_open  = line.index("(") + 1
            val_kw    = up.index(") VALUES")
            col_names = [c.strip().strip("[]")
                         for c in line[col_open:val_kw].split(",")]

            val_open  = up.index("VALUES (") + len("VALUES (")
            vals_raw  = line[val_open:].rstrip()
            vals_str  = vals_raw[:_closing_paren(vals_raw)]

            vals = parse_values(vals_str)
            if len(vals) != len(col_names):
                continue

            row = dict(zip(col_names, vals))
            rows.append({k: row.get(k) for k in wanted})
        except (ValueError, IndexError):
            continue

    return rows


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def build_supabase_map() -> dict[str, str]:
    """Return {email.lower(): supabase_uuid} for all auth users."""
    svc = get_service_client()
    result: dict[str, str] = {}
    page = 1
    while True:
        resp = svc.auth.admin.list_users(page=page, per_page=1000)
        users = (resp.users if hasattr(resp, "users")
                 else resp if isinstance(resp, list) else [])
        for u in users:
            email = getattr(u, "email", None) or (u.get("email") if isinstance(u, dict) else None)
            uid   = getattr(u, "id",    None) or (u.get("id")    if isinstance(u, dict) else None)
            if email and uid:
                result[email.lower()] = uid
        if len(users) < 1000:
            break
        page += 1
    print(f"  Loaded {len(result)} Supabase users")
    if not result:
        raise SystemExit("ERROR: 0 Supabase users loaded — check SERVICE_ROLE_KEY in .env")
    return result


# ---------------------------------------------------------------------------
# Data mapping
# ---------------------------------------------------------------------------

def strip_cast(v: str | None) -> str:
    """Remove CAST(value AS Type) wrapper produced by SSMS exports."""
    if not v:
        return ""
    v = v.strip()
    m = _CAST_RE.match(v)
    return m.group(1).strip() if m else v


def build_data(row: dict, sub_type: str) -> dict:
    def s(key: str) -> str:
        return strip_cast(row.get(key) or "").strip()

    data: dict = {
        "submission_type":           sub_type,
        "song_title":                s("SongTitle"),
        "main_artists":              [{"name": s("Artist")}]   if s("Artist")   else [],
        "featured_artists":          [{"name": s("FtArtist")}] if s("FtArtist") else [],
        "isrc":                      s("Isrc"),
        "upc":                       s("Upc"),
        "release_date":              s("DateOfMusicRelease") or s("GoLiveDate"),
        "spotify_url":               s("SpotifyLink"),
        "apple_music_url":           s("AppleLink"),
        "youtube_url":               s("YoutubeLink"),
        "amazon_url":                s("AmazonLink"),
        "legacy_release_id":         s("ReleaseID"),
        "notes":                     s("MessageToAdmin"),
        # Cover art: filename only — Drive uploads all failed (quota exceeded).
        # Retrieve Upload/ from old IIS server (d:\inetpub\vhosts\tunefry.com\httpdocs\Upload\)
        # and run a separate script to push files to R2.
        "cover_art_legacy_filename": s("ArtworkUrl"),
        "audio_legacy_filename":     s("AudioFileUrl"),
    }
    return {k: v for k, v in data.items() if v not in (None, "", [], {})}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate legacy ReleaseDetails → public.submissions"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be inserted without touching the DB")
    args = parser.parse_args()
    dry_run: bool = args.dry_run

    print("Reading SQL file (197 MB — may take ~30 s) …")
    sql_text = open(SQL_FILE, encoding="utf-16").read()

    old_users = parse_table(sql_text, "dbo", "Users", ["UserID", "Email"])
    releases  = parse_table(sql_text, "dbo", "ReleaseDetails", [
        "ReleaseID", "UserID", "SongTitle", "Artist", "FtArtist",
        "ArtworkUrl", "AudioFileUrl", "Isrc", "Upc",
        "DateOfMusicRelease", "GoLiveDate", "ReleaseType",
        "MessageToAdmin", "SpotifyLink", "AppleLink",
        "YoutubeLink", "AmazonLink", "IsActive", "error",
    ])
    print(f"Parsed {len(old_users)} users, {len(releases)} releases from SQL file")

    user_email_map: dict[int, str] = {
        int(float(u["UserID"])): (u.get("Email") or "").strip()
        for u in old_users if u.get("UserID")
    }

    supabase_map = build_supabase_map()   # email.lower() → supabase_uuid

    batch: list[dict] = []
    inserted = skipped = skipped_existing = 0
    svc = get_service_client()

    # Idempotency: collect legacy_release_ids already imported so a re-run only
    # backfills new rows instead of duplicating the whole catalogue.
    existing_ids: set[str] = set()
    offset = 0
    while True:
        res = (svc.table("submissions").select("data")
               .like("admin_note", "Migrated from legacy system%")
               .range(offset, offset + 999).execute())
        page = res.data or []
        for r in page:
            lid = str((r.get("data") or {}).get("legacy_release_id") or "").strip()
            if lid:
                existing_ids.add(lid)
        if len(page) < 1000:
            break
        offset += 1000
    print(f"  {len(existing_ids)} releases already migrated — will skip those")

    def flush(force: bool = False) -> None:
        nonlocal inserted
        if not batch or (not force and len(batch) < BATCH_SIZE):
            return
        if not dry_run:
            svc.table("submissions").insert(batch).execute()
        inserted += len(batch)
        tag = "[DRY RUN] " if dry_run else ""
        print(f"  {tag}Flushed {len(batch)} rows (total so far: {inserted})")
        batch.clear()

    now = datetime.now(timezone.utc).isoformat()

    for row in releases:
        uid_raw = row.get("UserID") or "0"
        try:
            uid_int = int(float(uid_raw))
        except (ValueError, TypeError):
            skipped += 1
            continue

        rid = (row.get("ReleaseID") or "").strip()
        if rid and rid in existing_ids:
            skipped_existing += 1
            continue

        email   = user_email_map.get(uid_int, "")
        sb_uuid = supabase_map.get(email.lower()) if email else None

        if not sb_uuid:
            skipped += 1
            continue

        sub_type = ("new_album"
                    if "album" in (row.get("ReleaseType") or "").lower()
                    else "new_song")

        # Legacy has no status column; IsActive (0=not live) is the signal.
        status = "declined" if row.get("IsActive") == "0" else "approved"
        err = (strip_cast(row.get("error")) or "").strip()
        admin_note = ("Migrated from legacy system — " + err
                      if status == "declined" and err
                      else "Migrated from legacy system")

        batch.append({
            "user_email":      email,
            "user_plan":       "free",
            "submission_type": sub_type,
            "status":          status,
            "data":            build_data(row, sub_type),
            "admin_note":      admin_note,
            "reviewed_at":     now,
        })

        if len(batch) >= BATCH_SIZE:
            flush()

    flush(force=True)

    print(f"\n{'DRY RUN — ' if dry_run else ''}Done.")
    print(f"  inserted={inserted}  skipped(no user match)={skipped}  "
          f"skipped(already migrated)={skipped_existing}")
    print()
    print("  NOTE: cover art filenames are stored as data.cover_art_legacy_filename")
    print("        To complete cover art migration, retrieve the Upload/ folder from")
    print("        the old IIS server (d:\\inetpub\\vhosts\\tunefry.com\\httpdocs\\Upload\\)")
    print("        and run a separate script to push each file to R2.")


if __name__ == "__main__":
    main()
