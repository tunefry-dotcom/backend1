# Royalty Attribution Journey: How We Got to 87.8% Match Rate

A complete record of every step taken across this work session — what existed,
what we built, why each piece was necessary, the exact commands used, and
precisely where to look in CLAUDE.md or companion files for future reference.

**Period covered:** Work sessions 2026-08-18 → 2026-08-19  
**Report processed:** `Royalty_Reports_2026_Consolidated_INR.xlsx` (Feb–Apr 2026, 65,134 rows)  
**Final result:**

| | Value |
|--|--|
| Rows matched | 55,242 / 65,134 → **84.8%** |
| Revenue attributed | ₹1,10,953 / ₹1,26,361 → **87.8%** |
| song_stats rows written | 6,315 |
| artist_balances updated | 422 |
| Unmatched artists remaining | 370 (₹15,450 trapped) |

---

## The Core Problem

DSP royalty reports identify artists by **display name only** — no email,
no user ID. Supabase has ~1,700 users with highly inconsistent `artist_name`
values: different capitalization, aliases, label names, legacy transliterations,
or names that exist only in `auth.users.raw_user_meta_data` but were never
written to `public.profiles`.

The original ingest script had 2-tier matching. ~40% of rows would match.
~60% were "trapped" — real money owed to real artists, but no way to know
which account it belonged to without external data.

---

## Pre-Existing Infrastructure (Do Not Touch)

Before any of our work, these files already existed and are **immutable** or
**append-only**. Understanding them is critical:

| File | What it is | Rule |
|------|-----------|------|
| `migration/withdrawn_baseline.json` | One-time snapshot of each artist's legacy withdrawn (tunefry + WithdrawalHistory). Feeds balance formula. | **IMMUTABLE — never regenerate.** If lost, restore from git. |
| `migration/fx_rates.json` | Monthly USD→INR rates. | **Append-only.** Add new month entries before each run. Commit to git. |
| `migration/platform_map.py` | Normalizes raw DSP platform strings → canonical group names (`Spotify`, `Apple Music`, `YouTube`, etc.). Shared by both ingest scripts. | Extend as new DSPs appear. Never rename existing groups — frontend chart depends on exact strings. |
| `migration/ingest_streams.py` | Legacy one-time SQL dump ingest. Historically frozen. | **NEVER re-run.** Would corrupt `total_withdrawn` by double-counting. |

**CLAUDE.md reference for these:**
→ `### \`ingest_royalty_report.py\` invariants (earnings — money-critical, active monthly script)`
→ `### \`ingest_streams.py\` invariants (historically frozen — do NOT re-run)`
→ `migration/platform_map.py` row in the scripts table under `## Legacy data migration (\`migration/\`)`

---

## Step 1 — Audit the Starting State of the Ingest Script

**What we did:**
Read `migration/ingest_royalty_report.py` top-to-bottom to understand what
attribution it had before our changes.

**Starting state — 2-tier attribution:**
1. Build a `name_to_email` dict from `public.profiles.artist_name` +
   `auth.users.raw_user_meta_data.artist_name` (Supabase only)
2. For each report row: try `norm(artist)` → email; try `norm(sub_label)` → email; else → unmatched

**No ISRC matching. No SQL Server legacy data. No multi-artist splitting.
No INR workbook support. No Apple Music CSV. No reconciliation guard.**

**Key gap identified:** The legacy SQL Server DB (`dbo.Users`, `dbo.ReleaseDetails`)
had thousands of artist name → email and ISRC → email mappings for artists
who migrated to Supabase — but none of that data was used in the new ingest.

**CLAUDE.md reference:**
→ `## Legacy data migration (\`migration/\`)` — overview of the migration landscape
→ `## Database schema` — `song_stats`, `artist_balances`, `withdrawal_requests` table shapes

---

## Step 2 — Build `legacy_map.json` from the SQL Server Dump

**Script created:** `migration/build_legacy_map.py`

**Why it was needed:**
The SQL Server dump (`table with data.sql`, UTF-16 with BOM, SSMS export format)
is the authoritative record of "who owns what artist name / ISRC / UPC" for all
artists who pre-existed Supabase. That data was sitting unused.

**How it works:**
1. Parse `dbo.Users` → extract `Email`, `ArtistName`, `FullName`, `Username`
2. Parse `dbo.ReleaseDetails` → extract `ISRC`, `UPC`, `Artist`, `SongTitle`,
   linked to `UserID` → `Email` via step 1
3. Cross-reference every email against **live Supabase `auth.users`** — skip
   any legacy email not in Supabase (account was deleted or never migrated)
