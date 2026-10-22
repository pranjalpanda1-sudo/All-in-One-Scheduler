-- ====================================================================
-- STEADFAST MULTI-TENANT UPGRADE MIGRATION SCRIPT
-- ====================================================================
-- This script safely builds on top of the 'user_roles' table you created yesterday.
-- It upgrades existing tables, relaxes constraints, and adds the multi-tenant
-- tables (businesses, employees, bookings) + strict concurrency controls.

-- 1. Enable UUID Extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ====================================================================
-- 2. UPGRADE EXISTING user_roles TABLE (BACKWARDS-COMPATIBLE)
-- ====================================================================

-- Ensure table exists
CREATE TABLE IF NOT EXISTS public.user_roles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'CUSTOMER',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Add missing columns required for multi-tenant business ownership
ALTER TABLE public.user_roles ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE public.user_roles ADD COLUMN IF NOT EXISTS business_id UUID;
ALTER TABLE public.user_roles ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now());

-- Drop the old constraint that restricted role to only ('user', 'admin')
ALTER TABLE public.user_roles DROP CONSTRAINT IF EXISTS user_roles_role_check;

-- Add updated constraint allowing both new and legacy roles
ALTER TABLE public.user_roles ADD CONSTRAINT user_roles_role_check 
CHECK (role IN ('CUSTOMER', 'BUSINESS_ADMIN', 'user', 'admin'));

-- Normalize existing legacy roles: 'user' -> 'CUSTOMER', 'admin' -> 'BUSINESS_ADMIN'
UPDATE public.user_roles SET role = 'CUSTOMER' WHERE role = 'user';
UPDATE public.user_roles SET role = 'BUSINESS_ADMIN' WHERE role = 'admin';

-- Set default role to 'CUSTOMER'
ALTER TABLE public.user_roles ALTER COLUMN role SET DEFAULT 'CUSTOMER';

-- Update the auto-creation trigger function to insert 'CUSTOMER' and include email
CREATE OR REPLACE FUNCTION public.handle_new_user_role() 
RETURNS trigger 
LANGUAGE plpgsql 
SECURITY DEFINER 
SET search_path = public 
AS $$ 
BEGIN 
  INSERT INTO public.user_roles (id, role, email) 
  VALUES (new.id, 'CUSTOMER', new.email) 
  ON CONFLICT (id) DO UPDATE 
  SET email = COALESCE(EXCLUDED.email, public.user_roles.email); 
  RETURN new; 
END; 
$$;

-- Ensure trigger is active
DROP TRIGGER IF EXISTS on_auth_user_created_role ON auth.users;
CREATE TRIGGER on_auth_user_created_role 
AFTER INSERT ON auth.users 
FOR EACH ROW EXECUTE FUNCTION public.handle_new_user_role();

-- Update RLS on user_roles: allow users to view, insert, and update their own role
ALTER TABLE public.user_roles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Users can view own role" ON public.user_roles;
DROP POLICY IF EXISTS "Users can update own role" ON public.user_roles;
DROP POLICY IF EXISTS "Users can insert own role" ON public.user_roles;

CREATE POLICY "Users can view own role" 
ON public.user_roles FOR SELECT USING (auth.uid() = id);

CREATE POLICY "Users can update own role" 
ON public.user_roles FOR UPDATE USING (auth.uid() = id);

CREATE POLICY "Users can insert own role" 
ON public.user_roles FOR INSERT WITH CHECK (auth.uid() = id);

-- ====================================================================
-- 3. CREATE BUSINESSES TABLE (Multi-Tenant)
-- ====================================================================
CREATE TABLE IF NOT EXISTS public.businesses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    address TEXT,
    appointment_interval INTEGER NOT NULL DEFAULT 30, -- In minutes (e.g. 15, 30, 45, 60)
    max_bookings_per_slot INTEGER NOT NULL DEFAULT 1, -- Capacity per time slot
    allow_employee_selection BOOLEAN NOT NULL DEFAULT TRUE, -- Toggle staff picker
    opening_time TEXT NOT NULL DEFAULT '09:00', -- HH:MM
    closing_time TEXT NOT NULL DEFAULT '17:00', -- HH:MM
    google_calendar_id TEXT,
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- Foreign key link between user_roles and businesses
DO $$ 
BEGIN 
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_user_roles_business'
    ) THEN
        ALTER TABLE public.user_roles 
        ADD CONSTRAINT fk_user_roles_business 
        FOREIGN KEY (business_id) REFERENCES public.businesses(id) ON DELETE SET NULL;
    END IF;
END $$;

-- ====================================================================
-- 4. CREATE EMPLOYEES / STAFF TABLE
-- ====================================================================
CREATE TABLE IF NOT EXISTS public.employees (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    google_calendar_id TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- ====================================================================
-- 5. CREATE SLOTS / BOOKINGS TABLE
-- ====================================================================
CREATE TABLE IF NOT EXISTS public.bookings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
    employee_id UUID REFERENCES public.employees(id) ON DELETE SET NULL,
    customer_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    customer_name TEXT,
    customer_email TEXT NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'CONFIRMED' CHECK (status IN ('CONFIRMED', 'CANCELLED')),
    notes TEXT,
    google_event_id TEXT,
    created_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
);

-- ====================================================================
-- STRICT CONCURRENCY CONTROL & UNIQUE CONSTRAINTS
-- ====================================================================

-- Guarantee that an employee cannot be double-booked for the same start_time
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_employee_confirmed_slot
ON public.bookings (employee_id, start_time)
WHERE status = 'CONFIRMED' AND employee_id IS NOT NULL;

