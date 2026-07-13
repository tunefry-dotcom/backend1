-- Add Apple Music profile URL to artist profiles.
-- Run once in Supabase SQL Editor.

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS apple_music_url TEXT;

-- Queue of new artists (no Spotify/Apple at upload time) who need links added
-- after their first song is approved by admin.
CREATE TABLE IF NOT EXISTS public.new_artist_queue (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_email      TEXT NOT NULL,
  artist_name     TEXT NOT NULL DEFAULT '',
  submission_id   UUID REFERENCES public.submissions(id) ON DELETE SET NULL,
  spotify_url     TEXT NOT NULL DEFAULT '',
  apple_music_url TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'updated')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS new_artist_queue_status_idx
  ON public.new_artist_queue (status, created_at DESC);
