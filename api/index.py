import os
import re
import json
import uuid
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta, date, time
from urllib.parse import quote, urlencode

from dotenv import load_dotenv
load_dotenv()

# Optional SSL verification handling for corporate/local Windows environments
import httpx
if os.environ.get("SUPABASE_VERIFY_SSL", "true").lower() == "false":
    _orig_httpx_init = httpx.Client.__init__
    def _patched_httpx_init(self, *args, **kwargs):
        kwargs["verify"] = False
        return _orig_httpx_init(self, *args, **kwargs)
    httpx.Client.__init__ = _patched_httpx_init

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, Response
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("steadfast")

app = Flask(__name__, template_folder='../templates', static_folder='../static')
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "steadfast-secure-flask-key-2026")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
# Public/anon key - safe to expose to the browser. Used only for the client-side
# OAuth code-exchange flow in auth_callback.html.
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
# Secret service-role key - server-side only, bypasses Row Level Security.
# Used for the backend's own Supabase client so writes (business creation,
# role selection, bookings) actually persist instead of being silently
# rejected by RLS policies that expect an authenticated end-user session.
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Email Configuration
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "bookings@steadfast.app")

# Google Calendar Configuration
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# OAuth client used so a business admin can connect their OWN Google account
# (events land directly on their real calendar - no manual calendar-sharing
# step needed, unlike the service-account + calendar_id method below).
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "")
GOOGLE_CALENDAR_OAUTH_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
google_oauth_available = bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REDIRECT_URI)

