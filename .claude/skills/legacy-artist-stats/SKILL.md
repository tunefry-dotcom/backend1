---
name: legacy-artist-stats
description: Three related tools — (1) query a single artist's legacy earnings/streams/balance from the SQL dump, (2) generate a full per-individual-artist Excel royalty report from Royalty_Report.xlsx cross-referenced with the SQL dump and reference identities, (3) generate a user-wise, withdrawal-aware Excel report for every account in the raw SQL dump. Use (1) for specific "what did X earn" questions; use (2) when the user asks to generate or regenerate the full Artist_Reports xlsx (2026 DSP data); use (3) when the user asks for a report across all users/accounts sourced purely from the legacy dump, with explicit total income vs. remaining balance.
---

# Legacy artist stats

Three distinct tools live under `migration/`:

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

> **Standing policy — multi-artist conflict resolution (2026-09-02, permanent
> default, do not ask the user about this again):** when 2+ credited artists
> on a track are both registered Tunefry accounts, the artist listed **first**
> in the original artist-credit string is silently assigned the full
> royalty/stats; the other registered co-artist(s) get nothing from that
> track. This is baked into `aggregate()` in the script itself (not a flag),
> so every future run behaves this way automatically. The output has **no
> Conflicts sheet and no conflict banners** — do not reintroduce them unless
> the user explicitly asks to change this policy.

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

3. **Identity resolution pass** (done upfront, before attribution, for every
   unique individual artist name across all rows):
   - Try exact case-insensitive match in reference file (Username or FullName).
   - If not found: exact match in SQL dump (ArtistName > Username > FullName).
   - If not found: substring match (≥ 4 chars) in SQL dump.
   - If still not found: `source = "Name Only"`, identity fields blank.
   An artist is **registered** if their source is `"Reference File"` or
   `"Legacy SQL Dump"` (not `"Name Only"`).

4. **Attribute each row** using Tunefry-account-aware rules:
   - **Single-artist row** → always attributed to that one artist, regardless
     of registration status.
   - **Multi-artist row, exactly 1 registered** → only that registered artist
     gets the royalty/streams; unregistered co-artists get nothing.
   - **Multi-artist row, 2+ registered** → the artist listed **first** in the
     original artist-credit string (left to right, before any splitting) is
     assigned the full royalty/streams; the other registered co-artist(s) get
     **nothing** from that row. This is deterministic and fully automatic —
     there is always exactly one "first" name, so nothing is ever left
     ambiguous. There is **no Conflicts sheet** in the output; this
     resolution happens silently during aggregation.
   - **Multi-artist row, 0 registered** → every name-only individual gets the
     royalty (no account dispute possible, so no need to pick one).
   Because of this rule, the sum across all artist sheets is very close to
   the report total (small residual delta comes only from the 0-registered
   case above, which is an intentional, separate double-count — not a bug).

5. **Accumulate per group.** For each artist: running streams, royalty, per-song
   breakdown (with distributor / sub_label), per-platform breakdown, per-month
   breakdown.

6. **Build workbook.** Tab order: **Summary** first (all artists sorted by
   royalty desc, with Identity Source), then one sheet per artist — no
   Conflicts tab, no per-artist conflict banner. Per-artist sheets include
   identity block, overall stats, song breakdown (Song | Full Artist Credit |
   Distributor | Streams | Royalty | Balance), platform breakdown, monthly
   breakdown. `total_balance = remaining_balance = total_royalty` — no
   withdrawal deductions.

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

### Expected output (current data, Feb–Apr 2026)

| Metric | Value |
|--------|-------|
| Unique artists with royalty (sheets) | 844 |
| Reference File matches | 44 |
| Legacy SQL Dump matches | ~728 |
| Name Only | ~72 |
| Report total (INR) | 1,26,361.52 |
| Sum across all artist sheets | ~1,26,370.58 — tiny residual delta (~₹9) from the 0-registered "all name-only get it" rule, not from conflicts (those are fully resolved, zero double-count) |
| Auto-resolved 2+-registered rows | 16,046 rows |
| Output sheets | 1 Summary + 844 artist sheets (no Conflicts tab) |