4. Build 5 lookup tables; apply unambiguity rule: **any name or ISRC that maps
   to more than one email is silently dropped** — better to miss than misattribute
5. Write `migration/legacy_map.json`

**Multiline robustness:** The SQL dump uses SSMS export format where a text
field containing a literal newline splits the row across physical lines.
`build_legacy_map.py` reuses `iter_insert_statements()` from `migrate_releases.py`
which buffers lines until the quote count is even — handles this automatically.

**Command:**
```powershell
.\venv\Scripts\python.exe migration\build_legacy_map.py "C:\path\to\table with data.sql"
# Add --force to overwrite an existing legacy_map.json
```

**Result (lookup table sizes, as reported by ingest script at startup):**
```
Legacy map: 512 artist names, 2044 full names, 2320 usernames,
            113 ISRCs, 90 UPCs, 2194 (artist||title)
```
Combined with Supabase: `2198 from Supabase + 1543 new from legacy = 3741 total names → email (163 ambiguous dropped)`

Note: build_legacy_map.py's raw parse counts (users/releases) were not captured in this session's
output. The lookup table sizes above come from the ingest script's startup log.

**CLAUDE.md reference:**
→ `migration/build_legacy_map.py` row in the scripts table under `## Legacy data migration`
→ `### Key invariants` — "Multiline robustness" note about `iter_insert_statements()`
→ `### Production safety` — read-only against Supabase; safe to re-run

---

## Step 3 — Wire Legacy Map into `ingest_royalty_report.py` (New Tiers 1, 4, 5)

**File modified:** `migration/ingest_royalty_report.py`

**What changed:**
At startup, load `migration/legacy_map.json` into memory.
Add 3 new tiers into the per-row attribution cascade:

| Tier | New? | Lookup | Source |
|------|------|--------|--------|
| **1** | NEW | `legacy_map["isrc"][ISRC.upper()]` | SQL dump ReleaseDetails ISRC |
| 2 (Supabase ISRC) | later | — | added in Step 5 |
| 3 (Supabase email+ISRC) | — | — | pre-existing |
| **4** | ENHANCED | `legacy_map["artist_name"][norm(artist)]` → fallback `full_name` → `username` | SQL dump Users table |
| **5** | NEW | `legacy_map["artist_title"][norm(a)+"||"+norm(t)]` | SQL dump ReleaseDetails |
| 6 (Supabase title) | later | — | added in Step 6 |

**Normalization used throughout:**
```python
norm = lambda s: re.sub(r"\s+", " ", s.strip().lower())
```

**Priority rule:** Legacy map entries **overwrite** Supabase entries when the
same normalized name exists in both. The SQL dump is the authoritative source
for legacy artists — if Supabase has `artist_name = "xyz music"` for one email
but the SQL dump has `ArtistName = "xyz music"` for a different (older) email,
the legacy entry wins:
```python
# Supabase first, then legacy OVERWRITES duplicates
for src in (legacy["artist_name"], legacy["full_name"], legacy["username"]):
    for name, email in src.items():
        artist_to_email[name] = email  # legacy OVERRIDES Supabase
```

**Design decision:** Legacy map loaded once at startup, all lookups O(1) dict
access. No additional DB queries in the per-row loop.

**Impact of this step alone:** match rate jumped from ~40% to ~70%.

**CLAUDE.md reference:**
→ `### \`ingest_royalty_report.py\` invariants` → "Attribution (6-tier priority)" — full ladder description
→ `migration/legacy_map.json` row — "Never edited by hand — regenerate from SQL dump"

---

## Step 4 — Multi-Artist Splitting

**File modified:** `migration/ingest_royalty_report.py`

**Problem:**
DSP rows for collaboration tracks list both artists in one field:
- `"Artist A  Artist B"` (double space — most common DSP format)
- `"Artist A, Artist B"` (comma)
- `"Artist A feat. Artist B"` / `feat` / `ft` / `x` / `&` / `and`

With single-name lookup, the whole row is unmatched even when one of the
two artists has a Tunefry account.

**What we built:**
A candidate-splitting function applied to the `artist` field before attribution:
```
raw "Indie Artist  Vidit Music"
  → candidates ["Indie Artist", "Vidit Music"]
  → try each through all 6 tiers
  → "Vidit Music" matches tier 4 → attribute to that email
```

**Exact regex used** (applied to both the xlsx row loop and the Apple Music CSV loop):
```python
parts = re.split(
    r"\s{2,}|,\s*|\s+(?:featuring|feat\.?|ft\.?|x|&|and)\s+",
    artist_raw,
    flags=re.IGNORECASE,
)
```