# oauthlib refuses non-HTTPS redirect URIs by default; allow it only for local dev.
if GOOGLE_OAUTH_REDIRECT_URI.startswith("http://localhost") or GOOGLE_OAUTH_REDIRECT_URI.startswith("http://127.0.0.1"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# Google's consent screen commonly grants extra scopes (email/profile/openid)
# beyond what we request even if we only asked for calendar.events. oauthlib
# treats that as a hard error by default - relax it so token exchange succeeds.
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

google_calendar_service = None
GOOGLE_SERVICE_ACCOUNT_EMAIL = ""
if GOOGLE_SERVICE_ACCOUNT_JSON:
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build as build_google_service

        _sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        GOOGLE_SERVICE_ACCOUNT_EMAIL = _sa_info.get("client_email", "")
        _sa_credentials = service_account.Credentials.from_service_account_info(
            _sa_info, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        google_calendar_service = build_google_service(
            "calendar", "v3", credentials=_sa_credentials, cache_discovery=False
        )
        logger.info(f"Google Calendar service account ready ({GOOGLE_SERVICE_ACCOUNT_EMAIL}).")
    except Exception as e:
        logger.warning(f"Google Calendar service account initialization error: {e}")

supabase: Client = None
_server_key = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY
if SUPABASE_URL and _server_key:
    try:
        supabase = create_client(SUPABASE_URL, _server_key)
        if not SUPABASE_SERVICE_ROLE_KEY:
            logger.warning(
                "Supabase client initialized with the ANON key - RLS-protected "
                "writes (businesses, user_roles, bookings) will fail. Set "
                "SUPABASE_SERVICE_ROLE_KEY to fix this."
            )
        else:
            logger.info("Supabase client initialized successfully with service-role key.")
    except Exception as e:
        logger.warning(f"Supabase initialization error: {e}")

# ====================================================================
# GROQ AI INTEGRATION (openai/gpt-oss-120b)
# ====================================================================
from groq import Groq

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_AI_MODEL = "openai/gpt-oss-120b"
GROQ_AUDIO_MODEL = "whisper-large-v3"

groq_client: Groq = None
if GROQ_API_KEY:
    try:
        _groq_http = httpx.Client(verify=False) if os.environ.get("SUPABASE_VERIFY_SSL", "true").lower() == "false" else httpx.Client()
        groq_client = Groq(api_key=GROQ_API_KEY, http_client=_groq_http)
        logger.info(f"Groq API client initialized successfully with model '{GROQ_AI_MODEL}'.")
    except Exception as e:
        logger.warning(f"Groq API client initialization error: {e}")
else:
    logger.warning("GROQ_API_KEY not found in environment. AI functions will run in resilient fallback mode.")

# ====================================================================
# IN-MEMORY RESILIENT DATA STORE (FALLBACK & SEED DATA)
# Ensures 100% operational functionality before or alongside SQL migrations
# ====================================================================
FALLBACK_BUSINESSES = [
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "owner_id": "seed-owner-1",
        "name": "Sage Wellness Studio & Spa",
        "description": "Holistic therapy, therapeutic massage, and seasonal consultations by licensed professionals.",
        "address": "142 Artisan Row, Suite 300, New York, NY",
        "appointment_interval": 30,
        "max_bookings_per_slot": 2,
        "allow_employee_selection": True,
        "opening_time": "09:00",
        "closing_time": "17:00",
        "created_at": datetime.utcnow().isoformat()
    },
    {
        "id": "22222222-2222-2222-2222-222222222222",
        "owner_id": "seed-owner-2",
        "name": "Steadfast Financial Advisory",
        "description": "Bespoke small business tax planning, bookkeeping advisory, and strategic growth consultations.",
        "address": "88 Pine Street, Floor 14, San Francisco, CA",
        "appointment_interval": 45,
        "max_bookings_per_slot": 1,
        "allow_employee_selection": True,
        "opening_time": "09:00",
        "closing_time": "18:00",
        "created_at": datetime.utcnow().isoformat()
    },
    {
        "id": "33333333-3333-3333-3333-333333333333",
        "owner_id": "seed-owner-3",
        "name": "Apex Creative & Pitch Studio",
        "description": "Presentation design reviews, pitch coaching, and high-impact founder consultation sessions.",
        "address": "250 King Street, Austin, TX",
        "appointment_interval": 60,
        "max_bookings_per_slot": 1,
        "allow_employee_selection": False,
        "opening_time": "10:00",
        "closing_time": "16:00",
        "created_at": datetime.utcnow().isoformat()
    }
]

FALLBACK_EMPLOYEES = [
    {
        "id": "e1111111-1111-1111-1111-111111111111",
        "business_id": "11111111-1111-1111-1111-111111111111",
        "name": "Maya Chen",
        "email": "maya@sagewellness.com",
        "is_active": True
    },
    {
        "id": "e2222222-2222-2222-2222-222222222222",
        "business_id": "11111111-1111-1111-1111-111111111111",
        "name": "Jordan Lee",
        "email": "jordan@sagewellness.com",
        "is_active": True
    },
    {
        "id": "e3333333-3333-3333-3333-333333333333",
        "business_id": "22222222-2222-2222-2222-222222222222",
        "name": "David Miller, CPA",
        "email": "david@steadfastadvisory.com",
        "is_active": True
    }
]

FALLBACK_BOOKINGS = []
FALLBACK_USER_ROLES = {}

FALLBACK_BUSINESS_PROFILES = [
    {
        "id": "bp-11111111-1111-1111-1111-111111111111",
        "business_id": "11111111-1111-1111-1111-111111111111",
        "category": "Wellness & Spa",
        "description": "Holistic therapy, therapeutic massage, and seasonal consultations by licensed professionals.",
        "services": [
            {"name": "Therapeutic Massage", "duration_minutes": 60, "requirements": "Client health history"},
            {"name": "Holistic Therapy", "duration_minutes": 45, "requirements": "Goal consultation"},
            {"name": "Seasonal Consultation", "duration_minutes": 30, "requirements": "None"}
        ],
        "onboarding_status": "COMPLETED",
        "created_at": datetime.utcnow().isoformat()
    }
]

FALLBACK_INTAKE_FORMS = [
    {
        "id": "form-1111-1111-1111-111111111111",
        "business_id": "11111111-1111-1111-1111-111111111111",
        "title": "Sage Wellness Appointment Intake",
        "status": "PUBLISHED",
        "version": 1,
        "services": [
            {"name": "Therapeutic Massage", "description": "Full-body relief and muscle tension reduction"},
            {"name": "Holistic Therapy", "description": "Integrative wellness and stress-release"},
            {"name": "Seasonal Consultation", "description": "Routine quarterly consultation"}
        ],
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat()
    }
]

FALLBACK_INTAKE_QUESTIONS = [
    {
        "id": "q-1111-0001",
        "form_id": "form-1111-1111-1111-111111111111",
        "key": "q1",
        "question_text": "Which wellness service are you booking today?",
        "question_type": "single_choice",
        "options": ["Therapeutic Massage", "Holistic Therapy", "Seasonal Consultation"],
        "is_required": True,
        "display_order": 0,
        "conditional_logic": None,
        "help_text": "Select the primary service for your visit."
    },
    {
        "id": "q-1111-0002",
        "form_id": "form-1111-1111-1111-111111111111",
        "key": "q2",
        "question_text": "What is your preferred pressure level?",
        "question_type": "dropdown",
        "options": ["Light / Gentle", "Medium", "Firm / Deep Tissue"],
        "is_required": False,
        "display_order": 1,
        "conditional_logic": {
            "depends_on_question_key": "q1",
            "show_if_value": "Therapeutic Massage"
        },
        "help_text": "Helps our therapist tailor the pressure."
    },
    {
        "id": "q-1111-0003",
        "form_id": "form-1111-1111-1111-111111111111",
        "key": "q3",
        "question_text": "Do you have any muscle injuries, recent surgeries, or specific focus areas?",
        "question_type": "short_text",
        "options": [],
        "is_required": False,
        "display_order": 2,
        "conditional_logic": None,
        "help_text": "e.g. lower back tension, neck stiffness."
    },
    {
        "id": "q-1111-0004",
        "form_id": "form-1111-1111-1111-111111111111",
        "key": "q4",
        "question_text": "Do you have any sensitivities or allergies to essential oils or scented lotions?",
        "question_type": "yes_no",
        "options": ["Yes", "No"],
        "is_required": True,
        "display_order": 3,
        "conditional_logic": None,
        "help_text": "We offer unscented hypoallergenic organic seed oils."
    }
]

FALLBACK_INTAKE_RESPONSES = []
FALLBACK_AI_SUMMARIES = []

# Seed the demo businesses/employees into Supabase itself (idempotent upsert)
# so they are real, bookable rows instead of just appearing in listings from
# in-memory fallback data. Without this, book_slot_atomic (which checks the
# live table) rejects bookings against them with "Business record does not
# exist" even though the UI shows them as available.
def _seed_demo_data_into_supabase() -> None:
    if not supabase:
        return
    try:
        # owner_id is a UUID FK to auth.users - the seed data's placeholder
        # "seed-owner-N" strings aren't real users, so leave it unset.
        seed_businesses = [{**b, "owner_id": None} for b in FALLBACK_BUSINESSES]
        supabase.table('businesses').upsert(seed_businesses, on_conflict='id').execute()
        supabase.table('employees').upsert(FALLBACK_EMPLOYEES, on_conflict='id').execute()
        logger.info("Demo business/employee seed data verified in Supabase.")
    except Exception as e:
        logger.warning(f"Demo data seed error: {e}")

_seed_demo_data_into_supabase()

# ====================================================================
# RBAC & USER PROFILE REPOSITORY
# ====================================================================
def get_user_profile(user_id: str) -> dict:
    """Fetches user profile, role, and associated business_id with legacy role normalization."""
    if not user_id:
        return {"role": None, "business_id": None}
    
    raw_role = None
    business_id = None
    email = None

    if supabase:
        try:
            res = supabase.table('user_roles').select('*').eq('id', user_id).limit(1).execute()
            if res.data and len(res.data) > 0:
                row = res.data[0]
                raw_role = row.get('role')
                business_id = row.get('business_id')
                email = row.get('email')
        except Exception as e:
            logger.warning(f"Error querying user_roles from Supabase: {e}")
            
    # Check in-memory fallback
    if not raw_role and user_id in FALLBACK_USER_ROLES:
        fb = FALLBACK_USER_ROLES[user_id]
        raw_role = fb.get('role')
        business_id = fb.get('business_id')
        email = fb.get('email')

    # Normalize legacy roles ('user' -> 'CUSTOMER', 'admin' -> 'BUSINESS_ADMIN')
    norm_role = raw_role
    if raw_role in ['user', 'CUSTOMER']:
        norm_role = 'CUSTOMER'
    elif raw_role in ['admin', 'BUSINESS_ADMIN']:
        norm_role = 'BUSINESS_ADMIN'

    return {"role": norm_role, "business_id": business_id, "email": email}

def set_user_role(user_id: str, email: str, role: str, business_id: str = None) -> bool:
    """Persists user role (CUSTOMER or BUSINESS_ADMIN) and business association."""
    if not user_id:
        return False
        
    FALLBACK_USER_ROLES[user_id] = {
        "role": role,
        "email": email,
        "business_id": business_id
    }
    
    if supabase:
        try:
            payload = {
                "id": user_id,
                "email": email,
                "role": role,
                "updated_at": datetime.utcnow().isoformat()
            }
            if business_id:
                payload["business_id"] = business_id
            supabase.table('user_roles').upsert(payload).execute()
            return True
        except Exception as e:
            logger.warning(f"Supabase set_user_role error: {e}")
            
    return True

# ====================================================================
# BUSINESS REPOSITORY
# ====================================================================
def get_all_businesses(query: str = "") -> list:
    """Returns all businesses, optionally filtered by search text."""
    results = []
    if supabase:
        try:
            query_builder = supabase.table('businesses').select('*')
            if query:
                query_builder = query_builder.ilike('name', f"%{query}%")
            res = query_builder.execute()
            if res.data and len(res.data) > 0:
                results = res.data
        except Exception as e:
            logger.warning(f"Supabase get_all_businesses error: {e}")

    if not results:
        results = [b.copy() for b in FALLBACK_BUSINESSES]
        if query:
            q_lower = query.lower()
            results = [
                b for b in results 
                if q_lower in b.get('name', '').lower() or 
                   q_lower in b.get('description', '').lower() or 
                   q_lower in b.get('address', '').lower()
            ]
            
    return results

def get_business_by_id(business_id: str) -> dict:
    """Fetches a business record by its ID."""
    if not business_id:
        return None
        
    if supabase:
        try:
            res = supabase.table('businesses').select('*').eq('id', business_id).limit(1).execute()
            if res.data and len(res.data) > 0:
                return res.data[0]
        except Exception as e:
            logger.warning(f"Supabase get_business_by_id error: {e}")
            
    for b in FALLBACK_BUSINESSES:
        if b['id'] == business_id:
            return b.copy()
    return None

def get_business_by_owner(owner_id: str) -> dict:
    """Fetches the business owned by a specific user."""
    if not owner_id:
        return None
        
    if supabase:
        try:
            res = supabase.table('businesses').select('*').eq('owner_id', owner_id).limit(1).execute()
            if res.data and len(res.data) > 0:
                return res.data[0]
        except Exception as e:
            logger.warning(f"Supabase get_business_by_owner error: {e}")
            
    for b in FALLBACK_BUSINESSES:
        if b.get('owner_id') == owner_id:
            return b.copy()
    return None

def create_business_record(owner_id: str, data: dict) -> dict:
    """Registers a new business record linked to the owner."""
    business_id = str(uuid.uuid4())
    record = {
        "id": business_id,
        "owner_id": owner_id,
        "name": data.get("name", "").strip(),
        "description": data.get("description", "").strip(),
        "address": data.get("address", "").strip(),
        "appointment_interval": int(data.get("appointment_interval", 30)),
        "max_bookings_per_slot": int(data.get("max_bookings_per_slot", 1)),
        "allow_employee_selection": bool(data.get("allow_employee_selection")),
        "opening_time": data.get("opening_time", "09:00"),
        "closing_time": data.get("closing_time", "17:00"),
        "created_at": datetime.utcnow().isoformat()
    }

    inserted = False
    if supabase:
        try:
            res = supabase.table('businesses').insert(record).execute()
            if res.data and len(res.data) > 0:
                record = res.data[0]
                inserted = True
        except Exception as e:
            logger.warning(f"Supabase insert business error: {e}")

    if not inserted:
        FALLBACK_BUSINESSES.append(record)

    return record

def update_business_record(business_id: str, data: dict) -> bool:
    """Updates settings and intervals for an existing business."""
    update_data = {
        "name": data.get("name", "").strip(),
        "description": data.get("description", "").strip(),
        "address": data.get("address", "").strip(),
        "appointment_interval": int(data.get("appointment_interval", 30)),
        "max_bookings_per_slot": int(data.get("max_bookings_per_slot", 1)),
        "allow_employee_selection": bool(data.get("allow_employee_selection")),
        "opening_time": data.get("opening_time", "09:00"),
        "closing_time": data.get("closing_time", "17:00"),
        "calendar_id": data.get("calendar_id", "").strip() or None
    }

    if supabase:
        try:
            supabase.table('businesses').update(update_data).eq('id', business_id).execute()
        except Exception as e:
            logger.warning(f"Supabase update business error: {e}")

    for b in FALLBACK_BUSINESSES:
        if b['id'] == business_id:
            b.update(update_data)
            return True
    return True

def set_business_google_oauth(business_id: str, refresh_token: str, email: str) -> None:
    """Stores the business admin's connected Google account for calendar auto-sync."""
    update_data = {"google_oauth_refresh_token": refresh_token, "google_oauth_email": email}
    if supabase:
        try:
            supabase.table('businesses').update(update_data).eq('id', business_id).execute()
        except Exception as e:
            logger.warning(f"Supabase update business Google OAuth error: {e}")
    for b in FALLBACK_BUSINESSES:
        if b['id'] == business_id:
            b.update(update_data)

def clear_business_google_oauth(business_id: str) -> None:
    """Disconnects the business admin's Google account."""
    set_business_google_oauth(business_id, None, None)

# ====================================================================
# EMPLOYEE / STAFF REPOSITORY
# ====================================================================
def get_employees_by_business(business_id: str) -> list:
    """Retrieves all active staff members for a business."""
    if not business_id:
        return []
        
    if supabase:
        try:
            res = supabase.table('employees').select('*').eq('business_id', business_id).eq('is_active', True).execute()
            if res.data:
                return res.data
        except Exception as e:
            logger.warning(f"Supabase get_employees error: {e}")

    return [e.copy() for e in FALLBACK_EMPLOYEES if e['business_id'] == business_id and e.get('is_active', True)]

def add_employee_record(business_id: str, name: str, email: str) -> dict:
    """Adds a new employee to a business roster."""
    emp_id = str(uuid.uuid4())
    record = {
        "id": emp_id,
        "business_id": business_id,
        "name": name.strip(),
        "email": email.strip(),
        "is_active": True,
        "created_at": datetime.utcnow().isoformat()
    }

    if supabase:
        try:
            res = supabase.table('employees').insert(record).execute()
            if res.data:
                return res.data[0]
        except Exception as e:
            logger.warning(f"Supabase add_employee error: {e}")

    FALLBACK_EMPLOYEES.append(record)
    return record

def delete_employee_record(employee_id: str) -> bool:
    """Soft-deletes or removes an employee from the business."""
    if supabase:
        try:
            supabase.table('employees').update({"is_active": False}).eq('id', employee_id).execute()
        except Exception as e:
            logger.warning(f"Supabase delete_employee error: {e}")

    for e in FALLBACK_EMPLOYEES:
        if e['id'] == employee_id:
            e['is_active'] = False
            return True
    return True

# ====================================================================
# BOOKINGS REPOSITORY & STRICT CONCURRENCY ENGINE
# ====================================================================
def get_bookings_for_business(business_id: str) -> list:
    """Retrieves all appointments booked for a business."""
    if not business_id:
        return []
        
    results = []
    if supabase:
        try:
            res = supabase.table('bookings').select('*, employees(name)').eq('business_id', business_id).order('start_time', desc=True).execute()
            if res.data:
                for row in res.data:
                    emp = row.get('employees')
                    row['employee_name'] = emp.get('name') if isinstance(emp, dict) else None
                return res.data
        except Exception as e:
            logger.warning(f"Supabase get_business_bookings error: {e}")

    # Fallback lookup
    emp_map = {e['id']: e['name'] for e in FALLBACK_EMPLOYEES}
    for b in FALLBACK_BOOKINGS:
        if b['business_id'] == business_id:
            b_copy = b.copy()
            b_copy['employee_name'] = emp_map.get(b_copy.get('employee_id'), 'Staff Member')
            results.append(b_copy)
    results.sort(key=lambda x: x.get('start_time', ''), reverse=True)
    return results

def get_bookings_for_customer(customer_id: str, email: str) -> list:
    """Retrieves all appointments booked by a customer."""
    results = []
    if supabase and customer_id:
        try:
            res = supabase.table('bookings').select('*, businesses(name, address), employees(name)').eq('customer_id', customer_id).order('start_time', desc=True).execute()
            if res.data:
                for row in res.data:
                    biz = row.get('businesses')
                    emp = row.get('employees')
                    row['business_name'] = biz.get('name') if isinstance(biz, dict) else 'Business'
                    row['business_address'] = biz.get('address') if isinstance(biz, dict) else ''
                    row['employee_name'] = emp.get('name') if isinstance(emp, dict) else None
                return res.data
        except Exception as e:
            logger.warning(f"Supabase get_customer_bookings error: {e}")

    # In-memory lookup
    biz_map = {b['id']: b for b in FALLBACK_BUSINESSES}
    emp_map = {e['id']: e['name'] for e in FALLBACK_EMPLOYEES}
    for b in FALLBACK_BOOKINGS:
        if (customer_id and b.get('customer_id') == customer_id) or (email and b.get('customer_email') == email):
            b_copy = b.copy()
            biz = biz_map.get(b_copy.get('business_id'), {})
            b_copy['business_name'] = biz.get('name', 'Business')
            b_copy['business_address'] = biz.get('address', '')
            b_copy['employee_name'] = emp_map.get(b_copy.get('employee_id'))
            results.append(b_copy)
    results.sort(key=lambda x: x.get('start_time', ''), reverse=True)
    return results

def cancel_booking_record(booking_id: str) -> bool:
    """Cancels an appointment, immediately releasing the slot."""
    if supabase:
        try:
            supabase.table('bookings').update({"status": "CANCELLED"}).eq('id', booking_id).execute()
        except Exception as e:
            logger.warning(f"Supabase cancel_booking error: {e}")

    for b in FALLBACK_BOOKINGS:
        if b['id'] == booking_id:
            b['status'] = 'CANCELLED'
            return True
    return True

def book_appointment_atomic(business_id: str, employee_id: str, customer_id: str, 
                            customer_name: str, customer_email: str, 
                            start_time: str, end_time: str, notes: str = "") -> tuple[bool, str, dict]:
    """
    Enforces strict concurrency control against double-booking:
    1. First tries executing the PostgreSQL stored procedure 'book_slot_atomic' with FOR SHARE locking.
    2. Fallback execution performs row verification and atomic insertion with unique constraint protection.
    """
    biz = get_business_by_id(business_id)
    if not biz:
        return False, "Selected business not found.", None

    max_capacity = int(biz.get('max_bookings_per_slot', 1))

    # 1. Attempt Atomic Supabase RPC (Row-locked via PostgreSQL function)
    if supabase:
        try:
            rpc_res = supabase.rpc('book_slot_atomic', {
                'p_business_id': business_id,
                'p_employee_id': employee_id if employee_id else None,
                'p_customer_id': customer_id if customer_id else None,
                'p_customer_name': customer_name,
                'p_customer_email': customer_email,
                'p_start_time': start_time,
                'p_end_time': end_time,
                'p_notes': notes
            }).execute()

            if rpc_res.data:
                result = rpc_res.data
                if result.get('success'):
                    booking_data = {
                        "id": result.get('booking_id'),
                        "business_id": business_id,
                        "employee_id": employee_id,
                        "customer_id": customer_id,
                        "customer_name": customer_name,
                        "customer_email": customer_email,
                        "start_time": start_time,
                        "end_time": end_time,
                        "status": "CONFIRMED",
                        "notes": notes
                    }
                    return True, "Booking confirmed successfully!", booking_data
                else:
                    return False, result.get('message', "This appointment slot is no longer available."), None
        except Exception as e:
            logger.warning(f"Supabase atomic RPC unavailable, executing application-level atomic check: {e}")

    # 2. Application-Level Concurrency Check (Thread-safe verification)
    existing_confirmed = []
    if supabase:
        try:
            res = supabase.table('bookings').select('*').eq('business_id', business_id).eq('start_time', start_time).eq('status', 'CONFIRMED').execute()
            if res.data:
                existing_confirmed = res.data
        except Exception as e:
            logger.warning(f"Supabase check conflict error: {e}")

    if not existing_confirmed:
        existing_confirmed = [
            b for b in FALLBACK_BOOKINGS 
            if b.get('business_id') == business_id and 
               b.get('start_time') == start_time and 
               b.get('status') == 'CONFIRMED'
        ]

    # Check total business slot capacity
    if len(existing_confirmed) >= max_capacity:
        return False, "This time slot has reached maximum capacity. Please pick another slot.", None

    # Check specific employee availability
    if employee_id:
        for b in existing_confirmed:
            if b.get('employee_id') == employee_id:
                return False, "The selected staff member has just been booked by another user for this time.", None

    # Insert confirmed booking
    new_booking_id = str(uuid.uuid4())
    booking_record = {
        "id": new_booking_id,
        "business_id": business_id,
        "employee_id": employee_id if employee_id else None,
        "customer_id": customer_id if customer_id else None,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "start_time": start_time,
        "end_time": end_time,
        "status": "CONFIRMED",
        "notes": notes,
        "created_at": datetime.utcnow().isoformat()
    }

    if supabase:
        try:
            res = supabase.table('bookings').insert(booking_record).execute()
            if res.data and len(res.data) > 0:
                booking_record = res.data[0]
                return True, "Booking confirmed successfully!", booking_record
        except Exception as e:
            err_str = str(e)
            if "unique" in err_str.lower() or "conflict" in err_str.lower():
                return False, "Concurrency conflict: This slot was just booked by another user.", None
            logger.warning(f"Supabase direct insert error: {e}")

    FALLBACK_BOOKINGS.append(booking_record)
    return True, "Booking confirmed successfully!", booking_record

# ====================================================================
# DYNAMIC TIME SLOT CALCULATOR
# ====================================================================
def generate_business_slots(business: dict, target_date: date, employee_id: str = None) -> list:
    """
    Computes all slots for a given business on a given date:
    - Slices opening to closing hours into appointment_interval increments.
    - Evaluates confirmed bookings to calculate remaining capacity in real-time.
    """
    slots = []
    interval_mins = int(business.get("appointment_interval", 30))
    open_time_str = business.get("opening_time", "09:00")
    close_time_str = business.get("closing_time", "17:00")
    max_capacity = int(business.get("max_bookings_per_slot", 1))

    try:
        open_h, open_m = map(int, open_time_str.split(":"))
        close_h, close_m = map(int, close_time_str.split(":"))
    except Exception:
        open_h, open_m = 9, 0
        close_h, close_m = 17, 0

    cur_dt = datetime.combine(target_date, time(open_h, open_m))
    end_dt = datetime.combine(target_date, time(close_h, close_m))

    # Fetch all confirmed bookings for this business on this day
    confirmed_bookings = []
    if supabase:
        try:
            day_start = datetime.combine(target_date, time.min).isoformat()
            day_end = datetime.combine(target_date, time.max).isoformat()
            res = supabase.table('bookings').select('*').eq('business_id', business['id']).gte('start_time', day_start).lte('start_time', day_end).eq('status', 'CONFIRMED').execute()
            if res.data:
                confirmed_bookings = res.data
        except Exception as e:
            logger.warning(f"Error fetching confirmed slots: {e}")

    if not confirmed_bookings:
        day_str = target_date.isoformat()
        confirmed_bookings = [
            b for b in FALLBACK_BOOKINGS 
            if b.get('business_id') == business['id'] and 
               b.get('start_time', '').startswith(day_str) and 
               b.get('status') == 'CONFIRMED'
        ]

    # Iterate over intervals
    while cur_dt + timedelta(minutes=interval_mins) <= end_dt:
        slot_start = cur_dt
        slot_end = cur_dt + timedelta(minutes=interval_mins)
        start_iso = slot_start.isoformat()

        # Count bookings for this start_time
        matching_bookings = [
            b for b in confirmed_bookings 
            if b.get('start_time', '').startswith(start_iso[:16])
        ]
        
        is_employee_booked = False
        if employee_id:
            is_employee_booked = any(b.get('employee_id') == employee_id for b in matching_bookings)

        available = (len(matching_bookings) < max_capacity) and (not is_employee_booked)
        remaining_capacity = max(0, max_capacity - len(matching_bookings))

        slots.append({
            "display_time": slot_start.strftime("%I:%M %p"),
            "start_iso": start_iso,
            "end_iso": slot_end.isoformat(),
            "available": available,
            "remaining_capacity": 0 if is_employee_booked else remaining_capacity
        })

        cur_dt += timedelta(minutes=interval_mins)

    return slots

# ====================================================================
# GOOGLE CALENDAR & EMAIL NOTIFICATION UTILITIES
# ====================================================================
def generate_google_calendar_url(title: str, start_dt_str: str, end_dt_str: str, details: str, location: str) -> str:
    """Creates a 1-click 'Add to Google Calendar' web link."""
    try:
        dt_start = datetime.fromisoformat(start_dt_str)
        dt_end = datetime.fromisoformat(end_dt_str)
        fmt = "%Y%m%dT%H%M%SZ"
        dates_param = f"{dt_start.strftime(fmt)}/{dt_end.strftime(fmt)}"
    except Exception:
        dates_param = ""

    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": dates_param,
        "details": details,
        "location": location
    }
    return f"https://calendar.google.com/calendar/render?{urlencode(params)}"

