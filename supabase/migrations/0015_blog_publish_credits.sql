-- Tunefry Daily publish-credit ledger (one free article per user, then paid
-- credit packs: single = 1 credit, bundle_20 = 20 credits).
--
-- Additive only: adds two new tables. Nothing existing is altered.
-- All writes happen via the service-role client (RLS denies anon/auth writes).
--
-- blog_publish_credits holds the LIVE per-user balance (free_article_used +
-- credits_remaining) — read-modify-write on submit/purchase, same
-- no-distributed-locking convention as earnings.service.recompute_balance.
--
-- blog_credit_purchases is an immutable audit ledger, same model as
-- public.referral_earnings / public.balance_adjustments. razorpay_payment_id
-- is UNIQUE — this is the replay guard: a duplicate verify call for the same
-- payment hits a unique-violation and is rejected before any credit is
-- granted twice (a DB-enforced version of billing's payment_ref equality
-- check in billing/router.py).

CREATE TABLE IF NOT EXISTS public.blog_publish_credits (
  user_email        TEXT        PRIMARY KEY,
  free_article_used BOOLEAN     NOT NULL DEFAULT false,
  credits_remaining INTEGER     NOT NULL DEFAULT 0,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.blog_credit_purchases (
  id                  UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  user_email          TEXT          NOT NULL,
  razorpay_order_id   TEXT          NOT NULL,
  razorpay_payment_id TEXT          NOT NULL UNIQUE,
  pack                TEXT          NOT NULL CHECK (pack IN ('single', 'bundle_20')),
  amount_inr          NUMERIC(10,2) NOT NULL,
  credits_granted     INTEGER       NOT NULL,
  created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS blog_credit_purchases_email_idx
  ON public.blog_credit_purchases (user_email, created_at DESC);

-- ---------------------------------------------------------------------------
-- RLS: enable, allow each authenticated user to READ only their own rows.
-- All writes are service-role only (via /blog/credits/*), which bypasses
-- RLS entirely.
-- ---------------------------------------------------------------------------
ALTER TABLE public.blog_publish_credits ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS blog_publish_credits_read_own ON public.blog_publish_credits;
CREATE POLICY blog_publish_credits_read_own ON public.blog_publish_credits
  FOR SELECT TO authenticated
  USING (lower(user_email) = lower(auth.jwt() ->> 'email'));

ALTER TABLE public.blog_credit_purchases ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS blog_credit_purchases_read_own ON public.blog_credit_purchases;
CREATE POLICY blog_credit_purchases_read_own ON public.blog_credit_purchases
  FOR SELECT TO authenticated
  USING (lower(user_email) = lower(auth.jwt() ->> 'email'));