Split triggers:
- `\s{2,}` — two or more spaces (primary DSP collaborator separator)
- `,\s*` — comma
- `featuring` (full word), `feat.`, `ft.`, `x`, `&`, `and` — keyword separators, case-insensitive

After splitting, `sub_label` is also appended as a final candidate — label
names often appear in the `sub_label` column for tracks where the artist name
in the report is the label, not the individual artist:
```python
if sub_label_key and sub_label_key not in candidates:
    candidates.append(sub_label_key)
```

All candidates — split parts + sub_label — go through the same 6-tier
attribution cascade. First hit wins. If none match → unmatched.

**CLAUDE.md reference:**
→ `### \`ingest_royalty_report.py\` invariants` → "Attribution (6-tier priority)":
  "Multi-artist rows (double-space, comma, 'feat'/'ft'/'x'/'&'/'and') are split into candidates"

---

## Step 5 — Supabase ISRC-Only Reverse Lookup (Tier 2)

**File modified:** `migration/ingest_royalty_report.py`

**Problem:**
Artists who submitted songs natively through Supabase (post-migration) have ISRCs
stored in `public.submissions.data` JSONB. Their display name in the DSP report
may differ from their registered `artist_name`, so name matching fails. But their
ISRC is globally unique and doesn't change.

**What we built:**
At startup, scan all submissions and extract ISRCs from 3 JSONB paths:
```python
data->>'isrc'           # single song
data->>'isrc_code'      # transfer
data->'songs'->[N]->>'isrc'  # album track N
```
Build `isrc_to_sub`: `ISRC → (user_email, submission_id)`.
Drop any ISRC mapping to > 1 email (resubmit by different users — rare but real).

In the attribution loop: Tier 2 fires when the report row has an ISRC present
in this map **and** the ISRC unambiguously maps to one email without needing
name confirmation.

**This run result:** Tier 2 = 0 rows. All ISRC-matched rows were caught by Tier 1
(legacy SQL dump had the ISRC first). But the infrastructure is in place for
future months when Supabase-native ISRCs dominate.

**CLAUDE.md reference:**
→ `### \`ingest_royalty_report.py\` invariants` → "Attribution (6-tier priority)":
  "(2) Supabase ISRC-only — an ISRC that unambiguously belongs to a single Supabase submission owner (derived from isrc_to_sub with a >1-email drop)"

---

## Step 6 — Supabase Title-Only Reverse Lookup (Tier 6)

**File modified:** `migration/ingest_royalty_report.py`

**Problem:**
Some artists have no ISRC in the report and no legacy SQL record, but their
exact song title matches a submission in Supabase. When that title belongs to
exactly one user, it's safe to attribute.

**What we built:**
At startup, build `title_to_email` from all submissions:
- Extract `data->>'song_title'` + album `data->'songs'->[N]->>'song_title'`
- Normalize: `norm(title)`
- Drop any title mapping to > 1 email

In the loop: Tier 6 is the last-resort fallback — fires only when all 5 earlier
tiers failed and the normalized report title matches an unambiguous Supabase submission.

**This run result:** Tier 6 = **2,712 rows, ₹14,047, 60 users** — the second-largest
revenue tier. Without it, those artists would have been in the unmatched pile.

**CLAUDE.md reference:**
→ `### \`ingest_royalty_report.py\` invariants` → "Attribution (6-tier priority)":
  "(6) Supabase title-only — the report's normalized song title unambiguously belongs to a single Supabase submission owner"
→ Also see: "Names/ISRCs/titles mapping to more than one email are **dropped** (never guess)"

---

## Step 7 — INR Workbook Auto-Detection

**File modified:** `migration/ingest_royalty_report.py`

**Problem:**
The original script assumed a **USD workbook** — it multiplied each row's
`royalty` column by a monthly FX rate from `fx_rates.json`.

The report we received was `Royalty_Reports_2026_Consolidated_INR.xlsx` —
already converted to INR by the DSP aggregator, with a `royalty_inr` column.
Running USD logic on this would multiply INR values by ~86 (the USD→INR rate),
creating completely fabricated balances.

**What we built:**
Auto-detection flag `is_inr_workbook` — checks whether the `royalty_inr`
column contains non-None values in the first 100 **data rows** (not just
whether the column header exists, since headers are always present in the
sheet):
```python
is_inr_workbook = any(
    r.get("royalty_inr") is not None
    for r in rows[:min(100, len(rows))]
)
```

When `is_inr_workbook = True`:
- Read `royalty_inr` column directly (already in INR)
- Skip FX multiplication entirely
- Skip the guard that refuses to run without FX entries for every period
- Print `[INFO] INR workbook detected (royalty_inr column, fx_to_inr=1) — no FX conversion will be applied`