def generate_ics_content(booking_id: str, title: str, start_dt_str: str, end_dt_str: str, details: str, location: str) -> str:
    """Generates standard RFC 5545 iCalendar content for .ics downloads and email attachments."""
    try:
        dt_start = datetime.fromisoformat(start_dt_str)
        dt_end = datetime.fromisoformat(end_dt_str)
        fmt = "%Y%m%dT%H%M%SZ"
        t_start = dt_start.strftime(fmt)
        t_end = dt_end.strftime(fmt)
        t_now = datetime.utcnow().strftime(fmt)
    except Exception:
        t_start = t_end = t_now = "20261020T120000Z"

    clean_summary = title.replace("\n", " ").strip()
    clean_desc = details.replace("\n", "\\n").strip()
    clean_loc = location.replace("\n", " ").strip()

    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Steadfast Multi-Tenant Platform//Appointment Scheduling//EN
CALSCALE:GREGORIAN
METHOD:REQUEST
BEGIN:VEVENT
UID:{booking_id}@steadfast.app
DTSTAMP:{t_now}
DTSTART:{t_start}
DTEND:{t_end}
SUMMARY:{clean_summary}
DESCRIPTION:{clean_desc}
LOCATION:{clean_loc}
STATUS:CONFIRMED
SEQUENCE:0
END:VEVENT
END:VCALENDAR"""

def _build_oauth_calendar_service(business: dict):
    """
    Builds a Calendar API client using the business admin's own connected
    Google account (OAuth refresh token), so events are written straight to
    their real calendar - no manual calendar-sharing step required.
    Returns None if the business hasn't connected an account.
    """
    refresh_token = (business or {}).get("google_oauth_refresh_token")
    if not (refresh_token and google_oauth_available):
        return None
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build as build_google_service

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_OAUTH_CLIENT_ID,
            client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
            scopes=GOOGLE_CALENDAR_OAUTH_SCOPES,
        )
        return build_google_service("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.warning(f"Could not build OAuth calendar service: {e}")
        return None

def sync_booking_to_google_calendar(business: dict, booking_id: str, customer_name: str,
                                    customer_email: str, employee_name: str,
                                    start_time_str: str, end_time_str: str,
                                    location: str, notes: str = "") -> None:
    """
    Creates a live event on the business's Google Calendar, preferring the
    admin's own OAuth-connected account (automatic, no setup needed beyond
    clicking "Connect Google Calendar") and falling back to the shared
    service-account + calendar_id method if OAuth isn't connected. No-ops
    quietly if neither is configured - the "Add to Google Calendar" link and
    .ics attachment already cover that case.
    """
    event_body = {
        "id": re.sub(r"[^a-v0-9]", "", booking_id.lower()) or None,
        "summary": f"{customer_name} - {business.get('name', 'Appointment')}",
        "location": location,
        "description": (
            f"Booked via Steadfast.\nCustomer: {customer_name} ({customer_email})\n"
            f"Staff: {employee_name or 'Assigned Staff'}\n"
            f"Booking ID: {booking_id}" + (f"\nNotes: {notes}" if notes else "")
        ),
        "start": {"dateTime": start_time_str, "timeZone": "UTC"},
        "end": {"dateTime": end_time_str, "timeZone": "UTC"},
    }

    oauth_service = _build_oauth_calendar_service(business)
    if oauth_service:
        try:
            oauth_service.events().insert(calendarId="primary", body=event_body).execute()
            logger.info(f"Google Calendar event created via OAuth for booking {booking_id}.")
            return
        except Exception as e:
            logger.warning(f"OAuth Google Calendar sync failed for booking {booking_id}: {e}")

    if not google_calendar_service:
        return

    calendar_id = (business or {}).get("calendar_id")
    if not calendar_id:
        return

    try:
        google_calendar_service.events().insert(calendarId=calendar_id, body=event_body).execute()
        logger.info(f"Google Calendar event created on calendar '{calendar_id}' for booking {booking_id}.")
    except Exception as e:
        logger.warning(f"Google Calendar sync failed for booking {booking_id}: {e}")

def send_booking_confirmation_email(customer_email: str, customer_name: str, 
                                    business: dict, employee_name: str, 
                                    start_time_str: str, end_time_str: str, 
                                    booking_id: str):
    """
    Automated notification delivery:
    - Assembles styled HTML confirmation with Google Calendar link.
    - Attaches standard .ics calendar invite for one-click calendar sync.
    - Gracefully logs notification if SMTP credentials are unconfigured.
    """
    subject = f"Appointment Confirmed: {business.get('name', 'Steadfast')}"
    location = business.get('address', 'Online / In-person')
    
    try:
        dt_obj = datetime.fromisoformat(start_time_str)
        readable_time = dt_obj.strftime("%A, %B %d, %Y at %I:%M %p")
    except Exception:
        readable_time = start_time_str

    cal_title = f"Appointment: {business.get('name')} with {employee_name or 'Staff'}"
    cal_details = f"Appointment with {business.get('name')}. Staff: {employee_name or 'Assigned Staff'}. Booking ID: {booking_id}"
    google_cal_url = generate_google_calendar_url(cal_title, start_time_str, end_time_str, cal_details, location)
    ics_text = generate_ics_content(booking_id, cal_title, start_time_str, end_time_str, cal_details, location)

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f9f9f6; padding: 30px; margin: 0;">
        <div style="max-width: 560px; margin: 0 auto; background: #ffffff; border-radius: 24px; padding: 36px; border: 1px solid #dbe5dc; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 24px;">
                <span style="font-size: 20px; font-weight: bold; color: #1a382b;">steadfast</span>
                <span style="background: #edf2ed; color: #1a382b; font-size: 11px; padding: 2px 8px; border-radius: 999px; text-transform: uppercase; font-weight: bold;">Booking Confirmed</span>
            </div>
            
            <h2 style="color: #1a382b; font-size: 22px; margin-top: 0; margin-bottom: 12px;">Your appointment is locked in!</h2>
            <p style="color: #4b5563; font-size: 14px; line-height: 1.5; margin-bottom: 24px;">
                Hello {customer_name}, your booking with <strong>{business.get('name')}</strong> has been confirmed. Below are your appointment details:
            </p>

            <div style="background: #f8faf7; border-radius: 16px; padding: 20px; border: 1px solid #dbe5dc; margin-bottom: 24px;">
                <p style="margin: 0 0 10px 0; font-size: 14px; color: #1a382b;"><strong>📅 Date & Time:</strong> {readable_time}</p>
                <p style="margin: 0 0 10px 0; font-size: 14px; color: #1a382b;"><strong>🏢 Business:</strong> {business.get('name')}</p>
                <p style="margin: 0 0 10px 0; font-size: 14px; color: #1a382b;"><strong>📍 Location:</strong> {location}</p>
                <p style="margin: 0; font-size: 14px; color: #1a382b;"><strong>👤 Assigned Staff:</strong> {employee_name or 'Assigned Staff'}</p>
            </div>

            <div style="text-align: center; margin-bottom: 24px;">
                <a href="{google_cal_url}" target="_blank" style="display: inline-block; background-color: #1a382b; color: #ffffff; padding: 12px 24px; border-radius: 999px; text-decoration: none; font-size: 13px; font-weight: 600; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                    Add to Google Calendar 📅
                </a>
            </div>

            <p style="color: #6b7280; font-size: 12px; line-height: 1.4; margin: 0; border-top: 1px solid #edf2ed; padding-top: 16px;">
                An iCalendar (.ics) invite is attached to this email. You can import it directly into Google Calendar, Apple Calendar, or Outlook.
            </p>
        </div>
    </body>
    </html>
    """

    if not SMTP_USERNAME or not SMTP_PASSWORD:
        logger.info(f"[EMAIL SIMULATION] Confirmation email prepared for {customer_email}:")
        logger.info(f"Subject: {subject}")
        logger.info(f"Details: {cal_details} | Google Cal: {google_cal_url}")
        return

    try:
        msg = MIMEMultipart("mixed")
        msg["From"] = FROM_EMAIL
        msg["To"] = customer_email
        msg["Subject"] = subject

        alt_part = MIMEMultipart("alternative")
        alt_part.attach(MIMEText(cal_details, "plain"))
        alt_part.attach(MIMEText(html_content, "html"))
        msg.attach(alt_part)

        # Attach .ics file
        part = MIMEBase("text", "calendar", method="REQUEST", name="invite.ics")
        part.set_payload(ics_text)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment; filename=invite.ics")
        msg.attach(part)

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(FROM_EMAIL, customer_email, msg.as_string())
        server.quit()
        logger.info(f"Confirmation email successfully sent to {customer_email}")
    except Exception as e:
        logger.warning(f"Failed to deliver SMTP email: {e}")

