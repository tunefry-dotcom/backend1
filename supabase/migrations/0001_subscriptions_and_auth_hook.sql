-- ============================================================================
-- Tunefry — subscriptions table + auto-assignment trigger + access-token hook
-- ============================================================================
-- Run this in the Supabase SQL editor (or via the CLI) on project
-- sdvmavpoyzeteduucdsq, THEN enable the hook (see step 6 at the bottom).
--
-- Design
--   * public.subscriptions is the SINGLE SOURCE OF TRUTH for a user's plan,
--     status and expiry. Only the service role (backend) may write it.
--   * A trigger on auth.users auto-creates a Free row for EVERY new user, so
--     plan assignment is a database invariant — it can't be missed by any signup
--     path (email, Google OAuth, dev/admin).
--   * A custom access-token hook injects the EFFECTIVE plan into every JWT at
--     issue-time (app_metadata.plan), downgrading expired/cancelled plans to
--     'free'. Gating then reads the JWT with zero per-request DB calls, yet always
--     reflects the table on the next token refresh.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Table
-- ----------------------------------------------------------------------------
create table if not exists public.subscriptions (
  user_id     uuid primary key references auth.users (id) on delete cascade,
  plan        text        not null default 'free',
  status      text        not null default 'active',
  started_at  timestamptz not null default now(),
  expires_at  timestamptz,                       -- NULL = never expires (free)
  payment_ref text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint subscriptions_status_check
    check (status in ('active', 'expired', 'cancelled')),
  constraint subscriptions_plan_check
    check (plan in ('free','single-song','starter','single-artist','double-artist','label'))
);

-- ----------------------------------------------------------------------------
-- 2. Keep updated_at fresh
-- ----------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_subscriptions_updated_at on public.subscriptions;
create trigger trg_subscriptions_updated_at
  before update on public.subscriptions
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- 3. Row-level security
--    Users may READ their own subscription. Nobody but the service role (which
--    bypasses RLS) may write it — there are deliberately no insert/update/delete
--    policies for authenticated users, so plans can't be self-escalated.
-- ----------------------------------------------------------------------------
alter table public.subscriptions enable row level security;

drop policy if exists "read own subscription" on public.subscriptions;
create policy "read own subscription"
  on public.subscriptions
  for select
  to authenticated
  using (auth.uid() = user_id);

-- ----------------------------------------------------------------------------
-- 4. Auto-assign a Free subscription to every new user (fixes all signup paths)
-- ----------------------------------------------------------------------------
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = ''
as $$
begin
  insert into public.subscriptions (user_id, plan, status)
  values (new.id, 'free', 'active')
  on conflict (user_id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Backfill: give any pre-existing users a Free row so nobody is left without one.
insert into public.subscriptions (user_id, plan, status)
select id, 'free', 'active' from auth.users
on conflict (user_id) do nothing;

-- ----------------------------------------------------------------------------
-- 5. Custom access-token hook — inject the EFFECTIVE plan into the JWT
--    Expired ('expires_at' in the past) or non-active plans resolve to 'free'.
-- ----------------------------------------------------------------------------
create or replace function public.custom_access_token_hook(event jsonb)
returns jsonb
language plpgsql
stable
as $$
declare
  claims   jsonb;
  sub      public.subscriptions%rowtype;
  eff_plan text := 'free';
begin
  select * into sub
  from public.subscriptions
  where user_id = (event ->> 'user_id')::uuid;

  if found then
    if sub.status = 'active'
       and (sub.expires_at is null or sub.expires_at > now()) then
      eff_plan := sub.plan;
    end if;
  end if;

  claims := coalesce(event -> 'claims', '{}'::jsonb);

  if claims ? 'app_metadata' then
    claims := jsonb_set(claims, '{app_metadata,plan}', to_jsonb(eff_plan));
  else
    claims := jsonb_set(claims, '{app_metadata}', jsonb_build_object('plan', eff_plan));
  end if;

  event := jsonb_set(event, '{claims}', claims);
  return event;
end;
$$;

-- Permissions required by GoTrue (runs the hook as role supabase_auth_admin).
grant usage on schema public to supabase_auth_admin;
grant execute on function public.custom_access_token_hook(jsonb) to supabase_auth_admin;
revoke execute on function public.custom_access_token_hook(jsonb) from authenticated, anon, public;

-- The hook (run as supabase_auth_admin) must be able to read the table.
grant select on public.subscriptions to supabase_auth_admin;

drop policy if exists "auth admin reads subscriptions for hook" on public.subscriptions;
create policy "auth admin reads subscriptions for hook"
  on public.subscriptions
  for select
  to supabase_auth_admin
  using (true);

-- ----------------------------------------------------------------------------
-- 6. MANUAL STEP — enable the hook (cannot be done in SQL):
--    Supabase Dashboard → Authentication → Hooks → "Customize Access Token (JWT) Claims"
--    → select schema "public", function "custom_access_token_hook" → Enable → Save.
--    After enabling, sign out & back in so a fresh JWT carries app_metadata.plan.
-- ============================================================================