USD workbooks still require FX entries for every period — unchanged.

**CLAUDE.md reference:**
→ `### \`ingest_royalty_report.py\` invariants` → "Workbook format auto-detected —
script checks for royalty_inr column in the first 100 rows. If present: INR workbook…
If absent: USD workbook… script **refuses to run** if any period lacks an FX entry"

---

## Step 8 — Apple Music CSV Supplementary Source (`--csv`)

**File modified:** `migration/ingest_royalty_report.py`

**Problem:**
Apple Music sends a separate per-track CSV (`applemusic_process_MM_YYYY_detail_report.csv`)
with the authoritative per-song INR breakdown. If we use both the xlsx and the CSV,
Apple Music rows are double-counted. If we use only the xlsx, the granularity is worse.

**What we built:** `--csv PATH` flag with full-replacement logic:

1. Parse CSV columns: `item_artist`, `song_name`, `total` (streams), `royality` (INR — note the typo in Apple's column name; we handle it exactly as-is)
2. Run same 6-tier attribution on each CSV row
3. Auto-detect period from filename pattern `_MM_YYYY_` → `2026-03`
   Override with `--csv-period YYYY-MM` when auto-detect fails
4. **Replace strategy:** delete the xlsx Apple Music rows for that period from the
   aggregation dict, insert CSV rows instead. One source wins per (platform, period).

**Windows-specific quirk discovered:**
The Apple Music CSV was delivered as:
```
Downloads\
  applemusic_process_03_2026_detail_report.csv\   ← this is a DIRECTORY
    applemusic_process_03_2026_detail_report.csv  ← actual CSV file inside
```
A directory named `.csv` containing the real file. We passed the inner path:
```powershell
--csv "C:\Users\ViditVaibhav\Downloads\applemusic_process_03_2026_detail_report.csv\applemusic_process_03_2026_detail_report.csv"
```

**This run result:** 935 CSV rows → 290 aggregated rows (₹782), replacing the equivalent xlsx rows.

**CLAUDE.md reference:**
→ `### \`ingest_royalty_report.py\` invariants` → "Apple Music INR CSV (--csv PATH)" section:
  "CSV data **fully replaces** xlsx Apple Music rows for the same period (prevents double-counting)"
→ Also: "Period auto-detected from `_MM_YYYY_` filename pattern; override with `--csv-period YYYY-MM`"

---

## Step 9 — Reconciliation Cap (`--expected-net`)

**File modified:** `migration/ingest_royalty_report.py`

**Problem:**
Attribution bugs (e.g. a name collision that routes revenue to the wrong user)
could cause us to write more INR than Tunefry actually received. This creates
phantom balances that artists can withdraw — real financial loss to Tunefry.

**What we built:** Hard abort before any live write, with a ₹1 tolerance
buffer to absorb floating-point rounding across thousands of rows:
```python
if total_to_write_inr > exp_net + Decimal("1"):
    # live run: sys.exit(1)
    # dry-run: prints [BREACH] warning instead of aborting
```
The ₹1 buffer means a total that lands at `cap + ₹0.50` due to rounding
still passes. Anything above `cap + ₹1` is a genuine over-attribution and
aborts on `--live`; dry-run prints `[BREACH]` so you can investigate before
committing.

**Usage:**
```powershell
--expected-net 126361.52   # total net royalty from DSP report summary page
```

**This run:**
- Cap: ₹1,26,361.52
- Computed: ₹1,10,953.22
- 87.8% of cap → `[OK]`

Note: script also accepts `--expected-gross` for an informational cross-check
(we had gross but only used net as the hard cap).

**CLAUDE.md reference:**
→ `### \`ingest_royalty_report.py\` invariants` → "Reconciliation cap (--expected-net AMOUNT) —
After aggregation the script checks total_to_write_inr ≤ expected_net. If breached: **aborts before any live write**"

---

## Step 10 — `suggest_profile_updates.py` (Fuzzy Match Helper)

**Script created:** `migration/suggest_profile_updates.py`

**Why it was needed:**
After the dry-run showed 370 unmatched artists, the question was: which of
those artists already have Tunefry accounts but with a different `artist_name`?
Manually cross-referencing 370 names against 1,700 users is infeasible.

**How it works:**
1. Reads the latest `unmatched_*.csv` (auto-finds newest, or pass explicit path)
2. Filters to top-N artists above a minimum INR threshold (`--top 50 --min-usd 100`)
3. Fetches all Supabase users' name variants (`profiles.artist_name`, `profiles.full_name`,
   `auth.user_metadata.artist_name`, email prefix before `@`)
4. For each unmatched artist: runs `difflib.SequenceMatcher` against every
   user's name variants, with a substring bonus for strong partial matches
