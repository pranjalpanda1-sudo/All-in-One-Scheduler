-- ====================================================================
-- Migration 004: AI-generated appointment intake questionnaire
-- ====================================================================
-- Run this once against your Supabase project's SQL editor (or via the
-- Supabase CLI) AFTER 001-003 have already been applied. Safe to re-run -
-- uses IF NOT EXISTS / DROP POLICY IF EXISTS throughout.
--
-- What it does:
--   Backs the AI onboarding feature: business owner describes their business
--   (voice or text) -> Groq generates intake questions -> owner reviews/edits/
--   approves -> questions are shown to customers at booking -> customer answers
--   are summarized by AI for the owner before the appointment.
-- ====================================================================

-- Structured business profile extracted by AI from the owner's description
CREATE TABLE IF NOT EXISTS public.business_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL UNIQUE REFERENCES public.businesses(id) ON DELETE CASCADE,
    category TEXT,
    description TEXT,
    services JSONB NOT NULL DEFAULT '[]'::jsonb,
    requirements JSONB NOT NULL DEFAULT '{}'::jsonb,
    onboarding_status TEXT NOT NULL DEFAULT 'COMPLETED',
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- Intake form: one DRAFT (editable, unpublished) and one PUBLISHED (live) per business
CREATE TABLE IF NOT EXISTS public.intake_forms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'Appointment Intake Form',
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'PUBLISHED')),
    version INTEGER NOT NULL DEFAULT 1,
    services JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- Up to 5 intake questions per form (multiple choice / dropdown / multi-select / text / yes-no)
CREATE TABLE IF NOT EXISTS public.intake_questions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    form_id UUID NOT NULL REFERENCES public.intake_forms(id) ON DELETE CASCADE,
    question_text TEXT NOT NULL,
    question_type TEXT NOT NULL DEFAULT 'short_text'
        CHECK (question_type IN ('single_choice', 'dropdown', 'multiple_choice', 'short_text', 'long_text', 'yes_no')),
    options JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_required BOOLEAN NOT NULL DEFAULT FALSE,
    display_order INTEGER NOT NULL DEFAULT 0,
    conditional_logic JSONB,
    help_text TEXT,
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- Customer's answers to the published intake form, linked to their booking
CREATE TABLE IF NOT EXISTS public.appointment_intake_responses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id UUID NOT NULL REFERENCES public.bookings(id) ON DELETE CASCADE,
    business_id UUID NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
    form_id UUID REFERENCES public.intake_forms(id) ON DELETE SET NULL,
    service_selected TEXT,
    responses JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- AI-generated plain-English summary of the customer's responses, for the owner
CREATE TABLE IF NOT EXISTS public.ai_appointment_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id UUID NOT NULL UNIQUE REFERENCES public.bookings(id) ON DELETE CASCADE,
    business_id UUID NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
    summary TEXT NOT NULL,
    preparation_notes TEXT,
    status TEXT NOT NULL DEFAULT 'COMPLETED',
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_intake_forms_business_status
ON public.intake_forms (business_id, status, version DESC);

CREATE INDEX IF NOT EXISTS idx_intake_questions_form_order
ON public.intake_questions (form_id, display_order);

CREATE INDEX IF NOT EXISTS idx_intake_responses_booking
ON public.appointment_intake_responses (booking_id);

CREATE INDEX IF NOT EXISTS idx_ai_summaries_booking
ON public.ai_appointment_summaries (booking_id);

-- RLS: business owner manages their own profile/forms/questions/summaries;
-- anyone can read a PUBLISHED form (needed for the public booking page);
-- customers can insert their own intake responses when booking.
ALTER TABLE public.business_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intake_forms ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intake_questions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.appointment_intake_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_appointment_summaries ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Owners manage own business profile" ON public.business_profiles;
CREATE POLICY "Owners manage own business profile"
ON public.business_profiles FOR ALL USING (
    EXISTS (SELECT 1 FROM public.businesses b WHERE b.id = business_profiles.business_id AND b.owner_id = auth.uid())
);

DROP POLICY IF EXISTS "Published intake forms are viewable by everyone" ON public.intake_forms;
DROP POLICY IF EXISTS "Owners manage own intake forms" ON public.intake_forms;
CREATE POLICY "Published intake forms are viewable by everyone"
ON public.intake_forms FOR SELECT USING (status = 'PUBLISHED');
CREATE POLICY "Owners manage own intake forms"
ON public.intake_forms FOR ALL USING (
    EXISTS (SELECT 1 FROM public.businesses b WHERE b.id = intake_forms.business_id AND b.owner_id = auth.uid())
);

DROP POLICY IF EXISTS "Published intake questions are viewable by everyone" ON public.intake_questions;
DROP POLICY IF EXISTS "Owners manage own intake questions" ON public.intake_questions;
CREATE POLICY "Published intake questions are viewable by everyone"
ON public.intake_questions FOR SELECT USING (
    EXISTS (SELECT 1 FROM public.intake_forms f WHERE f.id = intake_questions.form_id AND f.status = 'PUBLISHED')
);
CREATE POLICY "Owners manage own intake questions"
ON public.intake_questions FOR ALL USING (
    EXISTS (
        SELECT 1 FROM public.intake_forms f
        JOIN public.businesses b ON b.id = f.business_id
        WHERE f.id = intake_questions.form_id AND b.owner_id = auth.uid()
    )
);

DROP POLICY IF EXISTS "Customers insert own intake responses" ON public.appointment_intake_responses;
DROP POLICY IF EXISTS "Owners view own intake responses" ON public.appointment_intake_responses;
CREATE POLICY "Customers insert own intake responses"
ON public.appointment_intake_responses FOR INSERT WITH CHECK (true);
CREATE POLICY "Owners view own intake responses"
ON public.appointment_intake_responses FOR SELECT USING (
    EXISTS (SELECT 1 FROM public.businesses b WHERE b.id = appointment_intake_responses.business_id AND b.owner_id = auth.uid())
);

DROP POLICY IF EXISTS "Owners view own ai summaries" ON public.ai_appointment_summaries;
CREATE POLICY "Owners view own ai summaries"
ON public.ai_appointment_summaries FOR SELECT USING (
    EXISTS (SELECT 1 FROM public.businesses b WHERE b.id = ai_appointment_summaries.business_id AND b.owner_id = auth.uid())
);
