"""One-time fix: correct status on migrated submissions from legacy IsActive/error.

The original import (migrate_releases.py) hardcoded status='approved' for EVERY
legacy release. The legacy ReleaseDetails table has no status column, but IsActive
(1=live, 0=not live) is a usable signal and `error` holds real rejection reasons on
a handful of rows. This re-derives the correct status by joining back to the legacy
SQL dump on data.legacy_release_id.

Mapping (user-confirmed):
    IsActive == '0'          -> declined
    IsActive == '1' or NULL  -> approved
For declined rows that also have an `error` reason, the reason is preserved in
admin_note.

Idempotent and re-runnable. Run from repo root:
    python migration/fix_migrated_status.py --dry-run
    python migration/fix_migrated_status.py
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

from app.core.supabase_client import get_service_client
# Reuse the exact SQL-dump parser the original import used.
from migration.migrate_releases import parse_table

SQL_FILE = r"C:\Users\ViditVaibhav\Downloads\table with data.sql"
MIGRATED_NOTE = "Migrated from legacy system"
PAGE = 1000        # submissions fetch page size
ID_CHUNK = 200     # row-ids per bulk update call


def build_legacy_map() -> dict[str, dict[str, str | None]]:
    """{ ReleaseID(str) : {"is_active": '0'|'1'|None, "error": str|None} }."""
    print("Reading legacy SQL dump (UTF-16, ~200 MB — may take ~30 s) …")
    sql_text = open(SQL_FILE, encoding="utf-16").read()
    rows = parse_table(sql_text, "dbo", "ReleaseDetails",
                        ["ReleaseID", "IsActive", "error"])
    out: dict[str, dict[str, str | None]] = {}
    for r in rows:
        rid = (r.get("ReleaseID") or "").strip()
        if rid:
            out[rid] = {"is_active": r.get("IsActive"), "error": r.get("error")}
    print(f"  Parsed {len(out)} legacy releases")
    return out


def derive_status(is_active: str | None) -> str:
    return "declined" if is_active == "0" else "approved"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Correct migrated submission statuses from legacy IsActive/error")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change without touching the DB")
    args = parser.parse_args()
    dry_run: bool = args.dry_run

    legacy = build_legacy_map()
    svc = get_service_client()

    # Collect intended changes across all migrated rows (paginated).
    to_approved: list[str] = []          # submission row ids
    to_declined: list[str] = []          # submission row ids
    note_updates: list[tuple[str, str]] = []  # (row id, new admin_note)
    scanned = unmatched = 0
    offset = 0

    while True:
        res = (svc.table("submissions")
               .select("id, data, status, admin_note")
               .like("admin_note", f"{MIGRATED_NOTE}%")
               .range(offset, offset + PAGE - 1)
               .execute())
        rows = res.data or []
        if not rows:
            break
        scanned += len(rows)

        for row in rows:
            rid = str((row.get("data") or {}).get("legacy_release_id") or "").strip()
            info = legacy.get(rid)
            if not info:
                unmatched += 1
                continue

            target = derive_status(info["is_active"])
            if row.get("status") != target:
                (to_declined if target == "declined" else to_approved).append(row["id"])

            # Preserve the rejection reason on declined rows only.
            err = (info["error"] or "").strip()
            if target == "declined" and err:
                new_note = f"{MIGRATED_NOTE} — {err}"
                if row.get("admin_note") != new_note:
                    note_updates.append((row["id"], new_note))

        if len(rows) < PAGE:
            break
        offset += PAGE

    def bulk_status(ids: list[str], status: str) -> None:
        for i in range(0, len(ids), ID_CHUNK):
            chunk = ids[i:i + ID_CHUNK]
            if not dry_run:
                svc.table("submissions").update({"status": status}).in_("id", chunk).execute()

    if not dry_run:
        bulk_status(to_declined, "declined")
        bulk_status(to_approved, "approved")
        for row_id, note in note_updates:
            svc.table("submissions").update({"admin_note": note}).eq("id", row_id).execute()

    tag = "[DRY RUN] " if dry_run else ""
    print(f"\n{tag}Done.")
    print(f"  scanned={scanned}  "
          f"changed_to_approved={len(to_approved)}  changed_to_declined={len(to_declined)}  "
          f"error_notes={len(note_updates)}  unmatched={unmatched}")


if __name__ == "__main__":
    main()
