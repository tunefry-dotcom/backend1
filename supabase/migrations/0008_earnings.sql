-- Artist earnings, per-song stream analytics, and withdrawal requests.
-- Run once in the Supabase SQL editor after deploying the backend.
--
-- Additive only: creates three new tables. Nothing existing is altered.
-- All writes happen via the service-role client (RLS denies anon/auth writes).

-- ---------------------------------------------------------------------------
-- song_stats: one row per (song x platform x month) for a user.
-- Populated by migration/ingest_streams.py from legacy MusicStreams; the
-- UNIQUE key makes the monthly re-ingest an idempotent upsert.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.song_stats (
  id             UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  user_email     TEXT          NOT NULL,          -- lowercased; join key (matches submissions)
  submission_id  UUID          NULL,              -- link to public.submissions when matched
  artist_name    TEXT          NOT NULL,
  song_title     TEXT          NOT NULL,
  platform       TEXT          NOT NULL,          -- normalized canonical platform name
  platform_group TEXT          NOT NULL,          -- major platform name OR 'Other'
  period_month   TEXT          NOT NULL,          -- e.g. 'October'
  period_year    INT           NOT NULL,
  streams        INT           NOT NULL DEFAULT 0,
  revenue        NUMERIC(20, 10) NOT NULL DEFAULT 0,  -- high precision (legacy Decimal(18,10))
  created_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  CONSTRAINT song_stats_unique_key
    UNIQUE (user_email, song_title, platform, period_month, period_year)
);

CREATE INDEX IF NOT EXISTS song_stats_user_email_idx  ON public.song_stats (user_email);
CREATE INDEX IF NOT EXISTS song_stats_submission_idx  ON public.song_stats (submission_id);

-- ---------------------------------------------------------------------------
-- artist_balances: derived per-user rollup so reads never run a live SUM.
-- available_balance = total_earned - total_withdrawn - pending withdrawals.
-- Recomputed by the ingest script; zeroed when a user requests a withdrawal.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.artist_balances (
  user_email        TEXT          PRIMARY KEY,
  total_earned      NUMERIC(20, 10) NOT NULL DEFAULT 0,
  total_withdrawn   NUMERIC(20, 10) NOT NULL DEFAULT 0,
  available_balance NUMERIC(20, 10) NOT NULL DEFAULT 0,
  last_updated      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- withdrawal_requests: artist payout requests + admin review queue.
-- amount is server-derived (= available_balance at request time); the client
-- never sets it. A pending/approved/paid request is deducted by the ingest
-- script so a re-run never resurrects a balance that was already requested.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.withdrawal_requests (
  id             UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        UUID          NULL,
  user_email     TEXT          NOT NULL,
  amount         NUMERIC(20, 10) NOT NULL,
  status         TEXT          NOT NULL DEFAULT 'pending',  -- pending | paid (admin: mark Paid or Delete)
  method         TEXT          NOT NULL,                    -- 'upi' | 'bank'
  payout_details JSONB         NOT NULL DEFAULT '{}'::jsonb, -- {upi_id} OR {account_holder,bank_name,account_number,ifsc}
  snapshot       JSONB         NOT NULL DEFAULT '{}'::jsonb, -- {plan, full_name, artist_name, phone, city, state, age}
  admin_note     TEXT          NULL,
  requested_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  processed_at   TIMESTAMPTZ   NULL
);

CREATE INDEX IF NOT EXISTS withdrawal_requests_status_idx
  ON public.withdrawal_requests (status, requested_at DESC);
CREATE INDEX IF NOT EXISTS withdrawal_requests_user_idx
  ON public.withdrawal_requests (user_email);

-- ---------------------------------------------------------------------------
-- RLS: enable on all three, allow each authenticated user to READ only their
-- own rows (matched on the JWT email / uid). All writes are service-role only,
-- which bypasses RLS entirely.
-- ---------------------------------------------------------------------------
ALTER TABLE public.song_stats          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.artist_balances     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.withdrawal_requests ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS song_stats_read_own ON public.song_stats;
CREATE POLICY song_stats_read_own ON public.song_stats
  FOR SELECT TO authenticated
  USING (lower(user_email) = lower(auth.jwt() ->> 'email'));

DROP POLICY IF EXISTS artist_balances_read_own ON public.artist_balances;
CREATE POLICY artist_balances_read_own ON public.artist_balances
  FOR SELECT TO authenticated
  USING (lower(user_email) = lower(auth.jwt() ->> 'email'));

DROP POLICY IF EXISTS withdrawal_requests_read_own ON public.withdrawal_requests;
CREATE POLICY withdrawal_requests_read_own ON public.withdrawal_requests
  FOR SELECT TO authenticated
  USING (user_id = auth.uid()
         OR lower(user_email) = lower(auth.jwt() ->> 'email'));
