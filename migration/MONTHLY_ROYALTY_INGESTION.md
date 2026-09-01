# Monthly Royalty Report Ingestion — Operator Guide

This is the runbook for updating every artist's earnings, balance, and stats
in Tunefry each month using the DSP-consolidated Excel royalty report.

**READ THIS FIRST — this script moves real money.** A wrong FX rate, a
wrong file, or a mid-ingest paid-request delete can cause an artist to be
under-paid or over-paid. Everything here is written to make the correct
thing the easy thing.

---

## What this does (data-flow map)

```
Excel workbook (Combined_All sheet, USD)
       │
       │  ingest_royalty_report.py
       │  · apply monthly FX rate (fx_rates.json)
       │  · attribute artist → user_email (name match, ambiguity dropped)
       │  · aggregate to (email × song × platform × month)
       ▼
public.song_stats     ─────►  /earnings/me   → Overview + Stats charts
                              /earnings/songs/{sub_id} → per-song modal

public.artist_balances ────►  /earnings/balance → Withdraw Earnings hero card
                              (available_balance = total_earned − total_withdrawn − pending,
                               where total_withdrawn = withdrawn_baseline.json[email] + Σ paid requests)

public.submissions.status  ─►  admin panel + user submission list ("Approved" label)

withdrawal_requests   READ-ONLY. Ingest never writes this table.
```

---

## Monthly workflow

Every month, once the new DSP report is delivered:

1. **Save the workbook** into `migration/reports/YYYY-MM.xlsx` (create the
   folder if missing). Do NOT commit large Excel files — the `.gitignore`
   should exclude `migration/reports/*.xlsx`.

2. **Update `migration/fx_rates.json`** — add one entry per new
   `source_period` present in the file (`"YYYY-MM": <rate>`). Use the
   RBI monthly reference rate or an equivalent auditable source. **Commit
   this change before running the script** — the file is the audit trail
   for what rate was applied.

3. **Dry-run** (default — no writes):
   ```powershell
   .\venv\Scripts\python.exe migration\ingest_royalty_report.py `
     "migration\reports\YYYY-MM.xlsx" `
     --fx-rates migration\fx_rates.json `
     --expected-net 126361.52
   ```
   Pass `--expected-net` (net royalty payable from the report summary) so
   the reconciliation check can flag any over-attribution before writes.
   Read the whole output. Sanity checks:
   - `FX rates loaded` includes every period in the file
   - `matched_rows` / `unmatched_rows` ratio is reasonable (≥ 40 % is
     typical; the rest are artists without Tunefry accounts)
   - Reconciliation block shows `[OK]` and `Total to write ≤ expected net`
   - Top-10 `total_earned` deltas look plausible — flag any user whose
     delta looks like an order-of-magnitude error

3b. **(If Apple Music INR CSV is provided)** — pass it with `--csv`:
   ```powershell
   .\venv\Scripts\python.exe migration\ingest_royalty_report.py `
     "migration\reports\YYYY-MM.xlsx" `
     --fx-rates migration\fx_rates.json `
     --csv "migration\reports\applemusic_process_MM_YYYY_detail_report.csv" `
     --expected-net 126361.52
   ```
   The CSV's `royality` column is already in INR — no FX conversion is
   applied. The CSV data **fully replaces** the xlsx Apple Music rows for
   the same period to prevent double-counting. Period is auto-detected from
   the filename (`_MM_YYYY_` pattern); override with `--csv-period YYYY-MM`
   if needed.

4. **Review the unmatched CSV** (`unmatched_YYYYMMDD_HHMM.csv` next to the
   workbook). For each high-value unmatched artist:
   - If they have a Tunefry account with a mistyped/mis-cased artist name,
     update `public.profiles.artist_name` in Supabase and re-run the
     dry-run — they should now match.
   - If they don't have an account (label distributes through someone
     else), leave unmatched. Money remains with Tunefry.

5. **Live run** — only after the dry-run is clean and reviewed:
   ```powershell
   .\venv\Scripts\python.exe migration\ingest_royalty_report.py `
     "migration\reports\YYYY-MM.xlsx" `
     --fx-rates migration\fx_rates.json `
     --csv "migration\reports\applemusic_process_MM_YYYY_detail_report.csv" `
     --expected-net 126361.52 `
     --live
   ```

6. **Spot-check the UI** — log in as a test artist and confirm:
   - Overview → Total Streams / Estimated Revenue reflects new streams
   - Stats → Monthly chart has the new months; Platform chart includes
     Facebook / Snap if applicable
   - Withdraw Earnings → available_balance is not lower than before ingest
     for a user who did nothing (only new positive earnings + prior state)

---

## Balance formula

For each user (all users in `artist_balances`, not just users in this file):

```
total_earned      = Σ song_stats.revenue for user_email
paid_now          = Σ withdrawal_requests.amount where status='paid' AND user_email=X
pending_now       = Σ withdrawal_requests.amount where status='pending' AND user_email=X
legacy_withdrawn  = withdrawn_baseline.json[email]      (default 0 for new users)
total_withdrawn   = legacy_withdrawn + paid_now
available_balance = max(0, total_earned − total_withdrawn − pending_now)
```

Worked example (`trilochansingh480@icloud.com`, made-up numbers):

