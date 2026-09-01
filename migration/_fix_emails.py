"""One-time fix: lowercase user_email on all migrated submissions (paginated)."""
import sys
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()
from app.core.supabase_client import get_service_client

svc = get_service_client()

PAGE = 1000
offset = 0
total_rows = 0
total_fixed = 0

while True:
    res = (svc.table("submissions")
           .select("id, user_email")
           .eq("admin_note", "Migrated from legacy system")
           .range(offset, offset + PAGE - 1)
           .execute())
    rows = res.data or []
    if not rows:
        break

    total_rows += len(rows)
    needs_fix = [(r["id"], r["user_email"]) for r in rows if r["user_email"] != r["user_email"].lower()]

    for row_id, email in needs_fix:
        svc.table("submissions").update({"user_email": email.lower()}).eq("id", row_id).execute()

    total_fixed += len(needs_fix)
    print(f"  offset={offset}: {len(rows)} rows, {len(needs_fix)} fixed")

    if len(rows) < PAGE:
        break
    offset += PAGE

print(f"\nDone. Total migrated rows scanned: {total_rows}, emails lowercased: {total_fixed}")