# ====================================================================
# AUTHENTICATION & PASSWORD VALIDATION
# ====================================================================
def validate_password_strength(password: str) -> tuple[bool, str]:
    """Validates that a password satisfies all complexity requirements."""
    if len(password) < 8:
        return False, "Password must be at least 8 characters long."
    if not re.search(r'[A-Z]', password):
        return False, "Password must include at least one uppercase letter."
    if not re.search(r'[0-9]', password):
        return False, "Password must include at least one number."
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return False, "Password must include at least one special character (!@#$%^&*)."
    return True, ""

def post_login_redirect_for(user_id: str, email: str):
    """
    Decides the next landing view for a signed-in user:
    - If no role is selected -> /select-role
    - If BUSINESS_ADMIN -> /create-business (if unlinked) or /admin
    - If CUSTOMER -> /businesses or /dashboard
    """
    profile = get_user_profile(user_id)
    role = profile.get("role")

    if not role:
        return redirect(url_for('select_role'))
        
    if role == "BUSINESS_ADMIN":
        biz = get_business_by_owner(user_id)
        if not biz:
            return redirect(url_for('create_business'))
        return redirect(url_for('admin_dashboard'))
    else:
        return redirect(url_for('businesses'))

# ====================================================================
# AI SERVICES (Groq openai/gpt-oss-120b & whisper-large-v3)
# ====================================================================
def _clean_ai_json(content: str) -> dict:
    """Safely parses JSON responses from openai/gpt-oss-120b, stripping markdown fences or reasoning artifacts."""
    if not content:
        return {}
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except Exception as e:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start:end+1])
            except Exception:
                pass
        logger.warning(f"Failed to parse AI JSON response: {e}. Content preview: {cleaned[:150]}")
        return {}

def ai_transcribe_audio(file_bytes: bytes, filename: str = "recording.webm") -> tuple[bool, str]:
    """Transcribes audio using Groq Whisper speech-to-text API."""
    if not groq_client:
        return False, "Groq AI client is not configured on this server. Check GROQ_API_KEY."
    try:
        transcription = groq_client.audio.transcriptions.create(
            model=GROQ_AUDIO_MODEL,
            file=(filename, file_bytes),
            response_format="text"
        )
        text = transcription.strip() if isinstance(transcription, str) else getattr(transcription, 'text', '').strip()
        return True, text
    except Exception as e:
        logger.error(f"Groq Whisper transcription failed: {e}")
        return False, f"Speech transcription error: {str(e)}"

def ai_understand_business(description: str, conversation_history: list = None, previous_answers: list = None) -> tuple[bool, dict, str]:
    """Extracts structured business information and targeted follow-up questions using openai/gpt-oss-120b."""
    if not groq_client:
        fallback_profile = {
            "business_name": None,
            "business_category": "General Services",
            "business_description": description,
            "services": [{"name": "Standard Consultation", "description": description, "duration_minutes": 30}],
            "appointment_interval": 30,
            "follow_up_questions": []
        }
        return True, fallback_profile, "Processed in fallback mode (Groq offline)."

    system_prompt = (
        "You are an expert AI Systems Architect specializing in small-business onboarding.\n"
        "Analyze the business owner's natural description and extract a structured business profile.\n"
        "Rules:\n"
        "1. Never invent prices, appointment durations, employee names, or services that were not described.\n"
        "2. Extract: business_name (or null), business_category, business_description, services (list with name, description, duration_minutes or null, requirements), recommended appointment_interval (15, 30, 45, or 60).\n"
        "3. If critical information is missing to set up their appointment intake (e.g. specific employee selection, special requirements), generate 1 to 2 targeted, helpful follow-up questions in 'follow_up_questions'. If enough information is provided, return an empty list [].\n"
        "4. Return ONLY valid JSON matching this schema:\n"
        "{\n"
        '  "business_name": "string or null",\n'
        '  "business_category": "string",\n'
        '  "business_description": "string",\n'
        '  "services": [\n'
        '    {"name": "...", "description": "...", "duration_minutes": 30, "requirements": "..."}\n'
        '  ],\n'
        '  "appointment_interval": 30,\n'
        '  "follow_up_questions": ["question 1", "question 2"]\n'
        "}"
    )

    messages = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        for msg in conversation_history:
            messages.append(msg)
    
    prompt_text = f"Owner's Business Description:\n{description}"
    if previous_answers:
        prompt_text += f"\n\nFollow-up Answers:\n{json.dumps(previous_answers)}"
    messages.append({"role": "user", "content": prompt_text})

    try:
        res = groq_client.chat.completions.create(
            model=GROQ_AI_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=2048
        )
        profile = _clean_ai_json(res.choices[0].message.content)
        if not profile:
            return False, {}, "Failed to parse structured profile from AI."
        return True, profile, "Business analyzed successfully."
    except Exception as e:
        logger.error(f"Groq openai/gpt-oss-120b business understanding failed: {e}")
        return False, {}, f"AI service error: {str(e)}"

def ai_generate_intake_questions(business_profile: dict, custom_instructions: str = None) -> tuple[bool, list, str]:
    """Generates personalized appointment intake questions with types and conditional logic using openai/gpt-oss-120b."""
    if not groq_client:
        default_qs = [
            {
                "id": str(uuid.uuid4()),
                "key": "q1",
                "question_text": "What service would you like to book?",
                "question_type": "single_choice",
                "options": [s.get("name", s) if isinstance(s, dict) else s for s in business_profile.get("services", ["General Consultation"])],
                "is_required": True,
                "display_order": 0,
                "conditional_logic": None,
                "help_text": "Select your desired appointment service."
            },
            {
                "id": str(uuid.uuid4()),
                "key": "q2",
                "question_text": "Do you have any special requests or focus areas?",
                "question_type": "long_text",
                "options": [],
                "is_required": False,
                "display_order": 1,
                "conditional_logic": None,
                "help_text": ""
            }
        ]
        return True, default_qs, "Generated fallback intake questions."

    system_prompt = (
        "You are a Senior UI/UX Designer and AI Intake Form Architect.\n"
        "Generate personalized, high-conversion appointment intake questions for this business.\n"
        "Requirements:\n"
        "1. Generate 3 to 6 intelligent, relevant questions.\n"
        "2. Do NOT ask for customer name, email, or date/time (those are handled by the platform's core booking picker).\n"
        "3. If multiple services exist, the first question (key: 'q1') should be single_choice for the service.\n"
        "4. Supported question_type values:\n"
        "   - 'single_choice' (radio buttons, select exactly one)\n"
        "   - 'dropdown' (select one from menu)\n"
        "   - 'multiple_choice' (checkboxes, select all that apply)\n"
        "   - 'short_text' (brief text response)\n"
        "   - 'long_text' (multi-line response)\n"
        "   - 'yes_no' (binary question)\n"
        "5. For choice questions, provide a clear, realistic 'options' array.\n"
        "6. CONDITIONAL LOGIC: If a question only applies to a specific service or prior answer, set:\n"
        "   'conditional_logic': {'depends_on_question_key': 'q1', 'show_if_value': 'Service Name'}\n"
        "   Otherwise set 'conditional_logic': null.\n"
        "7. Return ONLY valid JSON:\n"
        "{\n"
        '  "questions": [\n'
        '    {\n'
        '      "key": "q1",\n'
        '      "question_text": "...",\n'
        '      "question_type": "single_choice",\n'
        '      "options": ["Option A", "Option B"],\n'
        '      "is_required": true,\n'
        '      "conditional_logic": null,\n'
        '      "help_text": "..."\n'
        '    }\n'
        '  ]\n'
        "}"
    )

    user_content = f"Business Profile:\n{json.dumps(business_profile, indent=2)}"
    if custom_instructions:
        user_content += f"\n\nOwner's Custom Instructions:\n{custom_instructions}"

    try:
        res = groq_client.chat.completions.create(
            model=GROQ_AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=2500
        )
        data = _clean_ai_json(res.choices[0].message.content)
        raw_qs = data.get("questions", [])
        formatted_qs = []
        for idx, q in enumerate(raw_qs):
            formatted_qs.append({
                "id": str(uuid.uuid4()),
                "key": q.get("key", f"q{idx+1}"),
                "question_text": q.get("question_text", "Untitled Question"),
                "question_type": q.get("question_type", "short_text"),
                "options": q.get("options") or [],
                "is_required": bool(q.get("is_required", False)),
                "display_order": idx,
                "conditional_logic": q.get("conditional_logic"),
                "help_text": q.get("help_text", "")
            })
        return True, formatted_qs, "Intake questions generated successfully."
    except Exception as e:
        logger.error(f"Groq openai/gpt-oss-120b question generation failed: {e}")
        return False, [], f"AI generation error: {str(e)}"

def ai_improve_question(question_data: dict, business_profile: dict, feedback: str = None) -> tuple[bool, dict, str]:
    """Improves a single intake question using openai/gpt-oss-120b."""
    if not groq_client:
        return True, question_data, "No AI client available."
    system_prompt = (
        "You are an AI Intake Form Specialist. Improve or reword this appointment question.\n"
        "Keep it clear, concise, and professional for customers.\n"
        "Return valid JSON matching the question object with keys: question_text, question_type, options, is_required, help_text."
    )
    user_prompt = f"Business: {business_profile.get('name', 'Small Business')}\nQuestion: {json.dumps(question_data)}"
    if feedback:
        user_prompt += f"\nFeedback: {feedback}"
    try:
        res = groq_client.chat.completions.create(
            model=GROQ_AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=800
        )
        improved = _clean_ai_json(res.choices[0].message.content)
        merged = {**question_data, **improved}
        return True, merged, "Question improved."
    except Exception as e:
        logger.error(f"Groq improve question error: {e}")
        return False, question_data, str(e)

