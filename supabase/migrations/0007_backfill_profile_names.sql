-- Migration: 0007_backfill_profile_names.sql
--
-- One-time backfill: copies full_name / artist_name / phone from
-- auth.users.raw_user_meta_data into public.profiles for users whose
-- profiles row has a NULL or empty-string value for these columns.
--
-- Safety guarantees:
--   • NEVER overwrites a non-empty existing profiles value (COALESCE logic).
--   • Only touches the 3 name columns + updated_at — no other data is modified.
--   • Idempotent: re-running after a successful backfill updates 0 rows.
--   • Blast radius: only public.profiles rows where at least one name column
--     is blank AND the corresponding user_metadata value is non-empty.
--
-- HOW TO RUN:
--   Step 1 — run the SELECT below first (dry-run preview).
--             Inspect the result set to confirm it looks correct.
--   Step 2 — run the UPDATE below to apply the backfill.
--   Step 3 — re-run the SELECT to confirm 0 rows remain (idempotency check).
--
-- ============================================================
-- STEP 1: DRY-RUN PREVIEW — shows what the UPDATE would change
-- ============================================================

SELECT
  p.user_id,
  u.email,
  p.full_name                          AS current_full_name,
  u.raw_user_meta_data->>'full_name'   AS meta_full_name,
  p.artist_name                        AS current_artist_name,
  u.raw_user_meta_data->>'artist_name' AS meta_artist_name,
  p.phone                              AS current_phone,
  u.raw_user_meta_data->>'phone'       AS meta_phone
FROM public.profiles p
JOIN auth.users u ON u.id = p.user_id
WHERE
     ((p.full_name    IS NULL OR trim(p.full_name)    = '') AND nullif(trim(u.raw_user_meta_data->>'full_name'),    '') IS NOT NULL)
  OR ((p.artist_name  IS NULL OR trim(p.artist_name)  = '') AND nullif(trim(u.raw_user_meta_data->>'artist_name'), '') IS NOT NULL)
  OR ((p.phone        IS NULL OR trim(p.phone)        = '') AND nullif(trim(u.raw_user_meta_data->>'phone'),       '') IS NOT NULL)
ORDER BY u.email;


-- ============================================================
-- STEP 2: BACKFILL — run only after reviewing STEP 1 output
-- ============================================================

UPDATE public.profiles p
SET
  full_name   = COALESCE(NULLIF(trim(p.full_name),   ''), nullif(trim(u.raw_user_meta_data->>'full_name'),   '')),
  artist_name = COALESCE(NULLIF(trim(p.artist_name), ''), nullif(trim(u.raw_user_meta_data->>'artist_name'), '')),
  phone       = COALESCE(NULLIF(trim(p.phone),       ''), nullif(trim(u.raw_user_meta_data->>'phone'),       '')),
  updated_at  = now()
FROM auth.users u
WHERE u.id = p.user_id
  AND (
       ((p.full_name    IS NULL OR trim(p.full_name)    = '') AND nullif(trim(u.raw_user_meta_data->>'full_name'),    '') IS NOT NULL)
    OR ((p.artist_name  IS NULL OR trim(p.artist_name)  = '') AND nullif(trim(u.raw_user_meta_data->>'artist_name'), '') IS NOT NULL)
    OR ((p.phone        IS NULL OR trim(p.phone)        = '') AND nullif(trim(u.raw_user_meta_data->>'phone'),       '') IS NOT NULL)
  );


-- ============================================================
-- STEP 3: IDEMPOTENCY CHECK — re-run STEP 1; should return 0 rows
-- ============================================================