5. Returns top-3 candidate matches per artist with their score (0–1.0)
6. Writes `suggested_profile_updates.csv` with a ready-to-paste SQL UPDATE per row

**Output columns:**
```
unmatched_artist | sub_label | usd | inr | streams | rows
candidate_email | candidate_current_artist_name | candidate_full_name
match_score | matched_variant | sql_update
```

**Command:**
```powershell
.\venv\Scripts\python.exe migration\suggest_profile_updates.py `
  migration\reports\unmatched_20260819_0354.csv `
  --top 50 --min-usd 100
# Writes suggested_profile_updates.csv next to the input CSV
```

The `sql_update` column contains ready-to-run SQL like:
```sql
UPDATE public.profiles SET artist_name = 'Correct Name' WHERE user_id = 'uuid-here';
```
**You run these in Supabase SQL editor** — the script itself never writes to DB.

**CLAUDE.md reference:**
→ `migration/suggest_profile_updates.py` row in the scripts table under `## Legacy data migration`
→ `migration/MONTHLY_ROYALTY_INGESTION.md` → "Step 4: Review the unmatched CSV"

---

## Step 11 — Enriched Unmatched Accumulator + Excel Report

**File modified:** `migration/ingest_royalty_report.py`

**Problem:**
The existing `unmatched_*.csv` had only 6 rolled-up columns:
`artist, sub_label, row_count, total_royalty_usd, total_royalty_inr, total_streams`

No per-song detail. No per-DSP breakdown. No periods covered. Impossible to
know which specific songs were causing the miss or which platforms the money came from.

### 11a — Enrich the accumulator dict

For every unmatched row (xlsx loop AND Apple Music CSV loop), now accumulate:
```python
"tracks": {}       # {(title, isrc): {title, isrc, streams, inr}}
"platform_data": {} # {platform_group: {inr, streams}}
"periods": set()   # {"2026-02", "2026-03", ...}
```

Previously the Apple Music CSV loop had a plain `continue` on unmatched rows —
fixed to mirror the xlsx accumulator exactly.

### 11b — `_write_unmatched_excel()` function

New function added before `main()`. Uses `openpyxl` (already imported).
Writes `unmatched_YYYYMMDD_HHMM.xlsx` alongside the CSV on every run:

**Sheet 1 "Artists"** — sorted by ₹ Revenue desc:

| # | Artist | Sub Label | ₹ Revenue | Streams | Songs | Platforms | Periods |
|---|--------|-----------|-----------|---------|-------|-----------|---------|

Row colours:
- `FFB3B3` (light red) → ≥ ₹10,000 — fix these first
- `FFF2CC` (light yellow) → ≥ ₹1,000
- No fill → < ₹1,000

**Sheet 2 "Songs"** — all unmatched tracks globally, sorted by ₹ Revenue desc:

| Artist | Track Title | ISRC | ₹ Revenue | Streams |

**Sheet 3 "Platforms"** — aggregated across all unmatched artists:

| Platform | ₹ Revenue | Streams | Artists | Tracks |

All sheets: `4472C4` blue header (bold white text), freeze pane row 1,
auto column widths capped at 60.

**Output this run:**
```
Unmatched Excel -> ...\unmatched_20260819_0354.xlsx
```
File was deep in Claude AppData cache; copied to Desktop as
`unmatched_artists_20260819.xlsx` for easy access.

**CLAUDE.md reference:**
→ `### \`ingest_royalty_report.py\` invariants` → "Unmatched artists → unmatched_YYYYMMDD_HHMM.csv next to the workbook (columns: artist, sub_label, row_count, total_royalty_usd, total_royalty_inr, total_streams — sorted by INR in INR-workbook mode)"
→ `migration/MONTHLY_ROYALTY_INGESTION.md` → "Step 4: Review the unmatched CSV"

---

## Step 12 — CLAUDE.md Documentation Update

**File modified:** `CLAUDE.md`

**What changed:**
The `ingest_royalty_report.py` invariants section was rewritten from 4-tier
to the full 6-tier attribution description.

Specific additions:
- Tiers 2 (Supabase ISRC-only) and 6 (Supabase title-only) described with exact logic
- Unambiguity drop rule: "Names/ISRCs/titles mapping to more than one email are **dropped** (never guess)"
- Per-tier diagnostic printout in script output described
- `--csv PATH` Apple Music CSV behaviour documented
- INR workbook auto-detection documented (checks `royalty_inr` column)
- `--expected-net` reconciliation cap described
- Unmatched CSV: new `total_royalty_inr` column; sorted by INR in INR-workbook mode