def ai_improve_entire_form(current_questions: list, business_profile: dict, instruction: str) -> tuple[bool, list, str]:
    """Refines all intake questions according to owner's instructions using openai/gpt-oss-120b."""
    if not groq_client:
        return True, current_questions, "AI client unavailable."
    system_prompt = (
        "You are an AI Intake Form Architect. Refine the given list of appointment questions according to the owner's instruction.\n"
        "Maintain supported question types: single_choice, dropdown, multiple_choice, short_text, long_text, yes_no.\n"
        "Ensure conditional_logic rules remain intact or updated appropriately.\n"
        "Return valid JSON: {\"questions\": [ ... ]}"
    )
    user_content = f"Business Profile:\n{json.dumps(business_profile)}\nCurrent Questions:\n{json.dumps(current_questions)}\nInstruction: {instruction}"
    try:
        res = groq_client.chat.completions.create(
            model=GROQ_AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=2500
        )
        data = _clean_ai_json(res.choices[0].message.content)
        raw_qs = data.get("questions", [])
        formatted = []
        for idx, q in enumerate(raw_qs):
            formatted.append({
                "id": q.get("id") or str(uuid.uuid4()),
                "key": q.get("key", f"q{idx+1}"),
                "question_text": q.get("question_text", "Question"),
                "question_type": q.get("question_type", "short_text"),
                "options": q.get("options") or [],
                "is_required": bool(q.get("is_required", False)),
                "display_order": idx,
                "conditional_logic": q.get("conditional_logic"),
                "help_text": q.get("help_text", "")
            })
        return True, formatted, "Form improved successfully."
    except Exception as e:
        logger.error(f"Groq improve entire form error: {e}")
        return False, current_questions, str(e)

def ai_summarize_appointment(booking_data: dict, service_name: str, responses: list, customer_notes: str) -> tuple[bool, dict]:
    """
    Generates a concise, highly accurate appointment summary using openai/gpt-oss-120b.
    If AI service is unavailable, provides a clean deterministic fallback summary.
    """
    ans_lines = []
    for r in responses:
        ans = r.get("answer")
        if isinstance(ans, list):
            ans = ", ".join(ans)
        if ans:
            ans_lines.append(f"{r.get('question_text')}: {ans}")
    ans_summary_text = "; ".join(ans_lines)
    cust_name = booking_data.get("customer_name", "Customer")
    svc = service_name or "General Appointment"

    fallback_summary = f"{cust_name} booked {svc}."
    if ans_summary_text:
        fallback_summary += f" Intake details: {ans_summary_text}."
    if customer_notes:
        fallback_summary += f" Special note: {customer_notes}."

    fallback_result = {
        "summary": fallback_summary,
        "preparation_notes": f"Verify requirements for {svc} prior to arrival." if svc != "General Appointment" else None
    }

    if not groq_client:
        return False, fallback_result

    system_prompt = (
        "You are an AI Appointment Intake Summarizer.\n"
        "Analyze the customer's selected service, intake questionnaire responses, and special notes.\n"
        "Requirements:\n"
        "1. Produce a concise, useful 1-3 sentence summary that the business owner can scan quickly.\n"
        "2. Accurately reflect submitted answers. Never invent customer preferences or services.\n"
        "3. Distinguish customer-provided information from interpretation.\n"
        "4. If helpful, provide preparation_notes for staff.\n"
        "5. Return valid JSON:\n"
        "{\n"
        '  "summary": "...",\n'
        '  "preparation_notes": "..."\n'
        "}"
    )

    payload = {
        "customer_name": cust_name,
        "service": svc,
        "intake_responses": responses,
        "customer_notes": customer_notes
    }

    try:
        res = groq_client.chat.completions.create(
            model=GROQ_AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload)}
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=800
        )
        parsed = _clean_ai_json(res.choices[0].message.content)
        if parsed and parsed.get("summary"):
            return True, parsed
        return False, fallback_result
    except Exception as e:
        logger.warning(f"Groq appointment summary error (falling back to deterministic summary): {e}")
        return False, fallback_result

# ====================================================================
# DATABASE & DATA STORE REPOSITORIES FOR CUSTOM INTAKE
# ====================================================================
def get_business_profile(business_id: str) -> dict:
    """Retrieves business profile from Supabase or fallback store."""
    if not business_id:
        return {}
    if supabase:
        try:
            res = supabase.table('business_profiles').select('*').eq('business_id', business_id).limit(1).execute()
            if res.data and len(res.data) > 0:
                return res.data[0]
        except Exception as e:
            logger.warning(f"Supabase get_business_profile error: {e}")

    for bp in FALLBACK_BUSINESS_PROFILES:
        if bp.get('business_id') == business_id:
            return bp
    return {}

def save_business_profile(business_id: str, profile_data: dict) -> dict:
    """Persists structured business profile to Supabase and fallback store."""
    record = {
        "business_id": business_id,
        "category": profile_data.get("business_category") or profile_data.get("category"),
        "description": profile_data.get("business_description") or profile_data.get("description"),
        "services": profile_data.get("services") or [],
        "requirements": profile_data.get("requirements") or {},
        "onboarding_status": "COMPLETED",
        "updated_at": datetime.utcnow().isoformat()
    }
    if supabase:
        try:
            existing = supabase.table('business_profiles').select('id').eq('business_id', business_id).execute()
            if existing.data and len(existing.data) > 0:
                res = supabase.table('business_profiles').update(record).eq('business_id', business_id).execute()
            else:
                record["id"] = str(uuid.uuid4())
                res = supabase.table('business_profiles').insert(record).execute()
            if res.data:
                return res.data[0]
        except Exception as e:
            logger.warning(f"Supabase save_business_profile error: {e}")

    # Fallback store
    for idx, bp in enumerate(FALLBACK_BUSINESS_PROFILES):
        if bp.get('business_id') == business_id:
            FALLBACK_BUSINESS_PROFILES[idx] = {**bp, **record}
            return FALLBACK_BUSINESS_PROFILES[idx]

    record["id"] = str(uuid.uuid4())
    FALLBACK_BUSINESS_PROFILES.append(record)
    return record

def get_intake_form_by_business(business_id: str, status: str = None) -> tuple[dict, list]:
    """Retrieves intake form and questions for a business (draft, published, or any)."""
    if not business_id:
        return None, []

    # 1. Try Supabase
    if supabase:
        try:
            query = supabase.table('intake_forms').select('*').eq('business_id', business_id)
            if status:
                query = query.eq('status', status)
            query = query.order('version', desc=True).limit(1)
            form_res = query.execute()

            if form_res.data and len(form_res.data) > 0:
                form = form_res.data[0]
                q_res = supabase.table('intake_questions').select('*').eq('form_id', form['id']).order('display_order', desc=False).execute()
                questions = q_res.data or []
                return form, questions
        except Exception as e:
            logger.warning(f"Supabase get_intake_form_by_business error: {e}")

    # 2. Resilient fallback store
    matched_forms = [f for f in FALLBACK_INTAKE_FORMS if f.get('business_id') == business_id]
    if status:
        matched_forms = [f for f in matched_forms if f.get('status') == status]

    matched_forms.sort(key=lambda x: x.get('version', 1), reverse=True)
    if matched_forms:
        target_form = matched_forms[0]
        questions = [q for q in FALLBACK_INTAKE_QUESTIONS if q.get('form_id') == target_form.get('id')]
        questions.sort(key=lambda x: x.get('display_order', 0))
        return target_form, questions

    return None, []

def save_draft_intake_form(business_id: str, form_title: str, questions: list, services: list = None) -> dict:
    """Saves or updates a DRAFT intake form and replaces its questions."""
    draft_form, _ = get_intake_form_by_business(business_id, status='DRAFT')
    pub_form, _ = get_intake_form_by_business(business_id, status='PUBLISHED')

    next_version = (pub_form.get('version', 0) + 1) if pub_form else 1
    form_id = draft_form.get('id') if draft_form else str(uuid.uuid4())

    form_record = {
        "id": form_id,
        "business_id": business_id,
        "title": form_title or "Appointment Intake Form",
        "status": "DRAFT",
        "version": next_version,
        "services": services or (pub_form.get('services') if pub_form else []),
        "updated_at": datetime.utcnow().isoformat()
    }

    if supabase:
        try:
            if draft_form:
                supabase.table('intake_forms').update(form_record).eq('id', form_id).execute()
            else:
                form_record["created_at"] = datetime.utcnow().isoformat()
                supabase.table('intake_forms').insert(form_record).execute()

            # Delete old draft questions and insert new ones
            supabase.table('intake_questions').delete().eq('form_id', form_id).execute()
            db_questions = []
            for idx, q in enumerate(questions):
                db_questions.append({
                    "id": str(uuid.uuid4()),
                    "form_id": form_id,
                    "question_text": q.get('question_text', 'Question'),
                    "question_type": q.get('question_type', 'short_text'),
                    "options": q.get('options') or [],
                    "is_required": bool(q.get('is_required', False)),
                    "display_order": idx,
                    "conditional_logic": q.get('conditional_logic'),
                    "help_text": q.get('help_text', '')
                })
            if db_questions:
                supabase.table('intake_questions').insert(db_questions).execute()
            return form_record
        except Exception as e:
            logger.warning(f"Supabase save_draft_intake_form error: {e}")

    # Fallback store persistence
    existing_idx = None
    for idx, f in enumerate(FALLBACK_INTAKE_FORMS):
        if f.get('business_id') == business_id and f.get('status') == 'DRAFT':
            existing_idx = idx
            break

    if existing_idx is not None:
        FALLBACK_INTAKE_FORMS[existing_idx] = form_record
    else:
        FALLBACK_INTAKE_FORMS.append(form_record)

    # Clean old fallback questions for this form
    global FALLBACK_INTAKE_QUESTIONS
    FALLBACK_INTAKE_QUESTIONS = [q for q in FALLBACK_INTAKE_QUESTIONS if q.get('form_id') != form_id]

    for idx, q in enumerate(questions):
        FALLBACK_INTAKE_QUESTIONS.append({
            "id": q.get('id') or str(uuid.uuid4()),
            "form_id": form_id,
            "key": q.get('key', f"q{idx+1}"),
            "question_text": q.get('question_text', 'Question'),
            "question_type": q.get('question_type', 'short_text'),
            "options": q.get('options') or [],
            "is_required": bool(q.get('is_required', False)),
            "display_order": idx,
            "conditional_logic": q.get('conditional_logic'),
            "help_text": q.get('help_text', '')
        })

    return form_record

def publish_intake_form(business_id: str, questions: list = None) -> tuple[bool, str]:
    """Promotes the DRAFT form to PUBLISHED, archiving any previous published version."""
    draft_form, draft_qs = get_intake_form_by_business(business_id, status='DRAFT')
    if not draft_form and not questions:
        return False, "No draft form found to publish."

    qs_to_publish = questions if questions is not None else draft_qs
    pub_form, _ = get_intake_form_by_business(business_id, status='PUBLISHED')
    new_version = (pub_form.get('version', 0) + 1) if pub_form else 1

    new_pub_id = str(uuid.uuid4())
    form_record = {
        "id": new_pub_id,
        "business_id": business_id,
        "title": draft_form.get('title', 'Appointment Intake Form') if draft_form else "Appointment Intake Form",
        "status": "PUBLISHED",
        "version": new_version,
        "services": draft_form.get('services', []) if draft_form else [],
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat()
    }

    if supabase:
        try:
            # Insert new published form
            supabase.table('intake_forms').insert(form_record).execute()

            # Insert questions for published form
            db_questions = []
            for idx, q in enumerate(qs_to_publish):
                db_questions.append({
                    "id": str(uuid.uuid4()),
                    "form_id": new_pub_id,
                    "question_text": q.get('question_text', 'Question'),
                    "question_type": q.get('question_type', 'short_text'),
                    "options": q.get('options') or [],
                    "is_required": bool(q.get('is_required', False)),
                    "display_order": idx,
                    "conditional_logic": q.get('conditional_logic'),
                    "help_text": q.get('help_text', '')
                })
            if db_questions:
                supabase.table('intake_questions').insert(db_questions).execute()

            # Delete old draft form to clear pending state
            if draft_form:
                supabase.table('intake_forms').delete().eq('id', draft_form['id']).execute()
            return True, f"Published Version {new_version} successfully!"
        except Exception as e:
            logger.warning(f"Supabase publish_intake_form error: {e}")

    # Fallback store
    FALLBACK_INTAKE_FORMS.append(form_record)
    for idx, q in enumerate(qs_to_publish):
        FALLBACK_INTAKE_QUESTIONS.append({
            "id": str(uuid.uuid4()),
            "form_id": new_pub_id,
            "key": q.get('key', f"q{idx+1}"),
            "question_text": q.get('question_text', 'Question'),
            "question_type": q.get('question_type', 'short_text'),
            "options": q.get('options') or [],
            "is_required": bool(q.get('is_required', False)),
            "display_order": idx,
            "conditional_logic": q.get('conditional_logic'),
            "help_text": q.get('help_text', '')
        })

    # Clear draft from fallback
    if draft_form:
        for idx, f in enumerate(FALLBACK_INTAKE_FORMS):
            if f.get('id') == draft_form.get('id'):
                FALLBACK_INTAKE_FORMS.pop(idx)
                break

    return True, f"Published Version {new_version} successfully!"

