-- Single-row admin-editable USD -> INR conversion rate used by the
-- Excel-import bulk song_stats endpoint (POST /admin/song-stats/import).
-- Same singleton-table pattern as public.home_content (0003).
CREATE TABLE IF NOT EXISTS public.fx_rate_settings (
  id          integer PRIMARY KEY,
  usd_to_inr  numeric(10,4) NOT NULL,
  updated_at  timestamptz DEFAULT now()
);

ALTER TABLE public.fx_rate_settings ENABLE ROW LEVEL SECURITY;

-- No policies — service-role only (same as submissions/song_stats). No seed
-- row either: GET /admin/fx-rate returns null until an admin sets it once.
