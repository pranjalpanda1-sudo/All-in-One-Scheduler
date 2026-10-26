-- ====================================================================
-- Migration 003: AI-Powered Business Onboarding & Custom Intake System
-- ====================================================================
-- This migration script creates tables and RLS policies for:
-- 1. Business Profiles (AI-extracted business understanding, services, requirements)
-- 2. Intake Forms (Multi-tenant, versioned, draft vs published state)
-- 3. Intake Questions (Dynamic types: single choice, dropdown, multi-choice, text, yes/no, conditional)
-- 4. Appointment Intake Responses (Customer answers linked to bookings)
-- 5. AI Appointment Summaries (Groq openai/gpt-oss-120b generated summaries & prep notes)
-- ====================================================================

-- 1. BUSINESS PROFILES TABLE
CREATE TABLE IF NOT EXISTS public.business_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE UNIQUE,
    category TEXT,
    description TEXT,
    services JSONB DEFAULT '[]'::jsonb,
    raw_onboarding_transcript TEXT,
    requirements JSONB DEFAULT '{}'::jsonb,
    onboarding_status TEXT NOT NULL DEFAULT 'COMPLETED' CHECK (onboarding_status IN ('PENDING', 'COMPLETED')),
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- Index for fast business profile lookup
CREATE INDEX IF NOT EXISTS idx_business_profiles_business_id
ON public.business_profiles (business_id);

-- 2. INTAKE FORMS TABLE
CREATE TABLE IF NOT EXISTS public.intake_forms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'Appointment Intake Form',
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'PUBLISHED')),
    version INTEGER NOT NULL DEFAULT 1,
    services JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- Index for querying business forms
CREATE INDEX IF NOT EXISTS idx_intake_forms_business_status
ON public.intake_forms (business_id, status);

-- 3. INTAKE QUESTIONS TABLE
CREATE TABLE IF NOT EXISTS public.intake_questions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    form_id UUID NOT NULL REFERENCES public.intake_forms(id) ON DELETE CASCADE,
    question_text TEXT NOT NULL,
    question_type TEXT NOT NULL CHECK (question_type IN ('single_choice', 'dropdown', 'multiple_choice', 'short_text', 'long_text', 'yes_no')),
    options JSONB DEFAULT '[]'::jsonb,
    is_required BOOLEAN NOT NULL DEFAULT FALSE,
    display_order INTEGER NOT NULL DEFAULT 0,
    conditional_logic JSONB DEFAULT NULL,
    help_text TEXT,
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- Index for form questions ordering
CREATE INDEX IF NOT EXISTS idx_intake_questions_form_order
ON public.intake_questions (form_id, display_order ASC);

-- 4. APPOINTMENT INTAKE RESPONSES TABLE
CREATE TABLE IF NOT EXISTS public.appointment_intake_responses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id UUID NOT NULL REFERENCES public.bookings(id) ON DELETE CASCADE,
    business_id UUID NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
    form_id UUID REFERENCES public.intake_forms(id) ON DELETE SET NULL,
    service_selected TEXT,
    responses JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- Index for querying appointment responses
CREATE INDEX IF NOT EXISTS idx_intake_responses_booking
ON public.appointment_intake_responses (booking_id);

CREATE INDEX IF NOT EXISTS idx_intake_responses_business
ON public.appointment_intake_responses (business_id);

-- 5. AI APPOINTMENT SUMMARIES TABLE
CREATE TABLE IF NOT EXISTS public.ai_appointment_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id UUID NOT NULL REFERENCES public.bookings(id) ON DELETE CASCADE UNIQUE,
    business_id UUID NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
    summary TEXT NOT NULL,
    key_details JSONB DEFAULT '{}'::jsonb,
    preparation_notes TEXT,
    status TEXT NOT NULL DEFAULT 'COMPLETED' CHECK (status IN ('COMPLETED', 'FAILED', 'FALLBACK')),
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- Index for business appointment summaries
CREATE INDEX IF NOT EXISTS idx_ai_summaries_business
ON public.ai_appointment_summaries (business_id);

