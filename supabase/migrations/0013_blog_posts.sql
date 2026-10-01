-- Tunefry Daily blog posts (artist-submitted drafts + admin-authored posts).
--
-- Additive only: adds one new table. Nothing existing is altered.
-- All writes happen via the service-role client (RLS denies anon/auth writes).
--
-- original_title/original_body are the artist's raw draft exactly as submitted
-- (immutable, kept for audit + as the admin's AI-rewrite input). final_title/
-- final_body are the admin-edited copy — the ONLY thing ever rendered publicly.
-- Artists never see final_title/final_body or any AI-rewritten output.
--
-- cover_image_keys is an ordered JSONB array of R2 keys: index 0 is always the
-- card thumbnail + article-top hero image; index 1 (tunefry-only, optional) is
-- rendered inline mid-article. Never more than 2 entries.

CREATE TABLE IF NOT EXISTS public.blog_posts (
  id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  slug           TEXT        NOT NULL,
  author_type    TEXT        NOT NULL CHECK (author_type IN ('artist', 'tunefry')),
  author_email   TEXT        NULL,
  author_name    TEXT        NOT NULL DEFAULT '',
  category       TEXT        NOT NULL CHECK (category IN (
                    'artist_journey', 'song_release', 'informative', 'success_story'
                  )),
  status         TEXT        NOT NULL DEFAULT 'pending' CHECK (status IN (
                    'pending', 'approved', 'declined'
                  )),
  original_title TEXT        NOT NULL DEFAULT '',
  original_body  TEXT        NOT NULL DEFAULT '',
  final_title    TEXT        NULL,
  final_body     TEXT        NULL,
  cover_image_keys JSONB     NOT NULL DEFAULT '[]',
  admin_note     TEXT        NOT NULL DEFAULT '',
  is_featured    BOOLEAN     NOT NULL DEFAULT false,
  is_popular     BOOLEAN     NOT NULL DEFAULT false,
  reviewed_at    TIMESTAMPTZ NULL,
  published_at   TIMESTAMPTZ NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS blog_posts_slug_idx
  ON public.blog_posts (slug);

-- Public listing: approved posts by category, newest first.
CREATE INDEX IF NOT EXISTS blog_posts_public_listing_idx
  ON public.blog_posts (status, category, published_at DESC);

-- Related-articles lookup: same category, approved, newest first.
CREATE INDEX IF NOT EXISTS blog_posts_related_idx
  ON public.blog_posts (category, published_at DESC)
  WHERE status = 'approved';

-- Admin review tabs: Artist / Tunefry, optionally filtered by status.
CREATE INDEX IF NOT EXISTS blog_posts_admin_tab_idx
  ON public.blog_posts (author_type, status, created_at DESC);

-- "My posts" (artist's own /daily page).
CREATE INDEX IF NOT EXISTS blog_posts_author_email_idx
  ON public.blog_posts (author_email, status, created_at DESC);

-- ---------------------------------------------------------------------------
-- RLS: enable, allow each authenticated artist to READ only their own rows
-- (any status) plus any publicly approved row. All writes are service-role
-- only (via /blog/* and /admin/blog/* routes), which bypasses RLS entirely.
-- ---------------------------------------------------------------------------
ALTER TABLE public.blog_posts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS blog_posts_read_own_or_approved ON public.blog_posts;
CREATE POLICY blog_posts_read_own_or_approved ON public.blog_posts
  FOR SELECT TO authenticated
  USING (status = 'approved'
         OR lower(author_email) = lower(auth.jwt() ->> 'email'));

DROP POLICY IF EXISTS blog_posts_read_approved_public ON public.blog_posts;
CREATE POLICY blog_posts_read_approved_public ON public.blog_posts
  FOR SELECT TO anon
  USING (status = 'approved');
