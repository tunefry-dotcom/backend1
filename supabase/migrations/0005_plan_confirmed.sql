-- Add plan_confirmed: users must explicitly activate a plan (even Free) before uploading.
-- Default false so new trigger-created rows are unconfirmed and must visit /plan first.
alter table public.subscriptions
  add column if not exists plan_confirmed boolean not null default false;

-- Backfill: grandfather anyone on a paid tier or who has ever made a payment.
-- Existing free users stay false and must re-activate Free on the plan page.
update public.subscriptions
  set plan_confirmed = true
  where plan <> 'free' or payment_ref is not null;
