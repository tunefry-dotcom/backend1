-- Record what the buyer actually paid (after the Diwali offer) on the subscription.
-- Additive and idempotent; run once in the Supabase SQL editor.
-- The app degrades gracefully if this has not been applied yet (assign_plan retries
-- without these columns), but amount_paid_inr will not be recorded until it is.
alter table public.subscriptions
  add column if not exists amount_paid_inr numeric,
  add column if not exists discount_pct integer;
