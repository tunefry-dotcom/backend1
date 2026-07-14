-- Home page CMS content (single-row table, id is always 1)
CREATE TABLE IF NOT EXISTS public.home_content (
  id                   integer PRIMARY KEY,
  artists              jsonb NOT NULL DEFAULT '[]'::jsonb,
  yt_testimonials      jsonb NOT NULL DEFAULT '[]'::jsonb,
  trending_links       jsonb NOT NULL DEFAULT '[]'::jsonb,
  latest_release_link  text,
  popular_artist_links jsonb NOT NULL DEFAULT '[]'::jsonb,
  top_hits_links       jsonb NOT NULL DEFAULT '[]'::jsonb,
  updated_at           timestamptz DEFAULT now()
);

INSERT INTO public.home_content (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

ALTER TABLE public.home_content ENABLE ROW LEVEL SECURITY;

-- Public read only; only service role can write (no self-escalation possible)
CREATE POLICY "home_content_public_read"
  ON public.home_content
  FOR SELECT
  USING (true);