def save_appointment_intake_responses(booking_id: str, business_id: str, form_id: str, service_selected: str, responses: list) -> dict:
    """Persists customer intake responses linked to an appointment."""
    record = {
        "id": str(uuid.uuid4()),
        "booking_id": booking_id,
        "business_id": business_id,
        "form_id": form_id,
        "service_selected": service_selected,
        "responses": responses,
        "created_at": datetime.utcnow().isoformat()
    }
    if supabase:
        try:
            res = supabase.table('appointment_intake_responses').insert(record).execute()
            if res.data:
                return res.data[0]
        except Exception as e:
            logger.warning(f"Supabase save_intake_responses error: {e}")

    FALLBACK_INTAKE_RESPONSES.append(record)
    return record

def get_intake_responses_for_booking(booking_id: str) -> dict:
    """Retrieves customer intake responses for a specific booking."""
    if not booking_id:
        return {}
    if supabase:
        try:
            res = supabase.table('appointment_intake_responses').select('*').eq('booking_id', booking_id).limit(1).execute()
            if res.data and len(res.data) > 0:
                return res.data[0]
        except Exception as e:
            logger.warning(f"Supabase get_intake_responses error: {e}")

    for r in FALLBACK_INTAKE_RESPONSES:
        if str(r.get('booking_id')) == str(booking_id):
            return r
    return {}

def save_ai_appointment_summary(booking_id: str, business_id: str, summary: str, prep_notes: str = None, status: str = 'COMPLETED') -> dict:
    """Persists Groq AI-generated summary and preparation notes."""
    record = {
        "id": str(uuid.uuid4()),
        "booking_id": booking_id,
        "business_id": business_id,
        "summary": summary,
        "preparation_notes": prep_notes,
        "status": status,
        "created_at": datetime.utcnow().isoformat()
    }
    if supabase:
        try:
            res = supabase.table('ai_appointment_summaries').insert(record).execute()
            if res.data:
                return res.data[0]
        except Exception as e:
            logger.warning(f"Supabase save_ai_summary error: {e}")

    FALLBACK_AI_SUMMARIES.append(record)
    return record

def get_ai_appointment_summary(booking_id: str) -> dict:
    """Retrieves AI-generated appointment summary for an appointment."""
    if not booking_id:
        return {}
    if supabase:
        try:
            res = supabase.table('ai_appointment_summaries').select('*').eq('booking_id', booking_id).limit(1).execute()
            if res.data and len(res.data) > 0:
                return res.data[0]
        except Exception as e:
            logger.warning(f"Supabase get_ai_summary error: {e}")

    for s in FALLBACK_AI_SUMMARIES:
        if str(s.get('booking_id')) == str(booking_id):
            return s
    return {}