-- Fast index for querying business bookings
CREATE INDEX IF NOT EXISTS idx_bookings_business_time
ON public.bookings (business_id, start_time, status);

-- Fast index for customer dashboard
CREATE INDEX IF NOT EXISTS idx_bookings_customer
ON public.bookings (customer_id, start_time DESC);

-- ====================================================================
-- ATOMIC BOOKING STORED PROCEDURE (ROW-LOCKING & CAPACITY ENFORCEMENT)
-- ====================================================================
CREATE OR REPLACE FUNCTION public.book_slot_atomic(
    p_business_id UUID,
    p_employee_id UUID,
    p_customer_id UUID,
    p_customer_name TEXT,
    p_customer_email TEXT,
    p_start_time TIMESTAMPTZ,
    p_end_time TIMESTAMPTZ,
    p_notes TEXT DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    v_max_capacity INT;
    v_current_count INT;
    v_new_booking_id UUID;
    v_assigned_employee UUID := p_employee_id;
BEGIN
    -- 1. Lock the business record row to serialize slot validation for this business
    SELECT max_bookings_per_slot INTO v_max_capacity
    FROM public.businesses
    WHERE id = p_business_id
    FOR SHARE;

    IF v_max_capacity IS NULL THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'BUSINESS_NOT_FOUND',
            'message', 'Business record does not exist.'
        );
    END IF;

    -- 2. Verify total active bookings for this time slot do not exceed capacity
    SELECT COUNT(*) INTO v_current_count
    FROM public.bookings
    WHERE business_id = p_business_id
      AND start_time = p_start_time
      AND status = 'CONFIRMED';

    IF v_current_count >= v_max_capacity THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'SLOT_FULL',
            'message', 'This appointment slot is already completely booked. Please select another time.'
        );
    END IF;

    -- 3. If a specific employee is assigned, verify they are not double-booked
    IF v_assigned_employee IS NOT NULL THEN
        IF EXISTS (
            SELECT 1 FROM public.bookings
            WHERE employee_id = v_assigned_employee
              AND start_time = p_start_time
              AND status = 'CONFIRMED'
        ) THEN
            RETURN jsonb_build_object(
                'success', false,
                'error', 'EMPLOYEE_UNAVAILABLE',
                'message', 'The chosen employee is no longer available at this time slot.'
            );
        END IF;
    END IF;

    -- 4. Insert booking atomically
    INSERT INTO public.bookings (
        business_id,
        employee_id,
        customer_id,
        customer_name,
        customer_email,
        start_time,
        end_time,
        status,
        notes
    ) VALUES (
        p_business_id,
        v_assigned_employee,
        p_customer_id,
        p_customer_name,
        p_customer_email,
        p_start_time,
        p_end_time,
        'CONFIRMED',
        p_notes
    )
    RETURNING id INTO v_new_booking_id;

    RETURN jsonb_build_object(
        'success', true,
        'booking_id', v_new_booking_id,
        'message', 'Appointment confirmed successfully.'
    );
EXCEPTION
    WHEN unique_violation THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'CONCURRENCY_CONFLICT',
            'message', 'Another customer just booked this slot. Please choose another time.'
        );
    WHEN OTHERS THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'INTERNAL_ERROR',
            'message', SQLERRM
        );
END;
$$;

-- ====================================================================
-- ROW LEVEL SECURITY (RLS) POLICIES
-- ====================================================================
ALTER TABLE public.businesses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.employees ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bookings ENABLE ROW LEVEL SECURITY;

-- Businesses policies
DROP POLICY IF EXISTS "Public businesses are viewable by everyone" ON public.businesses;
DROP POLICY IF EXISTS "Users can create business" ON public.businesses;
DROP POLICY IF EXISTS "Owners can update business" ON public.businesses;

CREATE POLICY "Public businesses are viewable by everyone" 
ON public.businesses FOR SELECT USING (true);

CREATE POLICY "Users can create business" 
ON public.businesses FOR INSERT WITH CHECK (auth.uid() = owner_id);

CREATE POLICY "Owners can update business" 
ON public.businesses FOR UPDATE USING (auth.uid() = owner_id);

-- Employees policies
DROP POLICY IF EXISTS "Public employees are viewable" ON public.employees;
DROP POLICY IF EXISTS "Business owners manage employees" ON public.employees;

CREATE POLICY "Public employees are viewable" 
ON public.employees FOR SELECT USING (is_active = true);

CREATE POLICY "Business owners manage employees" 
ON public.employees FOR ALL USING (
    EXISTS (
        SELECT 1 FROM public.businesses b 
        WHERE b.id = employees.business_id AND b.owner_id = auth.uid()
    )
);

-- Bookings policies
DROP POLICY IF EXISTS "Customers view own bookings" ON public.bookings;
DROP POLICY IF EXISTS "Customers insert bookings" ON public.bookings;
DROP POLICY IF EXISTS "Cancel bookings" ON public.bookings;

CREATE POLICY "Customers view own bookings" 
ON public.bookings FOR SELECT USING (
    auth.uid() = customer_id OR 
    EXISTS (
        SELECT 1 FROM public.businesses b 
        WHERE b.id = bookings.business_id AND b.owner_id = auth.uid()
    )
);

CREATE POLICY "Customers insert bookings" 
ON public.bookings FOR INSERT WITH CHECK (true);

CREATE POLICY "Cancel bookings" 
ON public.bookings FOR UPDATE USING (
    auth.uid() = customer_id OR 
    EXISTS (
        SELECT 1 FROM public.businesses b 
        WHERE b.id = bookings.business_id AND b.owner_id = auth.uid()
    )
);