### If the save fails with PermissionError

The output file is open in Excel. Either close it or pass a new `--output` path:
```bash
python migration/generate_artist_reports.py --output "C:\Users\ViditVaibhav\Desktop\Artist_Reports_v2.xlsx"
```

---

## Tool C — Batch per-user report from the raw dump (`generate_legacy_user_reports.py`)

Generates `Legacy_All_Users_Report.xlsx`: one Summary sheet + one sheet per
**account** (every `dbo.Users` row) + one `Unmatched_Names` review sheet.
Unlike Tool B, this is sourced **entirely from the legacy SQL dump** (no
`Royalty_Report.xlsx`), and is withdrawal-aware like Tool A.

> **Standing policy — user-wise, not label-wise (2026-09-02, permanent
> default, do not ask the user about this again):** stream rows are grouped
> by the **account** (`UserID`), never by the raw `MusicStreams.ArtistName`
> string. One account can appear under multiple name spellings in the dump
> (e.g. `"Rahul Sharma"` and `"RS Official"` for the same `Users` row) — all
> of them are merged into that one account's totals/sheet. A name that
> matches 2+ *different* accounts is never guessed; those rows are routed to
> the `Unmatched_Names` sheet with the candidate accounts listed. Every
> `Users` row gets a sheet, even with zero matched stream rows. **Total
> Income** and **Remaining Balance** are always separate, explicit columns
> — they diverge for any account with a prior withdrawal — never collapse
> them into one number the way Tool B does.

### How the resolution works

1. Parse `dbo.Users`, `dbo.MusicStreams` (drop `IsDeleted='1'` rows first),
   `dbo.WithdrawalHistory` via the same `load_sql`/`parse_table` helpers
   Tool A uses.
2. Build a `name (lowercased) → {UserID, ...}` map from every user's
   `ArtistName`, `Username`, and `FullName`.
3. For each **distinct** `MusicStreams.ArtistName` string, resolve it:
   - Exactly one candidate `UserID` → all rows with that string are merged
     into that account.
   - Zero or 2+ candidates → routed to `Unmatched_Names` (`reason` =
     `no_match` or `ambiguous`; ambiguous rows list every candidate as
     `UserID: ArtistName` so a reviewer can route manually without a second
     lookup).
4. Per account: `total_income` (sum `Revenue`), `total_redeemed` (sum
   `RedeemedAmount`), `remaining_balance = total_income − total_redeemed`
   (same formula as Tool A — `RedeemedAmount` is the per-row withdrawal
   allocation marker), plus song/platform/monthly breakdowns and that
   account's full `WithdrawalHistory`.

### Output layout

- **Summary**: one row per account — `#, UserID, Artist Name, Username, Full
  Name, Email, Matched Name Variants, Total Streams, Total Income (INR),
  Total Redeemed/Withdrawn (INR), Remaining Balance (INR)` — sorted by
  income desc.
- **Per-account sheet**: Identity block (UserID + Artist Name + Username +
  Full Name + Email + matched variants) → Overall Stats (4 separate rows:
  streams, income, redeemed, remaining) → Withdrawal History table → Song /
  Platform / Monthly breakdowns. Every section header restates `UserID ·
  ArtistName` so a single exported sheet is self-identifying.
- **Unmatched_Names**: raw ArtistName, reason, candidate `UserID: ArtistName`
  pairs, streams, income — for manual review, never silently dropped.

### Running it

```bash
python migration/generate_legacy_user_reports.py
```

Custom paths:
```bash
python migration/generate_legacy_user_reports.py "path/table with data.sql" \
    --output "path/Legacy_All_Users_Report.xlsx"
```

Default output: `C:\Users\ViditVaibhav\Desktop\tunefry reports\feb-april patched\Legacy_All_Users_Report.xlsx`.
Sheet count is large (~1 per `Users` row, i.e. 2,000+) since every account
gets a sheet — this is expected, not a bug. Same `PermissionError` caveat as
Tool B applies if the output file is open in Excel.
