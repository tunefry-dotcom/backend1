-- Refer & Earn: per-user referral codes, referral relationships, and a
-- referral-commission audit ledger.
--
-- Additive only: adds one column + two new tables. Nothing existing is altered.
-- All writes happen via the service-role client (RLS denies anon/auth writes).
--
-- referral_earnings does NOT hold the live wallet balance — it is an
-- immutable audit trail. The live balance is folded into the existing
-- public.artist_balances.available_balance by earnings.service.recompute_balance().

ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS referral_code TEXT UNIQUE;

-- ---------------------------------------------------------------------------
-- referrals: one row per successful signup-with-code. UNIQUE on
-- referred_user_id so a user can only ever be attributed to one referrer.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.referrals (
  id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  referrer_user_id   UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  referred_user_id   UUID        NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
  referral_code_used TEXT        NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS referrals_referrer_idx ON public.referrals (referrer_user_id);

-- ---------------------------------------------------------------------------
-- referral_earnings: one immutable row per commission event (10% of a
-- referred user's plan price, credited whenever their plan activates —
-- via a verified Razorpay payment or an admin-granted plan; repeats on
-- every future purchase, not just the first).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.referral_earnings (
  id               UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  referrer_user_id UUID          NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  referrer_email   TEXT          NOT NULL,  -- denormalized: artist_balances is keyed by email
  referred_user_id UUID          NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  plan             TEXT          NOT NULL,
  amount           NUMERIC(20, 10) NOT NULL,
  source           TEXT          NOT NULL DEFAULT 'payment',  -- 'payment' | 'admin'
  payment_ref      TEXT          NULL,
  created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS referral_earnings_referrer_email_idx
  ON public.referral_earnings (referrer_email);
CREATE INDEX IF NOT EXISTS referral_earnings_referrer_user_idx
  ON public.referral_earnings (referrer_user_id);

-- ---------------------------------------------------------------------------
-- RLS: enable on both, allow each authenticated user to READ only their own
-- rows. All writes are service-role only, which bypasses RLS entirely.
-- ---------------------------------------------------------------------------
ALTER TABLE public.referrals         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.referral_earnings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS referrals_read_own ON public.referrals;
CREATE POLICY referrals_read_own ON public.referrals
  FOR SELECT TO authenticated
  USING (referrer_user_id = auth.uid() OR referred_user_id = auth.uid());

DROP POLICY IF EXISTS referral_earnings_read_own ON public.referral_earnings;
CREATE POLICY referral_earnings_read_own ON public.referral_earnings
  FOR SELECT TO authenticated
  USING (referrer_user_id = auth.uid());