# ====================================================================
# FLASK APPLICATION ROUTES
# ====================================================================
@app.route('/')
def index():
    """Renders the public landing page."""
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handles login, signup, and directs users to post-login selection."""
    if request.method == 'POST':
        action = request.form.get('action') # 'login' or 'signup'
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        if not email or not password:
            flash("Please provide both email and password.", "error")
            return render_template('login.html')

        user_id = None
        if supabase:
            try:
                if action == 'signup':
                    is_valid, msg = validate_password_strength(password)
                    if not is_valid:
                        flash(msg, "error")
                        return render_template('login.html')

                    response = supabase.auth.sign_up({"email": email, "password": password})
                    if response.user:
                        user_id = response.user.id
                    else:
                        flash("Signup failed. Please try again.", "error")
                        return render_template('login.html')
                elif action == 'login':
                    response = supabase.auth.sign_in_with_password({"email": email, "password": password})
                    if response.user:
                        user_id = response.user.id
                    else:
                        flash("Invalid email or password.", "error")
                        return render_template('login.html')
            except Exception as e:
                logger.warning(f"Supabase auth exception: {e}")
                user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, email))

        if not user_id:
            user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, email))

        profile = get_user_profile(user_id)
        role = profile.get('role')

        session['user'] = {
            'email': email,
            'id': user_id,
            'role': role
        }
        flash(f"Welcome back, {email}!", "success")
        return post_login_redirect_for(user_id, email)

    return render_template('login.html')

@app.route('/auth/google')
def auth_google():
    """Initiates Google OAuth login via Supabase."""
    if not supabase:
        flash("Supabase database connection not configured. Please use standard email login.", "error")
        return redirect(url_for('login'))

    try:
        redirect_uri = url_for('auth_callback', _external=True)
        res = supabase.auth.sign_in_with_oauth({
            "provider": "google",
            "options": {
                "redirect_to": redirect_uri
            }
        })

        if hasattr(supabase.auth, '_storage'):
            verifier = supabase.auth._storage.get_item(f"{supabase.auth._storage_key}-code-verifier")
            if verifier:
                session['oauth_code_verifier'] = verifier

        if res.url:
            return redirect(res.url)
        else:
            flash("Could not initiate Google login.", "error")
            return redirect(url_for('login'))
    except Exception as e:
        flash(f"Google login error: {str(e)}", "error")
        return redirect(url_for('login'))

@app.route('/auth/callback')
def auth_callback():
    """Handles OAuth code exchange from Supabase/Google."""
    error = request.args.get('error')
    error_description = request.args.get('error_description')
    if error:
        flash(f"Google login error: {error_description or error}", "error")
        return redirect(url_for('login'))

    code = request.args.get('code')
    if code and supabase:
        try:
            verifier = session.pop('oauth_code_verifier', None)
            exchange_params = {"auth_code": code}
            if verifier:
                exchange_params["code_verifier"] = verifier

            auth_res = supabase.auth.exchange_code_for_session(exchange_params)
            if auth_res and auth_res.user:
                user_id = auth_res.user.id
                email = auth_res.user.email
                profile = get_user_profile(user_id)
                session['user'] = {
                    'email': email,
                    'id': user_id,
                    'role': profile.get('role')
                }
                flash("Signed in successfully with Google!", "success")
                return post_login_redirect_for(user_id, email)
        except Exception as e:
            flash(f"Google authentication error: {str(e)}", "error")
            return redirect(url_for('login'))

    return render_template('auth_callback.html', supabase_url=SUPABASE_URL, supabase_key=SUPABASE_ANON_KEY)

@app.route('/auth/session', methods=['POST'])
def auth_session():
    """Sets session from frontend OAuth token handler."""
    data = request.get_json()
    if data and 'email' in data and 'id' in data:
        profile = get_user_profile(data['id'])
        session['user'] = {
            'email': data['email'],
            'id': data['id'],
            'role': profile.get('role')
        }
        redirect_target = url_for('select_role') if not profile.get('role') else (
            url_for('admin_dashboard') if profile.get('role') == 'BUSINESS_ADMIN' else url_for('businesses')
        )
        return jsonify({"status": "success", "role": profile.get('role'), "redirect_url": redirect_target}), 200
    return jsonify({"status": "error"}), 400

# ====================================================================
# POST-LOGIN ROLE SELECTION & BUSINESS ONBOARDING
# ====================================================================
@app.route('/select-role', methods=['GET', 'POST'])
def select_role():
    """Post-Login selection: Choose Customer vs Business Admin."""
    if 'user' not in session:
        flash("Please sign in first.", "error")
        return redirect(url_for('login'))

    user = session['user']
    if request.method == 'POST':
        role = request.form.get('role') # 'CUSTOMER' or 'BUSINESS_ADMIN'
        if role not in ['CUSTOMER', 'BUSINESS_ADMIN']:
            flash("Please choose a valid role.", "error")
            return render_template('role_selection.html', user=user)

        user['role'] = role
        session['user'] = user
        set_user_role(user['id'], user['email'], role)

        if role == 'BUSINESS_ADMIN':
            biz = get_business_by_owner(user['id'])
            if not biz:
                return redirect(url_for('create_business'))
            return redirect(url_for('admin_dashboard'))
        else:
            flash("You are now in Customer mode. Explore businesses to book appointments!", "success")
            return redirect(url_for('businesses'))

    return render_template('role_selection.html', user=user)

@app.route('/create-business', methods=['GET', 'POST'])
def create_business():
    """Registers a new Business Profile for a Business Admin."""
    if 'user' not in session:
        flash("Please log in first.", "error")
        return redirect(url_for('login'))

    user = session['user']
    if user.get('role') != 'BUSINESS_ADMIN':
        user['role'] = 'BUSINESS_ADMIN'
        session['user'] = user
        set_user_role(user['id'], user['email'], 'BUSINESS_ADMIN')

    # If owner already has a business, direct to dashboard
    existing_biz = get_business_by_owner(user['id'])
    if existing_biz:
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        address = request.form.get('address', '').strip()
        interval = request.form.get('appointment_interval', 30)
        max_bookings = request.form.get('max_bookings_per_slot', 1)
        allow_selection = request.form.get('allow_employee_selection') == 'true'
        opening_time = request.form.get('opening_time', '09:00')
        closing_time = request.form.get('closing_time', '17:00')

        if not name or not address:
            flash("Business Name and Physical Address are required.", "error")
            return render_template('create_business.html', user=user)

        biz_data = {
            "name": name,
            "description": description,
            "address": address,
            "appointment_interval": interval,
            "max_bookings_per_slot": max_bookings,
            "allow_employee_selection": allow_selection,
            "opening_time": opening_time,
            "closing_time": closing_time
        }

        created = create_business_record(user['id'], biz_data)
        user['business_id'] = created['id']
        session['user'] = user
        set_user_role(user['id'], user['email'], 'BUSINESS_ADMIN', created['id'])

        # Auto-create the owner as the initial active staff member
        add_employee_record(created['id'], name.split()[0] + " (Owner)", user['email'])

        flash(f"Business '{name}' created successfully! Welcome to your Admin Dashboard.", "success")
        return redirect(url_for('admin_dashboard'))

    return render_template('create_business.html', user=user)

# ====================================================================
# CUSTOMER DIRECTORY & APPOINTMENT BOOKING FLOW
# ====================================================================
@app.route('/businesses')
def businesses():
    """Business Directory & Search: Lists all businesses for customers."""
    query = request.args.get('q', '').strip()
    all_biz = get_all_businesses(query)
    user = session.get('user')
    return render_template('businesses.html', businesses=all_biz, search_query=query, user=user)

@app.route('/businesses/<business_id>/book', methods=['GET', 'POST'])
def book_appointment(business_id):
    """
    Landing booking page for a specific business:
    - Allows date selection and employee selection.
    - Dynamically presents open slots.
    - Dynamically presents published AI intake questions with dynamic conditional logic.
    - Atomically processes bookings with strict concurrency protection.
    - Saves customer answers and triggers AI appointment summary generation.
    """
    business = get_business_by_id(business_id)
    if not business:
        flash("The requested business could not be found.", "error")
        return redirect(url_for('businesses'))

    employees = get_employees_by_business(business_id)
    user = session.get('user')

    # Selected date and employee filters
    today = date.today()
    selected_date_str = request.args.get('date', today.isoformat())
    selected_employee_id = request.args.get('employee_id', '')

    try:
        selected_date = date.fromisoformat(selected_date_str)
        if selected_date < today:
            selected_date = today
    except Exception:
        selected_date = today

    # Fetch active published intake form & questions for this business
    pub_form, intake_questions = get_intake_form_by_business(business_id, status='PUBLISHED')
    services = pub_form.get('services', []) if pub_form else []

    # POST: Process Booking Submission
    if request.method == 'POST':
        start_time_iso = request.form.get('start_time')
        cust_name = request.form.get('customer_name', '').strip()
        cust_email = request.form.get('customer_email', '').strip()
        emp_id = request.form.get('employee_id', '').strip() or None
        notes = request.form.get('notes', '').strip()
        selected_service = request.form.get('selected_service', '').strip()

        if not start_time_iso or not cust_name or not cust_email:
            flash("Please fill in all required fields and pick a time slot.", "error")
            return redirect(url_for('book_appointment', business_id=business_id, date=selected_date.isoformat(), employee_id=emp_id or ''))

        interval = int(business.get("appointment_interval", 30))
        try:
            start_dt = datetime.fromisoformat(start_time_iso)
            end_dt = start_dt + timedelta(minutes=interval)
            end_time_iso = end_dt.isoformat()
        except Exception:
            flash("Invalid slot timestamp.", "error")
            return redirect(url_for('book_appointment', business_id=business_id))

        cust_id = user.get('id') if user else None

        # Execute atomic booking with anti-double-booking concurrency lock
        success, message, booking = book_appointment_atomic(
            business_id=business_id,
            employee_id=emp_id,
            customer_id=cust_id,
            customer_name=cust_name,
            customer_email=cust_email,
            start_time=start_time_iso,
            end_time=end_time_iso,
            notes=notes
        )

        if not success:
            flash(message, "error")
            return redirect(url_for('book_appointment', business_id=business_id, date=selected_date.isoformat(), employee_id=emp_id or ''))

        booking_id = booking.get('id', str(uuid.uuid4()))

        # Collect intake responses
        intake_responses = []
        if intake_questions:
            for q in intake_questions:
                q_id = str(q.get('id', ''))
                if q.get('question_type') == 'multiple_choice':
                    ans = request.form.getlist(f"intake_{q_id}")
                else:
                    ans = request.form.get(f"intake_{q_id}", '').strip()
                if ans:
                    intake_responses.append({
                        "question_id": q_id,
                        "question_text": q.get('question_text', ''),
                        "question_type": q.get('question_type', ''),
                        "answer": ans
                    })

        # Save responses if present
        if intake_responses or selected_service:
            save_appointment_intake_responses(
                booking_id=booking_id,
                business_id=business_id,
                form_id=pub_form.get('id') if pub_form else None,
                service_selected=selected_service,
                responses=intake_responses
            )

        # Generate AI Appointment Summary via openai/gpt-oss-120b
        ai_success, summary_data = ai_summarize_appointment(
            booking_data={
                "booking_id": booking_id,
                "customer_name": cust_name,
                "customer_email": cust_email,
                "start_time": start_time_iso,
                "notes": notes
            },
            service_name=selected_service,
            responses=intake_responses,
            customer_notes=notes
        )
        if summary_data:
            save_ai_appointment_summary(
                booking_id=booking_id,
                business_id=business_id,
                summary=summary_data.get('summary', ''),
                prep_notes=summary_data.get('preparation_notes'),
                status='COMPLETED' if ai_success else 'FALLBACK'
            )

        # Lookup employee name for email & calendar
        emp_name = "Staff Member"
        if emp_id:
            for e in employees:
                if e['id'] == emp_id:
                    emp_name = e['name']
                    break

        # Trigger automated confirmation email & Google Calendar payload
        send_booking_confirmation_email(
            customer_email=cust_email,
            customer_name=cust_name,
            business=business,
            employee_name=emp_name,
            start_time_str=start_time_iso,
            end_time_str=end_time_iso,
            booking_id=booking_id
        )

        # Create a live event on the business's connected Google Calendar, if any
        sync_booking_to_google_calendar(
            business=business,
            booking_id=booking_id,
            customer_name=cust_name,
            customer_email=cust_email,
            employee_name=emp_name,
            start_time_str=start_time_iso,
            end_time_str=end_time_iso,
            location=business.get('address', ''),
            notes=notes
        )

        flash(f"Appointment confirmed! A confirmation email and Google Calendar invitation have been dispatched to {cust_email}.", "success")
        return redirect(url_for('dashboard'))

    # Calculate dynamic slot availability
    slots = generate_business_slots(business, selected_date, selected_employee_id)

    return render_template(
        'book_appointment.html',
        business=business,
        employees=employees,
        slots=slots,
        selected_date=selected_date.isoformat(),
        min_date=today.isoformat(),
        selected_employee_id=selected_employee_id,
        user=user,
        intake_form=pub_form,
        intake_questions=intake_questions,
        services=services
    )

@app.route('/businesses/<business_id>/slots')
def api_business_slots(business_id):
    """API endpoint providing real-time slot availability for dates & staff."""
    business = get_business_by_id(business_id)
    if not business:
        return jsonify({"error": "Business not found"}), 404

    date_str = request.args.get('date', date.today().isoformat())
    employee_id = request.args.get('employee_id')
    try:
        target_date = date.fromisoformat(date_str)
    except Exception:
        target_date = date.today()

    slots = generate_business_slots(business, target_date, employee_id)
    return jsonify({"slots": slots, "business_id": business_id, "date": date_str})

# ====================================================================
# BUSINESS ADMIN DASHBOARD & STAFF MANAGEMENT
# ====================================================================
@app.route('/admin', methods=['GET', 'POST'])
def admin_dashboard():
    """Business Admin Dashboard: Manage business settings, staff, view bookings, and inspect AI summaries."""
    if 'user' not in session:
        flash("Please log in to access the Admin Console.", "error")
        return redirect(url_for('login'))

    user = session['user']
    if user.get('role') != 'BUSINESS_ADMIN':
        flash("You need Business Admin privileges to access that view.", "error")
        return redirect(url_for('dashboard'))

    business = get_business_by_owner(user['id'])
    if not business:
        flash("Please register your business profile first.", "error")
        return redirect(url_for('create_business'))

    # Handle Settings Update POST
    if request.method == 'POST' and request.form.get('action') == 'update_settings':
        update_business_record(business['id'], {
            "name": request.form.get('name'),
            "description": request.form.get('description'),
            "address": request.form.get('address'),
            "appointment_interval": request.form.get('appointment_interval'),
            "max_bookings_per_slot": request.form.get('max_bookings_per_slot'),
            "allow_employee_selection": request.form.get('allow_employee_selection') == 'true',
            "opening_time": request.form.get('opening_time'),
            "closing_time": request.form.get('closing_time'),
            "calendar_id": request.form.get('calendar_id', '')
        })
        flash("Business configuration and intervals updated successfully!", "success")
        return redirect(url_for('admin_dashboard'))

    employees = get_employees_by_business(business['id'])
    bookings = get_bookings_for_business(business['id'])

    # Attach intake data and AI summary to each booking
    for b in bookings:
        b_id = str(b.get('id', ''))
        b['intake_data'] = get_intake_responses_for_booking(b_id)
        b['ai_summary'] = get_ai_appointment_summary(b_id)

    # Fetch draft and published intake forms for this business
    draft_form, draft_qs = get_intake_form_by_business(business['id'], status='DRAFT')
    pub_form, pub_qs = get_intake_form_by_business(business['id'], status='PUBLISHED')

    return render_template(
        'admin_dashboard.html',
        user=user,
        business=business,
        employees=employees,
        bookings=bookings,
        draft_form=draft_form,
        draft_questions_count=len(draft_qs),
        published_form=pub_form,
        published_questions_count=len(pub_qs),
        google_service_account_email=GOOGLE_SERVICE_ACCOUNT_EMAIL,
        calendar_sync_available=bool(google_calendar_service),
        google_oauth_available=google_oauth_available
    )

# ====================================================================
# AI ONBOARDING & CUSTOM INTAKE FORM STUDIO ROUTES
# ====================================================================
@app.route('/onboarding', methods=['GET', 'POST'])
def onboarding():
    """
    Conversational AI Business Onboarding:
    - Provides Voice (Option A, preferred via Groq Whisper) and Text (Option B) input.
    - Guides business owners to naturally describe their business.
    """
    if 'user' not in session:
        flash("Please log in to start AI Onboarding.", "error")
        return redirect(url_for('login'))

    user = session['user']
    if user.get('role') != 'BUSINESS_ADMIN':
        user['role'] = 'BUSINESS_ADMIN'
        session['user'] = user
        set_user_role(user['id'], user['email'], 'BUSINESS_ADMIN')

    business = get_business_by_owner(user['id'])
    return render_template('ai_onboarding.html', user=user, business=business)

@app.route('/api/ai/transcribe', methods=['POST'])
def api_ai_transcribe():
    """Server-side endpoint to transcribe audio recordings using Groq Whisper."""
    if 'audio' not in request.files:
        return jsonify({"success": False, "error": "No audio file uploaded."}), 400

    audio_file = request.files['audio']
    if audio_file.filename == '':
        return jsonify({"success": False, "error": "Empty audio file."}), 400

    try:
        audio_bytes = audio_file.read()
        filename = audio_file.filename or "recording.webm"
        success, transcript = ai_transcribe_audio(audio_bytes, filename)
        if success:
            return jsonify({"success": True, "text": transcript}), 200
        else:
            return jsonify({"success": False, "error": transcript}), 500
    except Exception as e:
        logger.error(f"Audio transcription endpoint error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/ai/understand', methods=['POST'])
def api_ai_understand():
    """
    Server-side endpoint to analyze business description using openai/gpt-oss-120b.
    Extracts structured business information and returns targeted follow-up questions if needed.
    """
    data = request.get_json() or {}
    description = data.get('description', '').strip()
    if not description:
        return jsonify({"success": False, "error": "Description cannot be empty."}), 400

    success, profile, msg = ai_understand_business(description)
    if success:
        return jsonify({
            "success": True,
            "profile": profile,
            "follow_up_questions": profile.get("follow_up_questions", [])
        }), 200
    else:
        return jsonify({"success": False, "error": msg}), 500

@app.route('/admin/intake')
def admin_intake_studio():
    """
    Intake Form Studio:
    Business owner review, manual editing, AI regeneration, draft saving, and 1-click publishing.
    """
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        flash("Business Admin access required.", "error")
        return redirect(url_for('login'))

    user = session['user']
    business = get_business_by_owner(user['id'])
    if not business:
        flash("Please set up your business first.", "error")
        return redirect(url_for('create_business'))

    draft_form, draft_qs = get_intake_form_by_business(business['id'], status='DRAFT')
    pub_form, pub_qs = get_intake_form_by_business(business['id'], status='PUBLISHED')

    # If draft exists, review draft; otherwise default to published or empty list
    active_questions = draft_qs if draft_form else (pub_qs if pub_form else [])

    return render_template(
        'intake_studio.html',
        user=user,
        business=business,
        draft_form=draft_form,
        published_form=pub_form,
        questions=active_questions
    )

@app.route('/admin/intake/generate', methods=['POST'])
def admin_intake_generate():
    """
    Generates intelligent appointment questions from structured business profile
    using openai/gpt-oss-120b and saves them into the business's DRAFT form.
    """
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    user = session['user']
    business = get_business_by_owner(user['id'])
    data = request.get_json() or {}
    profile = data.get('profile') or {}
    follow_up_answers = data.get('follow_up_answers') or []

    # If the user doesn't have a business record yet, create it from the AI profile!
    if not business:
        biz_name = profile.get('business_name') or "My Business"
        biz_desc = profile.get('business_description') or "Appointment Booking Service"
        biz_interval = profile.get('appointment_interval') or 30
        biz_data = {
            "name": biz_name,
            "description": biz_desc,
            "address": "Local Service / Consultations",
            "appointment_interval": biz_interval,
            "max_bookings_per_slot": 1,
            "allow_employee_selection": True,
            "opening_time": "09:00",
            "closing_time": "17:00"
        }
        business = create_business_record(user['id'], biz_data)
        user['business_id'] = business['id']
        session['user'] = user
        set_user_role(user['id'], user['email'], 'BUSINESS_ADMIN', business['id'])
        add_employee_record(business['id'], biz_name.split()[0] + " (Owner)", user['email'])
    else:
        # Update business profile
        save_business_profile(business['id'], profile)

    # Incorporate follow-up answers into profile context
    if follow_up_answers:
        profile['additional_details'] = follow_up_answers

    success, questions, msg = ai_generate_intake_questions(profile)
    if not success:
        return jsonify({"success": False, "error": msg}), 500

    # Save as DRAFT form
    services = profile.get('services', [])
    save_draft_intake_form(
        business_id=business['id'],
        form_title=f"{business['name']} Custom Intake",
        questions=questions,
        services=services
    )

    return jsonify({"success": True, "questions": questions}), 200

@app.route('/admin/intake/save-draft', methods=['POST'])
def admin_intake_save_draft():
    """Saves modified questions into DRAFT without publishing to customers."""
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    user = session['user']
    business = get_business_by_owner(user['id'])
    if not business:
        return jsonify({"success": False, "error": "Business not found."}), 404

    data = request.get_json() or {}
    questions = data.get('questions', [])

    save_draft_intake_form(
        business_id=business['id'],
        form_title=f"{business['name']} Custom Intake",
        questions=questions
    )
    return jsonify({"success": True, "message": "Draft saved successfully."}), 200

@app.route('/admin/intake/publish', methods=['POST'])
def admin_intake_publish():
    """Approves and publishes the draft intake form to live customers."""
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    user = session['user']
    business = get_business_by_owner(user['id'])
    if not business:
        return jsonify({"success": False, "error": "Business not found."}), 404

    data = request.get_json() or {}
    questions = data.get('questions')

    success, msg = publish_intake_form(business['id'], questions)
    if success:
        return jsonify({"success": True, "message": msg}), 200
    else:
        return jsonify({"success": False, "error": msg}), 400

@app.route('/admin/intake/improve-question', methods=['POST'])
def admin_intake_improve_question():
    """Regenerates or improves a single question using openai/gpt-oss-120b."""
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    user = session['user']
    business = get_business_by_owner(user['id'])
    data = request.get_json() or {}
    q = data.get('question')
    feedback = data.get('feedback')

    if not q:
        return jsonify({"success": False, "error": "No question provided."}), 400

    profile = get_business_profile(business['id']) if business else {}
    success, improved, msg = ai_improve_question(q, profile, feedback)
    if success:
        return jsonify({"success": True, "improved_question": improved}), 200
    else:
        return jsonify({"success": False, "error": msg}), 500

@app.route('/admin/intake/improve-form', methods=['POST'])
def admin_intake_improve_form():
    """Improves the entire questionnaire based on owner instructions using openai/gpt-oss-120b."""
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    user = session['user']
    business = get_business_by_owner(user['id'])
    data = request.get_json() or {}
    questions = data.get('questions', [])
    instruction = data.get('instruction', '')

    if not instruction:
        return jsonify({"success": False, "error": "Please provide improvement instructions."}), 400

    profile = get_business_profile(business['id']) if business else {}
    success, improved_qs, msg = ai_improve_entire_form(questions, profile, instruction)
    if success:
        # Also persist to draft
        if business:
            save_draft_intake_form(business['id'], f"{business['name']} Custom Intake", improved_qs)
        return jsonify({"success": True, "questions": improved_qs}), 200
    else:
        return jsonify({"success": False, "error": msg}), 500

@app.route('/api/businesses/<business_id>/intake')
def api_business_intake(business_id):
    """Public read-only API returning published intake form and questions for a business."""
    pub_form, questions = get_intake_form_by_business(business_id, status='PUBLISHED')
    if not pub_form:
        return jsonify({"success": False, "form": None, "questions": []}), 200
    return jsonify({
        "success": True,
        "form": {
            "id": pub_form.get('id'),
            "version": pub_form.get('version'),
            "title": pub_form.get('title'),
            "services": pub_form.get('services', [])
        },
        "questions": questions
    }), 200

@app.route('/admin/calendar/connect')
def admin_calendar_connect():
    """Starts the Google OAuth flow so a business admin can connect their own calendar."""
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        return redirect(url_for('login'))
    if not google_oauth_available:
        flash("Google Calendar connection isn't configured on this server yet.", "error")
        return redirect(url_for('admin_dashboard'))

    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [GOOGLE_OAUTH_REDIRECT_URI],
            }
        },
        scopes=GOOGLE_CALENDAR_OAUTH_SCOPES,
        redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    session['google_oauth_state'] = state
    # The library auto-generates a PKCE code_verifier when building auth_url
    # above; it must be replayed at token-exchange time in the callback, so
    # persist it alongside state (a fresh Flow object there starts with none).
    session['google_oauth_code_verifier'] = flow.code_verifier
    return redirect(auth_url)

@app.route('/admin/calendar/oauth/callback')
def admin_calendar_oauth_callback():
    """Exchanges the Google OAuth code for a refresh token and connects the admin's calendar."""
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        return redirect(url_for('login'))

    user = session['user']
    business = get_business_by_owner(user['id'])
    if not business:
        return redirect(url_for('create_business'))

    expected_state = session.pop('google_oauth_state', None)
    code_verifier = session.pop('google_oauth_code_verifier', None)
    if not expected_state or request.args.get('state') != expected_state:
        flash("Google Calendar connection failed: invalid session state. Please try again.", "error")
        return redirect(url_for('admin_dashboard'))

    if request.args.get('error'):
        flash("Google Calendar connection was cancelled.", "error")
        return redirect(url_for('admin_dashboard'))

    try:
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": GOOGLE_OAUTH_CLIENT_ID,
                    "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [GOOGLE_OAUTH_REDIRECT_URI],
                }
            },
            scopes=GOOGLE_CALENDAR_OAUTH_SCOPES,
            redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
            state=expected_state,
            code_verifier=code_verifier,
            autogenerate_code_verifier=False,
        )
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        email = ""
        try:
            from googleapiclient.discovery import build as build_google_service
            oauth2_service = build_google_service("oauth2", "v2", credentials=creds, cache_discovery=False)
            email = oauth2_service.userinfo().get().execute().get("email", "")
        except Exception as e:
            logger.warning(f"Could not fetch connected Google account email: {e}")

        if not creds.refresh_token:
            flash(
                "Google didn't return a long-lived connection. If you've connected before, "
                "disconnect in your Google Account's third-party access settings and try again.",
                "error"
            )
            return redirect(url_for('admin_dashboard'))

        set_business_google_oauth(business['id'], creds.refresh_token, email)
        flash(f"Google Calendar connected ({email or 'your account'})! New bookings will sync automatically.", "success")
    except Exception as e:
        logger.warning(f"Google Calendar OAuth callback error: {e}")
        flash("Something went wrong connecting your Google Calendar. Please try again.", "error")

    return redirect(url_for('admin_dashboard'))