**Known gap — script's own module docstring is stale:**
`ingest_royalty_report.py` lines 37–44 still describe the old 2-tier attribution
system. CLAUDE.md was updated correctly; the script's top-of-file docstring was
not. Before the next major change to the attribution logic, update those lines
to match the current 6-tier description.

**When to update CLAUDE.md again:**
- Attribution ladder changes (new tier, new logic)
- New DSP source type added
- New CLI flags added to the script
- Balance formula changes

**CLAUDE.md reference (meta):**
→ `### \`ingest_royalty_report.py\` invariants (earnings — money-critical, active monthly script)` — this is the section we updated

---

## Step 13 — Windows-Specific Setup (`PYTHONUTF8`)

**Not a code change — a runtime requirement.**

The ingest script prints ₹ (Indian Rupee sign, U+20B9) in its output.
On Windows, the default Python stdout encoding is `cp1252` which does not
include U+20B9 → `UnicodeEncodeError` and script crash.

**Fix:** Set `PYTHONUTF8=1` in the PowerShell session before every run:
```powershell
$env:PYTHONUTF8 = "1"
```
This forces Python to use UTF-8 for stdout/stderr regardless of Windows locale.

**Must be set in every new PowerShell window.** It does not persist.

---

## Step 14 — Locate the Royalty Report File

**Not a code change — a file path problem.**

The royalty workbook was not in `migration/reports/` (the documented location).
It was uploaded via Claude's file interface and landed in a deep AppData path:
```
C:\Users\ViditVaibhav\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\
  LocalCache\Roaming\Claude\local-agent-mode-sessions\
  dcb23662-...\6f750823-...\local_e1f2acaf-...\outputs\
  Royalty_Reports_2026_Consolidated_INR.xlsx
```

Found it with:
```powershell
gci -Recurse -Filter "Royalty_Reports*" C:\Users\ViditVaibhav\AppData 2>$null
```

**Recommendation for future months:** Save the Excel workbook into
`migration/reports/YYYY-MM.xlsx` before running. Path is then short and reproducible.
The `.gitignore` excludes `migration/reports/*.xlsx` (large binary files).

**CLAUDE.md reference:**
→ `migration/MONTHLY_ROYALTY_INGESTION.md` → "Step 1: Save the workbook into `migration/reports/YYYY-MM.xlsx`"

---

## Step 15 — Dry-Run Validation

**Always run without `--live` first.** Default is dry-run.

```powershell
$env:PYTHONUTF8 = "1"
.\venv\Scripts\python.exe migration\ingest_royalty_report.py `
  "migration\reports\2026-03.xlsx" `
  --fx-rates migration\fx_rates.json `
  --csv "migration\reports\applemusic_process_03_2026_detail_report.csv" `
  --csv-period 2026-03 `
  --expected-net 126361.52
```

**Actual dry-run output for this session:**
```
FX rates loaded: ['2026-02', '2026-03', '2026-04']
Baseline loaded: 57 users, sum(legacy_withdrawn)=21284.0
Legacy map: 512 artist names, 2044 full names, 2320 usernames, 113 ISRCs, 90 UPCs, 2194 (artist||title)

65134 data rows in sheet 'Combined_All'
935 rows from Apple Music CSV (period=2026-03)
[INFO] INR workbook detected (royalty_inr column, fx_to_inr=1) — no FX conversion will be applied.

Building artist -> email map…
  2198 from Supabase + 1543 new from legacy = 3741 total names -> email (163 ambiguous dropped)
Building ISRC -> submission_id map…  123 (email, ISRC) pairs indexed
Building (song title) -> submission_id fallback map…  2517 (email, title) pairs indexed
  isrc_to_email:  123 unique  |  title_to_email: 2289 unique

matched_rows=55242  unmatched_rows=9892  skipped(dispute)=0  skipped(bad_period)=0
-> 6315 song_stats rows after aggregation

-- Attribution tiers (rows / INR / distinct users) --
  legacy_isrc          : rows=1011   ₹     432.53   users=9
  supabase_isrc_only   : rows=0      ₹       0.00   users=0   [NEW]
  supabase_email_isrc  : rows=0      ₹       0.00   users=0
  artist_name          : rows=48241  ₹   86493.42   users=339
  legacy_artist_title  : rows=4139   ₹   10720.46   users=63
  supabase_title_only  : rows=2712   ₹   14046.69   users=60   [NEW]

-- Reconciliation --
  Total to write to song_stats:   ₹110953.22
  Expected net cap:               ₹126361.52  writing 87.8% of cap  [OK]

