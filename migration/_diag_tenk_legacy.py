import sys
sys.path.insert(0, ".")
from collections import defaultdict
from decimal import Decimal
from dotenv import load_dotenv; load_dotenv()

from app.core.supabase_client import get_service_client
from migration.migrate_releases import parse_table
from migration.ingest_streams import (
    norm, to_decimal, to_int, normalize_platform, ADJUSTMENT_PLATFORM,
    load_migrated_submissions,
)

SQL_FILE = r"C:\Users\ViditVaibhav\Downloads\table with data.sql"
EMAIL = "iamtenk9@gmail.com"

print("Reading SQL dump (UTF-16) ...")
sql_text = open(SQL_FILE, encoding="utf-16").read()

streams = parse_table(sql_text, "dbo", "MusicStreams",
                       ["ArtistName", "Song", "Streams", "Revenue",
                        "Month", "Year", "Platform", "IsDeleted"])
releases = parse_table(sql_text, "dbo", "ReleaseDetails",
                        ["ReleaseID", "Song", "SongTitle", "Artist"])
users = parse_table(sql_text, "dbo", "Users",
                     ["UserID", "Email", "ArtistName", "FullName", "Username"])
withdrawals = parse_table(sql_text, "dbo", "WithdrawalHistory", ["UserId", "Amount", "Status"])
print(f"Parsed {len(streams)} stream rows, {len(releases)} releases, {len(users)} users, {len(withdrawals)} withdrawals")

svc = get_service_client()
rid_to_sub = load_migrated_submissions(svc)

legacy_uid_email = {}
name_emails = defaultdict(set)
his_uids = []
for u in users:
    email = (u.get("Email") or "").strip().lower()
    if not email:
        continue
    try:
        uid = int(float(u["UserID"]))
        legacy_uid_email[uid] = email
    except (ValueError, TypeError, KeyError):
        uid = None
    for field in ("ArtistName", "FullName", "Username"):
        n = norm(u.get(field))
        if n:
            name_emails[n].add(email)
    if email == EMAIL:
        his_uids.append(uid)
        print("Legacy Users row for him:", u)

artist_to_email = {name: next(iter(emails)) for name, emails in name_emails.items() if len(emails) == 1}
his_names = {name for name, emails in name_emails.items() if EMAIL in emails}
print("Legacy names tied to his email:", his_names)

rd_key_to_rid = {}
for r in releases:
    rid = (r.get("ReleaseID") or "").strip()
    if not rid:
        continue
    artist = norm(r.get("Artist"))
    for title in {norm(r.get("Song")), norm(r.get("SongTitle"))}:
        if artist and title:
            rd_key_to_rid.setdefault((artist, title), rid)

def attribute(artist, song):
    rid = rd_key_to_rid.get((norm(artist), norm(song)))
    if rid and rid in rid_to_sub:
        sub_id, email = rid_to_sub[rid]
        return email, sub_id
    email = artist_to_email.get(norm(artist))
    return (email or None), None

agg = {}
withdrawn_adj = defaultdict(Decimal)
matched = unmatched = excluded_deleted = adjustments = 0
his_rows_seen = []

for row in streams:
    if row.get("IsDeleted") == "1":
        excluded_deleted += 1
        continue
    artist = row.get("ArtistName") or ""
    song = row.get("Song") or ""
    email, sub_id = attribute(artist, song)
    if not email:
        unmatched += 1
        continue
    matched += 1
    if email != EMAIL:
        continue
    his_rows_seen.append(row)

    platform_raw = row.get("Platform") or ""
    revenue = to_decimal(row.get("Revenue"))

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
        acc = {"user_email": email, "artist_name": artist.strip(), "song_title": song.strip(),
               "platform": canonical, "period_month": month, "period_year": year,
               "streams": 0, "revenue": Decimal("0")}
        agg[key] = acc
    acc["streams"] += to_int(row.get("Streams"))
    acc["revenue"] += revenue

print(f"\nTotal MusicStreams rows attributed to {EMAIL}: {len(his_rows_seen)}")
print(f"His song_stats-equivalent rows after aggregation: {len(agg)}")
for k, acc in sorted(agg.items(), key=lambda kv: (kv[1]['period_year'], kv[1]['period_month'])):
    print(" ", acc)

his_earned = sum((acc["revenue"] for acc in agg.values()), Decimal("0"))
print(f"\nTotal legacy-dump revenue (song_stats-style) for him: {his_earned}")
print(f"Legacy 'tunefry' adjustment (prior redemption) rows for him: {adjustments}, total={withdrawn_adj.get(EMAIL, Decimal('0'))}")

his_withdrawals = []
for w in withdrawals:
    try:
        uid = int(float(w["UserId"]))
    except (ValueError, TypeError, KeyError):
        continue
    if legacy_uid_email.get(uid) == EMAIL:
        his_withdrawals.append(w)
print(f"\nLegacy WithdrawalHistory rows for him: {len(his_withdrawals)}")
for w in his_withdrawals:
    print(" ", w)
