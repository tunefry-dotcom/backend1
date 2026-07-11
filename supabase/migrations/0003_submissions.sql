-- Submission management table for admin review workflow.
-- Run this in Supabase SQL Editor once.
-- Service-role is used for all reads/writes — no RLS needed.

CREATE TABLE IF NOT EXISTS public.submissions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_email      TEXT NOT NULL,
  user_plan       TEXT NOT NULL DEFAULT 'free',
  submission_type TEXT NOT NULL CHECK (submission_type IN (
    'new_song', 'transfer_song',
    'new_album', 'transfer_album',
    'profile_mismatch', 'claim_removal', 'insta_link'
  )),
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'declined')),
  data            JSONB NOT NULL DEFAULT '{}',
  admin_note      TEXT NOT NULL DEFAULT '',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reviewed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS submissions_type_status_idx
  ON public.submissions (submission_type, status, created_at DESC);
