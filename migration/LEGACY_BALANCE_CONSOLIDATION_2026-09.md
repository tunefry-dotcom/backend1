# Legacy Balance Consolidation (2026-09-02)

One-time migration that pushed `Combined_Artist_Legacy_Report.xlsx`
(`C:\Users\ViditVaibhav\Desktop\tunefry reports\feb-april patched\Combined_Artist_Legacy_Report.xlsx`)
into the live site, so every migrated artist's Tunefry dashboard shows the
same total earnings / balance figures as the legacy consolidation report.

## Why

The legacy SQL Server system tracked artist earnings/withdrawals separately
from Tunefry's Supabase tables. `Combined_Artist_Legacy_Report.xlsx` (built
by `merge_artist_legacy_reports.py` from `Artist_Reports_v5.xlsx` + 2026 DSP
data and `Legacy_All_Users_Report.xlsx` + legacy SQL dump data) is the
source of truth for "what should this artist's total earnings and balance
actually be, combining legacy history with 2026 activity." Nothing on the
live site reflected that combined number before this migration — Supabase's
`artist_balances`/`song_stats` only had whatever `ingest_royalty_report.py`
had ingested from 2026 DSP reports, with no legacy pre-2026 history folded
in for most users.

## What was run, in order

### Step 1 — `migration/apply_combined_legacy_balances.py --live`

Wrote `public.artist_balances` (`total_earned`, `total_withdrawn`,
`available_balance`) directly from the report, for every row across all
three sheets (`Summary`, `Unmatched_V5_Artists`, `Legacy_Only_Users`),
matched to a real Supabase account by email.

- **2318** users matched and written; **41** unmatched (no such email in
  `auth.users`) — logged to `unmatched_combined_balances_20260902_200312.csv`.
- **420** of the 2318 had a pre-existing `artist_balances` row — their prior
  values were backed up to `backup_artist_balances_20260902_200312.csv`
  before being overwritten. The other 1898 had no prior row (new insert).
- Value mapping used everywhere in this migration: for each sheet,
  `total_earned` = that sheet's "total balance" column and
  `available_balance` = that sheet's "remaining balance" column
  (`Summary` → Combined Total/Remaining Balance; `Unmatched_V5_Artists` →
  Total Royalty for both; `Legacy_Only_Users` → Total Income / Remaining
  Balance). `total_withdrawn` is derived as
  `max(0, total_earned - available_balance)` since none of the sheets carry
  a withdrawn figure. Cross-sheet duplicate emails resolved by priority
  `Summary` > `Unmatched_V5_Artists` > `Legacy_Only_Users`.
- **Known, accepted limitation:** this is an unconditional overwrite. The
  report's data only covers legacy history through Feb-Apr 2026; it does not
  account for any real earnings/withdrawal activity between then and the
  migration date (2026-09-02) for these users. This was explicitly decided
  by the user rather than reconciled — see "Decisions" below.

### Step 2 — discovered bug: "Total Earning" on the dashboard didn't change

`GET /earnings/me` (`get_earnings_summary` in
`app/modules/earnings/service.py`) computes the artist-facing "Total
Earning" figure **live**, by summing `public.song_stats.revenue` for the
user (plus `referral_earnings`) — it never reads `artist_balances.total_earned`
at all. Only `GET /earnings/balance` (withdrawal balance) and the admin
panel read `artist_balances` directly. So Step 1 fixed the withdrawal
balance and the admin view, but not the headline number artists actually
see. Confirmed with "Ten K" (`iamtenk9@gmail.com`): `artist_balances`
showed the correct 15907.37 but the dashboard still showed 8928.43 (the
live `song_stats` sum, untouched by Step 1).

### Step 3 — `migration/replace_song_stats_with_combined_legacy.py --live`

Full wipe-and-replace of `public.song_stats` for the same 2318 matched
users, so the live `song_stats` sum equals the report's target too.