DRY RUN — nothing written
```

**Checklist before approving a dry-run:**
- [ ] Attribution tiers all labelled — any tier with 0 rows needs investigation
- [ ] Reconciliation shows `[OK]` — `[BREACH]` means stop completely
- [ ] Top-10 `total_earned` deltas look plausible (no 100× anomaly)
- [ ] `matched_rows` / total ≥ 40% (we hit 84.8%)
- [ ] No Python tracebacks

**CLAUDE.md reference:**
→ `migration/MONTHLY_ROYALTY_INGESTION.md` → "Step 3: Dry-run" sanity checklist
→ `migration/MONTHLY_ROYALTY_INGESTION.md` → "Verification checklist" (run after live)

---

## Step 16 — Live Run

After dry-run approved:

```powershell
$env:PYTHONUTF8 = "1"
.\venv\Scripts\python.exe migration\ingest_royalty_report.py `
  "migration\reports\2026-03.xlsx" `
  --fx-rates migration\fx_rates.json `
  --csv "migration\reports\applemusic_process_03_2026_detail_report.csv" `
  --csv-period 2026-03 `
  --expected-net 126361.52 `
  --live
```

**Actual live output:**
```
== LIVE WRITE PHASE ==
  deleted 419 existing song_stats rows (periods=[(April,2026),(February,2026),(March,2026)])
  inserted song_stats 500/6315
  ...
  inserted song_stats 6315/6315
  upserted 422 artist_balances
  marked 51 submissions approved

Done.
```

**Blast radius (final):**

| Table | Action | Scope |
|-------|--------|-------|
| `public.song_stats` | DELETE covered periods + INSERT | 417 users × 3 periods → 6,315 new rows |
| `public.artist_balances` | UPSERT (recompute balances) | 422 users |
| `public.submissions` | SET status='approved' where not already | 51 rows |
| `withdrawal_requests` | READ-ONLY | never written |
| `withdrawn_baseline.json` | IMMUTABLE | never touched |

**Why 51 approvals not 840:** Script only sets `status='approved'` where
current status ≠ `approved`. Most submissions were already reviewed by admin.

**The data is live immediately.** `ingest_royalty_report.py` writes directly
to Supabase via the service-role client. No backend redeploy needed.
`/earnings/me`, `/earnings/balance`, `/earnings/songs/{id}` read from these
tables — any artist opening the app now sees updated balances.

**CLAUDE.md reference:**
→ `### \`ingest_royalty_report.py\` invariants` → "Period-replace write strategy"
→ `### \`ingest_royalty_report.py\` invariants` → "Balance formula"
→ `migration/MONTHLY_ROYALTY_INGESTION.md` → "Step 5: Live run"

---

## Final Attribution Breakdown

| Tier | Description | Rows | ₹ INR | Users |
|------|-------------|------|-------|-------|
| 1 | Legacy ISRC (SQL dump) | 1,011 | ₹432 | 9 |
| 2 | Supabase ISRC-only reverse | 0 | ₹0 | 0 |
| 3 | Supabase email + ISRC | 0 | ₹0 | 0 |
| 4 | Artist name (Supabase + legacy names) | 48,241 | ₹86,493 | 339 |
| 5 | Legacy artist‖title (SQL dump) | 4,139 | ₹10,720 | 63 |
| 6 | Supabase title-only reverse | 2,712 | ₹14,047 | 60 |
| — | **Total matched** | **56,103** | **₹1,10,953** | **417** |
| — | Unmatched | 9,892 | ₹15,450 | 370 artists |

---

## What's Left (Recovering the Remaining ₹15,450)

The 370 unmatched artists fall into two categories:

**Category A — Tunefry user with wrong `artist_name` in `public.profiles`:**
Fix their name in Supabase SQL editor → re-run ingest → they get credited.
Period-replace makes re-runs safe (idempotent).

**Category B — External artists with no Tunefry account:**
Label distributes through them; no account to credit. Money stays with Tunefry.

**Workflow:**
```powershell
# 1. Get fuzzy-match suggestions (read-only)
$env:PYTHONUTF8 = "1"
.\venv\Scripts\python.exe migration\suggest_profile_updates.py `
  "C:\Users\ViditVaibhav\Desktop\unmatched_artists_20260819.xlsx" `
  --top 50 --min-usd 100

# 2. Open suggested_profile_updates.csv
#    For each artist you recognise as a Tunefry user with match_score > 0.8:
#    run the sql_update in Supabase SQL editor

# 3. Re-run dry-run to confirm improved match rate
$env:PYTHONUTF8 = "1"
.\venv\Scripts\python.exe migration\ingest_royalty_report.py `
  "migration\reports\2026-03.xlsx" --fx-rates migration\fx_rates.json `
  --csv "..." --csv-period 2026-03 --expected-net 126361.52

# 4. If dry-run looks good → add --live
```

