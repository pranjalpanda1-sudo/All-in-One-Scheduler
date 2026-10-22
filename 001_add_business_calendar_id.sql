-- ====================================================================
-- Migration 001: Per-business Google Calendar sync
-- ====================================================================
-- Run this once against your Supabase project's SQL editor (or via the
-- Supabase CLI) AFTER the base schema in supabase_schema.sql has already
-- been applied. Safe to re-run - uses IF NOT EXISTS / OR REPLACE throughout.
--
-- What it does:
--   Adds a `calendar_id` column to `businesses` so each business admin can
--   connect their own Google Calendar. The backend writes real events to
--   this calendar via a Google service account (see GOOGLE_SERVICE_ACCOUNT_JSON
--   in .env). For this to work, the business admin must share their Google
--   Calendar with the service account's client_email (found inside that
--   JSON) and grant it "Make changes to events" permission.
-- ====================================================================

ALTER TABLE public.businesses
ADD COLUMN IF NOT EXISTS calendar_id TEXT;

COMMENT ON COLUMN public.businesses.calendar_id IS
'Google Calendar ID this business has shared with the service account, used for live event sync on booking confirmation. NULL = calendar sync disabled for this business.';
