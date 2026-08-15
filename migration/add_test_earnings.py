"""One-time: inject 3000 INR test earnings for theoutlaw566@gmail.com."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from app.core.supabase_client import get_service_client  # noqa: E402

EMAIL = "theoutlaw566@gmail.com"
NOW = datetime.now(timezone.utc).isoformat()

DRY_RUN = "--dry-run" in sys.argv


def main() -> None:
    song_row = {
        "user_email": EMAIL,
        "submission_id": None,
        "artist_name": "Test Artist",
        "song_title": "Test Song",
        "platform": "Spotify",
        "platform_group": "Spotify",
        "period_month": "August",
        "period_year": 2026,
        "streams": 10000,
        "revenue": "3000.0000000000",
        "updated_at": NOW,
    }
    balance_row = {
        "user_email": EMAIL,
        "total_earned": "3000.0000000000",
        "total_withdrawn": "0.0000000000",
        "available_balance": "3000.0000000000",
        "last_updated": NOW,
    }

    if DRY_RUN:
        print("DRY RUN — no writes performed.")
        print("song_stats row:", song_row)
        print("artist_balances row:", balance_row)
        return

    svc = get_service_client()

    svc.table("song_stats").upsert(
        song_row,
        on_conflict="user_email,song_title,platform,period_month,period_year",
    ).execute()
    print("song_stats upserted.")

    svc.table("artist_balances").upsert(
        balance_row,
        on_conflict="user_email",
    ).execute()
    print("artist_balances upserted.")

    print(f"Done — ₹3000 test earnings set for {EMAIL}")


if __name__ == "__main__":
    main()
