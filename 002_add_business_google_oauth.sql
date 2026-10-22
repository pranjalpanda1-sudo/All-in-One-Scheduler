-- ====================================================================
-- Migration 002: Per-business Google Calendar OAuth connection
-- ====================================================================
-- Run this once against your Supabase project's SQL editor (or via the
-- Supabase CLI) AFTER 001_add_business_calendar_id.sql has already been
-- applied. Safe to re-run - uses IF NOT EXISTS throughout.
--
-- What it does:
--   Lets a business admin connect their OWN Google account (OAuth) so
--   booking confirmations are written directly to their real calendar,
--   with no manual "share this calendar with the service account" step.
-- ====================================================================

ALTER TABLE public.businesses
ADD COLUMN IF NOT EXISTS google_oauth_refresh_token TEXT;

ALTER TABLE public.businesses
ADD COLUMN IF NOT EXISTS google_oauth_email TEXT;

COMMENT ON COLUMN public.businesses.google_oauth_refresh_token IS
'OAuth refresh token for the business admin''s own Google account, used to write booking events directly to their calendar. NULL = not connected (falls back to the shared-calendar_id + service account method, if configured).';

COMMENT ON COLUMN public.businesses.google_oauth_email IS
'Email of the Google account connected via OAuth, shown in the admin dashboard as confirmation of which account is linked.';
