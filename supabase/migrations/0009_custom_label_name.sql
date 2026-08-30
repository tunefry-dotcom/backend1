-- Add custom label name column to profiles for Double Artist / Label plan users.
-- Safe to re-run: IF NOT EXISTS guard.
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS custom_label_name TEXT;