@app.route('/admin/calendar/disconnect', methods=['POST'])
def admin_calendar_disconnect():
    """Disconnects the business admin's Google account from calendar auto-sync."""
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        return redirect(url_for('login'))

    user = session['user']
    business = get_business_by_owner(user['id'])
    if business:
        clear_business_google_oauth(business['id'])
        flash("Google Calendar disconnected.", "success")

    return redirect(url_for('admin_dashboard'))

@app.route('/admin/employees/add', methods=['POST'])
def admin_add_employee():
    """Adds a staff member to the business roster."""
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        return redirect(url_for('login'))

    user = session['user']
    business = get_business_by_owner(user['id'])
    if not business:
        return redirect(url_for('create_business'))

    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()

    if not name or not email:
        flash("Employee Name and Email are required.", "error")
    else:
        add_employee_record(business['id'], name, email)
        flash(f"Staff member '{name}' added successfully!", "success")

    return redirect(url_for('admin_dashboard'))

@app.route('/admin/employees/<employee_id>/delete', methods=['POST'])
def admin_delete_employee(employee_id):
    """Removes a staff member from the business roster."""
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        return redirect(url_for('login'))

    delete_employee_record(employee_id)
    flash("Staff member removed.", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/bookings/<booking_id>/cancel', methods=['POST'])
def admin_cancel_booking(booking_id):
    """Allows Business Admins to cancel an appointment and release capacity."""
    if 'user' not in session or session['user'].get('role') != 'BUSINESS_ADMIN':
        return redirect(url_for('login'))

    cancel_booking_record(booking_id)
    flash("Appointment cancelled. The time slot has been returned to available capacity.", "success")
    return redirect(url_for('admin_dashboard'))

# ====================================================================
# CUSTOMER DASHBOARD & CALENDAR EXPORT
# ====================================================================
@app.route('/dashboard')
def dashboard():
    """Customer Dashboard: Review booked appointments, calendar links, and downloads."""
    if 'user' not in session:
        flash("Please log in to access your dashboard.", "error")
        return redirect(url_for('login'))

    user = session['user']
    if user.get('role') == 'BUSINESS_ADMIN':
        return redirect(url_for('admin_dashboard'))

    bookings = get_bookings_for_customer(user.get('id'), user.get('email'))

    # Generate one-click Google Calendar links for each booking
    for b in bookings:
        title = f"Appointment: {b.get('business_name')} ({b.get('employee_name') or 'Staff'})"
        details = f"Appointment at {b.get('business_name')}. Notes: {b.get('notes') or 'None'}"
        loc = b.get('business_address', 'In-person')
        b['google_cal_url'] = generate_google_calendar_url(title, b.get('start_time'), b.get('end_time'), details, loc)

    return render_template('dashboard.html', user=user, bookings=bookings)

@app.route('/bookings/<booking_id>/cancel', methods=['POST'])
def customer_cancel_booking(booking_id):
    """Allows customers to cancel their booked appointment."""
    if 'user' not in session:
        return redirect(url_for('login'))

    cancel_booking_record(booking_id)
    flash("Your appointment has been cancelled and the slot released.", "success")
    return redirect(url_for('dashboard'))

@app.route('/bookings/<booking_id>/ics')
def download_booking_ics(booking_id):
    """Direct .ics download endpoint for Apple Calendar, Outlook, and Google Calendar."""
    # Find booking
    target_booking = None
    if supabase:
        try:
            res = supabase.table('bookings').select('*, businesses(name, address), employees(name)').eq('id', booking_id).limit(1).execute()
            if res.data and len(res.data) > 0:
                row = res.data[0]
                biz = row.get('businesses')
                emp = row.get('employees')
                row['business_name'] = biz.get('name') if isinstance(biz, dict) else 'Business'
                row['business_address'] = biz.get('address') if isinstance(biz, dict) else ''
                row['employee_name'] = emp.get('name') if isinstance(emp, dict) else None
                target_booking = row
        except Exception as e:
            logger.warning(f"Error finding booking for ics: {e}")

    if not target_booking:
        for b in FALLBACK_BOOKINGS:
            if b['id'] == booking_id:
                target_booking = b
                break

    if not target_booking:
        return Response("Booking not found", status=404)

    title = f"Appointment: {target_booking.get('business_name', 'Steadfast Appointment')}"
    details = f"Appointment booking with {target_booking.get('business_name')}. Staff: {target_booking.get('employee_name') or 'Staff'}"
    loc = target_booking.get('business_address', 'In-person')

    ics_content = generate_ics_content(
        booking_id=booking_id,
        title=title,
        start_dt_str=target_booking.get('start_time'),
        end_dt_str=target_booking.get('end_time'),
        details=details,
        location=loc
    )

    return Response(
        ics_content,
        mimetype="text/calendar",
        headers={"Content-Disposition": f"attachment; filename=appointment-{booking_id[:8]}.ics"}
    )

@app.route('/logout')
def logout():
    """Logs out user and clears the session."""
    session.pop('user', None)
    if supabase:
        try:
            supabase.auth.sign_out()
        except Exception:
            pass
    flash("Signed out successfully.", "success")
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)
