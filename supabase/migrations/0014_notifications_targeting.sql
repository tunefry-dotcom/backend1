-- Personal (non-broadcast) notification targeting.
--
-- Additive only: adds one nullable column + one index to the existing
-- public.notifications table. No existing rows are touched — every row
-- written before this migration has recipient_email = NULL, which keeps
-- its current "broadcast to everyone" behavior unchanged.
--
-- NULL   recipient_email -> broadcast, shown to every user (existing behavior)
-- non-null recipient_email -> personal notice, shown only to that one email
-- (used by Tunefry Daily blog approve/decline).

ALTER TABLE public.notifications
  ADD COLUMN IF NOT EXISTS recipient_email TEXT;

CREATE INDEX IF NOT EXISTS notifications_recipient_idx
  ON public.notifications (recipient_email, created_at DESC);
