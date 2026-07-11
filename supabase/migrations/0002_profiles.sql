-- ============================================================================
-- Tunefry — user profiles (basic details) + auto-provision trigger
-- ============================================================================
-- Run in the Supabase SQL editor on the SAME project as 0001
-- (project sdvmavpoyzeteduucdsq — the one the Render backend uses).
--
-- Stores an artist's basic details. The backend (service role) reads/writes it;
-- the "profile complete" check that gates plan payment is computed from the
-- required fields (full_name, artist_name, phone, city, state, date_of_birth).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Table
-- ----------------------------------------------------------------------------
create table if not exists public.profiles (
  user_id       uuid primary key references auth.users (id) on delete cascade,
  full_name     text,
  artist_name   text,
  phone         text,
  date_of_birth date,
  gender        text,
  city          text,
  state         text,
  bio           text,
  spotify_url   text,
  instagram     text,
  youtube_url   text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- 2. Keep updated_at fresh (reuses public.set_updated_at() defined in 0001)
-- ----------------------------------------------------------------------------
drop trigger if exists trg_profiles_updated_at on public.profiles;
create trigger trg_profiles_updated_at
  before update on public.profiles
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- 3. Row-level security — a user may READ only their own profile. Writes go
--    through the service-role backend (which bypasses RLS), so there are no
--    user-facing insert/update policies.
-- ----------------------------------------------------------------------------
alter table public.profiles enable row level security;

drop policy if exists "read own profile" on public.profiles;
create policy "read own profile"
  on public.profiles
  for select
  to authenticated
  using (auth.uid() = user_id);

-- ----------------------------------------------------------------------------
-- 4. Auto-create a (blank) profile for every new user, seeding full_name from
--    the signup metadata when present. Covers all signup paths.
-- ----------------------------------------------------------------------------
create or replace function public.handle_new_user_profile()
returns trigger
language plpgsql
security definer set search_path = ''
as $$
begin
  insert into public.profiles (user_id, full_name)
  values (new.id, new.raw_user_meta_data ->> 'full_name')
  on conflict (user_id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created_profile on auth.users;
create trigger on_auth_user_created_profile
  after insert on auth.users
  for each row execute function public.handle_new_user_profile();

-- Backfill: give existing users a profile row (seed full_name from metadata).
insert into public.profiles (user_id, full_name)
select id, raw_user_meta_data ->> 'full_name' from auth.users
on conflict (user_id) do nothing;