- **Backup taken first**: every existing `song_stats` row for the 2318
  matched users was dumped to
  `backup_song_stats_20260902_202650.csv` (**4591 rows**) before deletion.
  This is the only recovery path — there is no Supabase backup on the free
  plan.
- **Deleted**: all 4591 of those rows (**99% of the site's entire
  `song_stats` table**, which had 4644 rows total before this migration).
- **Inserted**: one replacement row per matched user — `song_title =
  "Legacy Import (pre-Sep 2026 consolidated)"`, `platform = "Legacy Import"`,
  `platform_group = "Other"`, `artist_name = "Legacy Import"` (placeholder;
  the report doesn't carry a clean per-user artist-name field usable here),
  `period_month`/`period_year` = the month/year the migration ran
  (September 2026), `streams = 0`, `revenue = total_earned_target -
  referral_earnings_for_that_email` (so that after adding referral earnings
  back in, the live sum equals the report's target exactly). Only 1 row
  existed in `referral_earnings` site-wide at migration time, so this
  adjustment was a no-op for all but one user.
- **Recomputed**: `app.modules.earnings.service.recompute_balance(email)`
  was called for all 2318 users afterward (not hand-rolled again), so
  `artist_balances.total_earned` = `sum(new song_stats) + referral` = the
  report's target, staying internally consistent with any future recompute.
- **First attempt failed mid-run**: the initial `--live` run deleted the
  4591 rows successfully (backup already written), then failed on insert
  because `song_stats.artist_name` is `NOT NULL` and the script passed
  `None`. This left the 2318 users with **zero** `song_stats` rows for a
  short window. Fixed by changing the placeholder to the string
  `"Legacy Import"` and re-running `--live` immediately — the re-run's
  backup/delete steps were no-ops (nothing left to back up or delete), and
  the insert + recompute completed the fix.

## Step 3's trade-off was rejected by the user — see Correction below

Step 3's flat "Legacy Import" line per user (described above as a "known,
permanent trade-off") was shown live on the Stats page and the user rejected
it: real per-song visibility (song title, streams, per-platform detail) is
required, not a single aggregate line. This is addressed below.

### Correction — `migration/rebuild_song_stats_from_artist_sheets.py --live`

**The mistake**: Step 3 assumed the report only had artist-level aggregate
figures (`Summary`/`Unmatched_V5_Artists`/`Legacy_Only_Users` sheets), so a
lump-sum replacement seemed like the only option. This was wrong —
`Combined_Artist_Legacy_Report.xlsx` also has **777 individual per-artist
sheets** (one per `Summary`-sheet matched artist), each with a full `SONG
BREAKDOWN` section: real `(Period, Song, Streams, Income, Redeemed,
Remaining)` rows for both the `Legacy` and `Feb-Apr 2026` periods, plus
`PLATFORM BREAKDOWN` and `MONTHLY BREAKDOWN` sections. Summing a sheet's
`SONG BREAKDOWN` Income column by period reproduces that artist's `Legacy
Total Income` / `Feb-Apr 2026 Royalty` exactly (verified: "I Am Ten K" —
Legacy rows sum to 15907.36 vs. declared 15907.37; "Krantiveer" — Feb-Apr
rows sum to 30080.04 exact). This per-song data was missed in the initial
analysis and should have been the source from the start.

**What the correction does**:
- Deletes the Step 3 flat placeholder rows (and any other `song_stats` rows
  still present) for the 2318 matched users — backed up first to
  `backup_song_stats_rebuild_<ts>.csv`.
- For the **411 distinct emails** that own one or more of the 777 individual
  artist sheets (many accounts own 2+ sheets — label-style accounts
  uploading under multiple artist pseudonyms, e.g. one email owned 30
  different artist-name sheets; all songs across all of an email's sheets
  are merged together, not just the last one parsed), inserts the REAL
  per-song rows: song title, streams, and income exactly as reported, tagged
  with the owning artist name, `platform_group = "Other"` (the source
  doesn't join per-song data to a specific platform), and `period_month`/
  `period_year` set to the most recent real month found in that artist's
  `MONTHLY BREAKDOWN` for that period bucket (exact song-to-month mapping
  isn't recoverable from the source file, so the latest real month in the
  bucket is used rather than fabricating false precision).
- If a tiny residual gap remains after real songs are summed (rounding, or
  a user's referral earnings need netting out), one `"Legacy Balance
  Adjustment"` row closes it — usually $0 since the real song rows already
  sum to the exact target by construction.
- For the remaining **~1910 matched users** who were only ever in the
  `Legacy_Only_Users` sheet (no individual artist sheet exists for them in
  this workbook — they had no 2026 activity to combine against), one
  honestly-labeled `"Legacy Consolidated (no per-song detail available)"`
  row is used — this is a genuine limitation of the source file, not a
  shortcut: no per-song data exists anywhere in the report for this group.
- Recomputes `artist_balances` via `recompute_balance()` for all 2318 users
  afterward, same as Step 3.

**Bug caught mid-implementation (fixed before any live write)**: the first
draft of this script keyed parsed sheet data by email in a plain dict,
silently overwriting when an email owned multiple sheets — 777 sheets
collapsed to only 411 "found" entries, each keeping just the last sheet's
songs and discarding the rest. Caught via dry-run (`parsed 777 sheets -> 411
distinct emails`) before any write; fixed by merging all of an email's
sheets' songs together instead of keeping only one.

## Files produced (all in `migration/`)

| File | What it is |
|---|---|
| `apply_combined_legacy_balances.py` | Script for Step 1 (artist_balances overwrite) |
| `replace_song_stats_with_combined_legacy.py` | Step 3 (song_stats flat-line replace) — **superseded by the Correction below; kept for history, do not re-run** |
| `rebuild_song_stats_from_artist_sheets.py` | The Correction — rebuilds `song_stats` from the workbook's real per-song `SONG BREAKDOWN` data |
| `unmatched_combined_balances_20260902_200312.csv` | 41 report rows whose email matched no real Supabase account (Step 1) |
| `backup_artist_balances_20260902_200312.csv` | 420 users' pre-Step-1 `artist_balances` values |
| `unmatched_song_stats_replace_20260902_202650.csv` | Same 41 unmatched rows, re-logged by Step 3 |
| `backup_song_stats_20260902_202650.csv` | **4591 rows** — every real `song_stats` row that existed before Step 3 (i.e. before ANY of this migration touched song_stats), for all 2318 matched users. This is the deepest recovery point if anything below needs to be undone entirely. |
| `unmatched_rebuild_song_stats_<ts>.csv` | Same 41 unmatched emails, re-logged by the Correction |
| `backup_song_stats_rebuild_<ts>.csv` | Snapshot of the Step-3 flat placeholder rows immediately before the Correction replaced them with real per-song data |

## Decisions made during this migration (for future reference)

- **Row scope**: include all three sheets (`Summary`, `Unmatched_V5_Artists`,
  `Legacy_Only_Users`), not just `Summary`'s 777 matched rows — user's
  explicit choice, to cover every artist the report has a number for.
- **`total_withdrawn`**: derived as `total_earned - available_balance`
  rather than left untouched, so the three numbers stay internally
  consistent — user's explicit choice.
- **Post-report activity (May-Sep 2026)**: overwrite unconditionally, do
  not attempt to detect/preserve newer real earnings or withdrawals for
  these users — user's explicit choice, after the risk was surfaced.
- **`song_stats` full wipe vs. non-destructive top-up**: user explicitly
  chose the full wipe (destroys real per-song history, replaced by one
  lump row) over a non-destructive alternative (keep real rows, add one
  adjustment row per user for the gap) — see "Known, permanent trade-off"
  above.
