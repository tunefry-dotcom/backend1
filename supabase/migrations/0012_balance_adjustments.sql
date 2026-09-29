-- Manual admin balance adjustments (add/subtract with a required comment).
--
-- Additive only: adds one new table. Nothing existing is altered.
-- All writes happen via the service-role client (RLS denies anon/auth writes).
--
-- balance_adjustments is an immutable-per-row audit ledger, same model as
-- public.referral_earnings: it does NOT hold the live wallet balance itself.
-- The live balance folds SUM(balance_adjustments.amount) into
-- public.artist_balances.total_earned via earnings.service.recompute_balance()
-- (and the monthly migration/ingest_royalty_report.py recompute), so a row
-- here survives every future balance recompute. Deleting a row and
-- recomputing is the only "undo" — there is no separate credit-back step,
-- unlike public.withdrawal_requests.

-- ---------------------------------------------------------------------------
-- balance_adjustments: one row per admin add/subtract action.
-- amount is signed: positive = credit (add), negative = debit (subtract).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.balance_adjustments (
  id         UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID          NULL,
  user_email TEXT          NOT NULL,
  amount     NUMERIC(20, 10) NOT NULL,
  comment    TEXT          NOT NULL,
  created_at TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS balance_adjustments_user_email_idx
  ON public.balance_adjustments (user_email);

-- ---------------------------------------------------------------------------
-- RLS: enable, allow each authenticated user to READ only their own rows.
-- All writes are service-role only (via /admin/balance-adjustments), which
-- bypasses RLS entirely.
-- ---------------------------------------------------------------------------
ALTER TABLE public.balance_adjustments ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS balance_adjustments_read_own ON public.balance_adjustments;
CREATE POLICY balance_adjustments_read_own ON public.balance_adjustments
  FOR SELECT TO authenticated
  USING (user_id = auth.uid()
         OR lower(user_email) = lower(auth.jwt() ->> 'email'));
