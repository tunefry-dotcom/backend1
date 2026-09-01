---
name: legacy-artist-stats
description: Two related tools — (1) query a single artist's legacy earnings/streams/balance from the SQL dump, (2) generate a full per-individual-artist Excel royalty report from Royalty_Report.xlsx cross-referenced with the SQL dump and reference identities. Use (1) for specific "what did X earn" questions; use (2) when the user asks to generate or regenerate the full Artist_Reports xlsx.
---

# Legacy artist stats

Two distinct tools live under `migration/`:

---

## Tool A — Single-artist lookup (`legacy_artist_stats.py`)

Runs `migration/legacy_artist_stats.py` — a read-only parser over the legacy
SQL Server SSMS export (`Users` / `MusicStreams` / `WithdrawalHistory`) — and
turns the JSON output into a markdown report. Never writes to the dump,
Supabase, or anywhere else.

## Parsing `$ARGUMENTS`

Arguments are free text. Extract:
- **artist name** (required) — everything that isn't clearly a month/year/path.
- **month + year** (optional) — must come as a pair. Accept a full month name
  ("March"), a number (1–12), or things like "March 2026". If the user gives a
  month with no year, ask which year before running (legacy data spans
  2025–2026 for known artists).
- **dump path** (optional) — only if the user gives one explicitly. Otherwise
  omit it and let the script use its built-in default
  (`C:\Users\ViditVaibhav\Downloads\table with data.sql`).

If the request only asks for something the script doesn't cover (e.g. FX
conversion, live Supabase balance), say so — this tool is legacy-dump-only.

## Running it

From the repo root:

```bash
python migration/legacy_artist_stats.py --artist "<name>" --json
```

Add month scoping if given:

```bash
python migration/legacy_artist_stats.py --artist "<name>" --month <Month> --year <YYYY> --json
```

Always use `--json` so the output can be parsed programmatically instead of
scraped from text.

## Handling multiple matches

If stdout starts with `Multiple users match '<name>'` followed by a list of
candidates (UserID, Username, FullName, Email), do **not** guess. Show the
candidate list to the user and ask which UserID they mean, unless one
candidate is an obvious exact match on the full name given (in which case
proceed with that one and mention the assumption). Re-run with `--user-id
<id>` once resolved.

If stdout says no user matched, tell the user the name wasn't found in the
legacy dump and suggest they check spelling or try a partial name.

If the JSON has `"matched_stream_artist_name": null` (or the equivalent
warning in text mode), the user exists in `Users` but has zero matching rows
in `MusicStreams` — report all stats as zero/none rather than treating it as
an error.

## Formatting the report

Build a markdown report from the JSON with these sections:

**Identity** — one line: `Username (FullName) — email`.

**All-time combined stats (all platforms)** — from `all_time`: total streams,
total revenue, total redeemed, remaining available balance. Then a
platform-wise table (`platforms[]`: platform, streams, revenue).

**Song-wise breakdown** — table from `all_time.songs[]`: song, streams,
revenue, redeemed, remaining. If multiple song names look like the same
release (e.g. `"Sachchai"` vs `"Sachchai [Explicit]"` vs `"SACHCHAI 2"`), note
that these are kept separate as distinct legacy rows and are **not**
auto-merged — flag it as a caveat rather than silently combining them.

**Balance** — remaining available balance = total revenue − total redeemed
across the scope in question. Also list `withdrawals[]` (WithdrawalHistory
rows: id, amount, status, dates) so the user can see what's already
requested — make clear `Pending` means requested-but-not-yet-paid, and is
already excluded from `remaining_balance` via the per-row `RedeemedAmount`
allocation (it's not double-subtracted, it's the same number).

**If a month/year was requested** — add a "Scoped: <Month> <Year>" section
using the `scoped` block (same shape as `all_time`), and highlight
`remaining_balance` for that month specifically since that's usually the
actual question. Also show the full `monthly_breakdown[]` table for context
(streams/revenue/redeemed/remaining per month) so the requested month's
numbers are visible alongside neighboring months.

**Caveats** (always include, briefly):
- This is legacy-system data as stored in the old dump — amounts are in the
  currency they were recorded in, no FX conversion applied.