| Quantity | Value | Source |
|---|---:|---|
| total_earned (before this ingest) | ₹7,204.81 | pre-existing song_stats |
| new revenue (this ingest, Feb–Apr) | +₹558,012.31 | Excel × FX |
| **total_earned (after)** | **₹565,217.12** | |
| legacy_withdrawn | ₹5,000.00 | `withdrawn_baseline.json` |
| paid_now | ₹0.00 | no paid Supabase requests |
| pending_now | ₹0.00 | no pending requests |
| **total_withdrawn** | **₹5,000.00** | |
| **available_balance** | **₹560,217.12** | |

If the user then requests a withdrawal (say ₹560,217.12), the app zeroes
`available_balance` immediately. Next ingest:
- `paid_now` still 0 (admin hasn't marked paid yet), so ingest recomputes
  `pending_now = 560,217.12` → `available_balance = 5000 (baseline+earnings) − 5000 − 560217 = -560212 → clamped to 0`. ✓
- Once admin marks paid: `paid_now = 560,217.12` → `total_withdrawn = 565,217.12`, `available_balance = 0`. ✓

---

## Idempotency & re-runs

**Safe to re-run the same file with the same FX rates.** The script uses a
period-replace strategy:

1. For each user with rows in the file: `DELETE FROM song_stats WHERE
   user_email=X AND (period_month, period_year) IN <covered periods>`.
2. Insert the freshly computed rows.

The second run produces byte-identical DB state.

**Re-run with corrected FX rates:** the same period-replace flow cleanly
overwrites; any previous mistake is corrected.

**Re-run against a wider file** (e.g., a report now covering Feb–May
instead of just Feb–Apr): Feb–Apr rows get overwritten identically, May
rows get inserted. Older months (Jan and before) are never touched.

---

## Danger zones (READ BEFORE RUNNING LIVE)

### FX rate mistakes
Wrong rate → all revenue for that month is wrong for every artist. Fix by
correcting `fx_rates.json` and re-running (period-replace handles the
overwrite). Because `fx_rates.json` is committed, you have an audit trail
of exactly what rate produced what balance.

### `withdrawn_baseline.json` is immutable
This file is the ONLY record of legacy `tunefry` adjustments +
`WithdrawalHistory` payouts. If it's edited by hand or lost, every
downstream balance will drift by the difference. If it's ever lost,
restore from git — do NOT regenerate with
`compute_withdrawn_baseline.py`. Regenerating uses the current
`artist_balances.total_withdrawn`, which by then reflects *this* script's
writes (including current paid requests) — the regenerated baseline will
double-count paid amounts on the next ingest.

**Correct recovery procedure if lost:** restore
`migration/withdrawn_baseline.json` from git history
(`git show HEAD:migration/withdrawn_baseline.json > migration/withdrawn_baseline.json`).

### Do NOT re-run `ingest_streams.py`
`migration/ingest_streams.py` is the legacy SQL-dump ingest — it writes
`total_withdrawn` from its own model of the world (`tunefry` adjustments,
`WithdrawalHistory`, `withdrawal_requests`). Running it AFTER this script
overwrites `total_withdrawn` and corrupts the model. Treat
`ingest_streams.py` as historically frozen.

### Ambiguous artist names
Names that map to more than one user are always dropped from ingestion
(better than misattributing money). Fix by giving each user a unique
`artist_name` in `public.profiles` (Supabase SQL editor is fine — this is
an admin-metadata edit, not a schema change). Then re-run the dry-run.

### Never delete `withdrawal_requests` mid-ingest
Deleting a `paid` request between ingest runs would silently un-pay the
user: their `paid_now` drops, `available_balance` goes up, and they can
withdraw again. If a paid request is genuinely bogus, refund it via a
NEW ledger row (record in a comment/admin-note somewhere) — do not delete.

### Unmatched artists = trapped money
Every unmatched row in the CSV is revenue Tunefry received but hasn't
attributed to an artist. Prioritize fixing high-value unmatched rows
before running live — an artist who signs up next month will not
automatically pick up their historical streams unless we re-ingest with
their artist_name fixed in profiles.

---

## Verification checklist

Run through this after every live ingest:

- [ ] Dry-run summary matches the live-run summary (no unexpected changes
      between the two)
- [ ] Sum of `song_stats.revenue` after ingest = sum of `royalty × FX`
      from Excel + sum of `song_stats.revenue` NOT in covered periods
      before ingest. Off by more than ₹1 → stop, investigate.
- [ ] Pick 3 users manually and verify their `available_balance` matches
      the formula above (one Free-plan, one paid, one with a pending
      withdrawal request).
- [ ] `/earnings/me` for a test artist shows the new monthly + platform
      splits in the UI.
- [ ] `/withdrawals/me` history for the same user is unchanged (script
      never writes `withdrawal_requests`).
- [ ] Idempotency: re-run the script (dry-run) — the output should
      report a zero delta (nothing new to write). If it reports changes,
      investigate what floated.

---

## Files at a glance

| File | Purpose | Immutable? |
|---|---|---|
| `migration/ingest_royalty_report.py` | The main script | Evolves with reqs |
| `migration/compute_withdrawn_baseline.py` | One-time baseline generator | Rarely rerun |
| `migration/withdrawn_baseline.json` | Legacy withdrawn per user | **Yes — never edit** |
| `migration/withdrawn_baseline.audit.json` | Sidecar for the above | Audit only |
| `migration/fx_rates.json` | Monthly USD→INR rates | Append-only |
| `migration/platform_map.py` | Shared platform normalization | Extend as new DSPs appear |
| `migration/reports/YYYY-MM.xlsx` | Monthly Excel report | Kept for audit; not in git |
| `unmatched_YYYYMMDD_HHMM.csv` | Per-run unmatched-artist report | Kept for review |