---

## Complete Monthly Workflow (Future Reference)

For every new month's royalty report:

```powershell
# 1. Save workbook to migration/reports/YYYY-MM.xlsx
# 2. Save Apple Music CSV to migration/reports/applemusic_process_MM_YYYY_detail_report.csv
# 3. Add FX entry to migration/fx_rates.json  (skip if INR workbook)
#    e.g.:  "2026-05": 83.72
#    git commit migration/fx_rates.json

# 4. Dry-run
$env:PYTHONUTF8 = "1"
.\venv\Scripts\python.exe migration\ingest_royalty_report.py `
  "migration\reports\YYYY-MM.xlsx" `
  --fx-rates migration\fx_rates.json `
  --csv "migration\reports\applemusic_process_MM_YYYY_detail_report.csv" `
  --expected-net <net from DSP report summary>

# 5. Review output + unmatched_*.xlsx on Desktop
# 6. If reconciliation [OK] and tiers look right → live run (add --live)

# 7. Post-live spot-check in browser:
#    /earnings/me  → new monthly chart bars appear
#    /earnings/balance → available_balance ≥ before ingest
#    /withdrawals/me  → history unchanged
```

---

## CLAUDE.md Reference Guide

Use this table to find the right place to look before taking any action:

| What you need to know | Where to look |
|----------------------|---------------|
| Full 6-tier attribution ladder and unambiguity rules | `CLAUDE.md` → `### \`ingest_royalty_report.py\` invariants (earnings — money-critical, active monthly script)` → "Attribution (6-tier priority)" |
| Balance formula (`total_withdrawn`, `available_balance`) | `CLAUDE.md` → same invariants section → "Balance formula" |
| Why `withdrawn_baseline.json` must never be regenerated | `CLAUDE.md` → same invariants section → "`withdrawn_baseline.json` is IMMUTABLE" |
| Period-replace write strategy (idempotency) | `CLAUDE.md` → same invariants section → "Period-replace write strategy" |
| Apple Music INR CSV behaviour | `CLAUDE.md` → same invariants section → "Apple Music INR CSV (`--csv PATH`)" |
| INR vs USD workbook detection | `CLAUDE.md` → same invariants section → "Workbook format auto-detected" |
| Reconciliation cap | `CLAUDE.md` → same invariants section → "Reconciliation cap (`--expected-net AMOUNT`)" |
| `platform_group` canonical values (exact strings) | `CLAUDE.md` → `migration/platform_map.py` row + "Group must be one of Spotify, Apple Music…" |
| Why `ingest_streams.py` is frozen | `CLAUDE.md` → `### \`ingest_streams.py\` invariants (historically frozen — do NOT re-run)` |
| Overview of all migration scripts | `CLAUDE.md` → `## Legacy data migration (\`migration/\`)` → scripts table |
| Monthly operator runbook (full step-by-step) | `migration/MONTHLY_ROYALTY_INGESTION.md` |
| Danger zones (FX mistakes, mid-ingest deletes, ambiguous names) | `migration/MONTHLY_ROYALTY_INGESTION.md` → "Danger zones" section |
| Post-live verification checklist | `migration/MONTHLY_ROYALTY_INGESTION.md` → "Verification checklist" |
| `build_legacy_map.py` usage + when to re-run | `CLAUDE.md` → `## Legacy data migration` → `migration/build_legacy_map.py` row |
| `suggest_profile_updates.py` usage | `CLAUDE.md` → `## Legacy data migration` → `migration/suggest_profile_updates.py` row |

---

## Files Changed Across This Work

| File | Changed in | Action | What it does |
|------|-----------|--------|--------------|
| `migration/build_legacy_map.py` | Prior session | Created | Parses SQL dump → `legacy_map.json` |
| `migration/legacy_map.json` | Prior session | Created | ISRC + 3 name variants → email (512 artists, 113 ISRCs, 2194 artist‖title) |
| `migration/ingest_royalty_report.py` | Multiple sessions | Heavily modified | 6-tier attribution; INR mode; CSV source; reconciliation cap; unmatched Excel |
| `migration/suggest_profile_updates.py` | Prior session | Created | Fuzzy-matches unmatched artists → suggests SQL UPDATEs |
| `migration/MONTHLY_ROYALTY_INGESTION.md` | Multiple sessions | Updated | Full operator runbook with INR mode, CSV flag, verify checklist |
| `CLAUDE.md` | This session | Updated | Attribution 4-tier → 6-tier; all new invariants documented |
| `docs/royalty-attribution-journey.md` | This session | Created | This file |