- This is separate from current Supabase `song_stats` / `artist_balances` —
  don't conflate the two unless the user is asking specifically about
  pre-migration history.
- `remaining_balance` is derived (`revenue − redeemed`), not a stored column —
  it's a computed, not authoritative-ledger, number.

---

## Tool B — Batch per-individual-artist report (`generate_artist_reports.py`)

Generates `Artist_Reports.xlsx` on the Desktop: one Summary sheet + one sheet
per individual artist (~1,248 artists from the Feb–Apr 2026 royalty data).

### Data sources and how they combine

| Source | File | Role |
|--------|------|------|
| Royalty data | `C:\Users\ViditVaibhav\Desktop\Royalty_Report.xlsx` | Single source of truth for streams and INR royalty (all 2026) |
| Identity tier 1 | `C:\Users\ViditVaibhav\Desktop\tunefry reports\old reports\legacy_all_artists_report.xlsx` | 55 accounts from 2025 legacy reports — used **only** for UserID / Username / FullName / Email matching, never as a royalty source |
| Identity tier 2 | `C:\Users\ViditVaibhav\Downloads\table with data.sql` | dbo.Users fallback — 2,368 users, ArtistName → Username → FullName lookup |
| Identity tier 3 | — | Name Only — artist gets a sheet but no account identity |

### How the mapping works (step by step)

1. **Read `Combined_All`** (65,134 rows). Key columns: `artist`, `track_title`,
   `source_platform`, `quantity`, `royalty_inr`, `source_period`, `sub_label`.

2. **Split multi-artist rows.** Each `artist` cell is split on:
   - Two or more consecutive spaces
   - Comma (`, `)
   - Keywords: `featuring`, `feat.`, `ft.`, `x`, `&`, `and` (word-boundary, case-insensitive)

   Example: `"Lucky The Rapper  Yung Bleu"` → `["Lucky The Rapper", "Yung Bleu"]`.

3. **Attribute full royalty to every credited artist.** Both primary AND featured
   artists each receive 100 % of that row's `royalty_inr`. This means the sum
   of all artist totals exceeds the report total — the difference is the
   collaborative-track double-count (for the current data: 1,95,421 vs 1,26,362,
   a delta of ~68,059 INR from collaborative rows).

4. **Accumulate per group.** For each artist: running streams, royalty, per-song
   breakdown (with distributor / sub_label), per-platform breakdown, per-month
   breakdown.

5. **Identity resolution pass** (one pass after all rows are processed):
   - Try exact case-insensitive match in reference file (Username or FullName).
   - If not found: exact match in SQL dump (ArtistName > Username > FullName).
   - If not found: substring match (≥ 4 chars) in SQL dump.
   - If still not found: `source = "Name Only"`, identity fields blank.

6. **Build workbook.** Summary sheet lists all artists sorted by royalty desc,
   with Source column. Per-artist sheets include identity block, overall stats,
   song breakdown (Song | Full Artist Credit | Distributor | Streams | Royalty |
   Balance), platform breakdown, monthly breakdown.
   `total_balance = remaining_balance = total_royalty` — no withdrawal deductions.

### Running it

```bash
python migration/generate_artist_reports.py
```

Custom paths:
```bash
python migration/generate_artist_reports.py "path/Royalty_Report.xlsx" \
    --reference "path/legacy_all_artists_report.xlsx" \
    --dump "path/table with data.sql" \
    --output "path/Artist_Reports.xlsx" \
    --sheet "Combined_All"
```

### Expected output (current data)

| Metric | Value |
|--------|-------|
| Unique individual artists | ~1,248 |
| Reference File matches | 44 |
| Legacy SQL Dump matches | ~1,004 |
| Name Only | ~200 |
| Report total (INR) | 1,26,361.52 |
| Sum across all artist sheets | ~1,95,421 (higher due to collaborations) |
| Output sheets | 1 Summary + 1,248 artist sheets = 1,249 total |

### If the save fails with PermissionError

The output file is open in Excel. Either close it or pass a new `--output` path:
```bash
python migration/generate_artist_reports.py --output "C:\Users\ViditVaibhav\Desktop\Artist_Reports_v2.xlsx"
```