-- ====================================================================
-- ROW LEVEL SECURITY (RLS) POLICIES
-- ====================================================================
ALTER TABLE public.business_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intake_forms ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intake_questions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.appointment_intake_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_appointment_summaries ENABLE ROW LEVEL SECURITY;

-- Business Profiles Policies: Business owner can manage their profile; anyone can view published profile info
DROP POLICY IF EXISTS "Business owners can manage business profiles" ON public.business_profiles;
CREATE POLICY "Business owners can manage business profiles"
ON public.business_profiles FOR ALL USING (
    EXISTS (
        SELECT 1 FROM public.businesses b
        WHERE b.id = business_profiles.business_id AND b.owner_id = auth.uid()
    )
);

DROP POLICY IF EXISTS "Public can view business profiles" ON public.business_profiles;
CREATE POLICY "Public can view business profiles"
ON public.business_profiles FOR SELECT USING (true);

-- Intake Forms Policies: Business owners manage all forms; customers can view published forms
DROP POLICY IF EXISTS "Business owners manage intake forms" ON public.intake_forms;
CREATE POLICY "Business owners manage intake forms"
ON public.intake_forms FOR ALL USING (
    EXISTS (
        SELECT 1 FROM public.businesses b
        WHERE b.id = intake_forms.business_id AND b.owner_id = auth.uid()
    )
);

DROP POLICY IF EXISTS "Public can view published forms" ON public.intake_forms;
CREATE POLICY "Public can view published forms"
ON public.intake_forms FOR SELECT USING (status = 'PUBLISHED');

-- Intake Questions Policies: Business owners manage questions; customers can view questions for published forms
DROP POLICY IF EXISTS "Business owners manage intake questions" ON public.intake_questions;
CREATE POLICY "Business owners manage intake questions"
ON public.intake_questions FOR ALL USING (
    EXISTS (
        SELECT 1 FROM public.intake_forms f
        JOIN public.businesses b ON b.id = f.business_id
        WHERE f.id = intake_questions.form_id AND b.owner_id = auth.uid()
    )
);

DROP POLICY IF EXISTS "Public can view published questions" ON public.intake_questions;
CREATE POLICY "Public can view published questions"
ON public.intake_questions FOR SELECT USING (
    EXISTS (
        SELECT 1 FROM public.intake_forms f
        WHERE f.id = intake_questions.form_id AND f.status = 'PUBLISHED'
    )
);

-- Appointment Responses Policies:
-- 1. Anyone can submit responses during booking
-- 2. Business owners can view responses for their businesses
-- 3. Customers can view responses for their own bookings
DROP POLICY IF EXISTS "Customers can insert intake responses" ON public.appointment_intake_responses;
CREATE POLICY "Customers can insert intake responses"
ON public.appointment_intake_responses FOR INSERT WITH CHECK (true);

DROP POLICY IF EXISTS "Business owners can view intake responses" ON public.appointment_intake_responses;
CREATE POLICY "Business owners can view intake responses"
ON public.appointment_intake_responses FOR SELECT USING (
    EXISTS (
        SELECT 1 FROM public.businesses b
        WHERE b.id = appointment_intake_responses.business_id AND b.owner_id = auth.uid()
    )
    OR
    EXISTS (
        SELECT 1 FROM public.bookings bk
        WHERE bk.id = appointment_intake_responses.booking_id AND bk.customer_id = auth.uid()
    )
);

-- AI Summaries Policies: Business owners can view summaries for their appointments
DROP POLICY IF EXISTS "Business owners can view appointment summaries" ON public.ai_appointment_summaries;
CREATE POLICY "Business owners can view appointment summaries"
ON public.ai_appointment_summaries FOR ALL USING (
    EXISTS (
        SELECT 1 FROM public.businesses b
        WHERE b.id = ai_appointment_summaries.business_id AND b.owner_id = auth.uid()
    )
);
