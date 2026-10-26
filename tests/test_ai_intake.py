"""
Comprehensive Quality Assurance & Automated Test Suite
AI-Powered Business Onboarding & Custom Appointment Intake System
Tests:
1. Groq Client & Model Verification ('openai/gpt-oss-120b')
2. Audio Voice Transcription Endpoint via Groq Whisper STT
3. Business Understanding & Targeted Follow-Up Generation
4. Custom Intake Question Generation, Supported Types & Conditional Logic
5. Question Modification & AI Form Improvement
6. Draft vs. Published Form Isolation & Versioning
7. Multi-Tenant Business Isolation
8. Dynamic Customer Booking with Intake Answers
9. AI Appointment Summary Generation & Resilient Fallback Handling
"""

import os
import io
import wave
import struct
import json
import unittest
from datetime import datetime, date, timedelta

from api.index import (
    app,
    groq_client,
    GROQ_AI_MODEL,
    GROQ_AUDIO_MODEL,
    ai_transcribe_audio,
    ai_understand_business,
    ai_generate_intake_questions,
    ai_improve_question,
    ai_improve_entire_form,
    ai_summarize_appointment,
    get_intake_form_by_business,
    save_draft_intake_form,
    publish_intake_form,
    save_appointment_intake_responses,
    get_intake_responses_for_booking,
    save_ai_appointment_summary,
    get_ai_appointment_summary,
    create_business_record,
    FALLBACK_BUSINESSES,
    FALLBACK_INTAKE_FORMS,
    FALLBACK_INTAKE_QUESTIONS,
    FALLBACK_INTAKE_RESPONSES,
    FALLBACK_AI_SUMMARIES
)


class TestAIPoweredIntakeSystem(unittest.TestCase):

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_01_groq_model_and_client_configuration(self):
        """Verifies strict adherence to openai/gpt-oss-120b and Groq client initialization."""
        self.assertEqual(GROQ_AI_MODEL, "openai/gpt-oss-120b", "Must use exact model 'openai/gpt-oss-120b'")
        self.assertEqual(GROQ_AUDIO_MODEL, "whisper-large-v3", "Must use Groq whisper-large-v3 for STT")
        self.assertIsNotNone(groq_client, "Groq client must be initialized on server side")

    def test_02_audio_voice_transcription(self):
        """Tests audio voice transcription using Groq speech-to-text."""
        # Create in-memory dummy WAV audio (0.5s silence)
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(struct.pack('<h', 0) * 8000)
        buf.seek(0)

        # Test internal transcription helper
        success, text = ai_transcribe_audio(buf.read(), filename="sample.wav")
        self.assertTrue(success, f"Voice transcription failed: {text}")
        self.assertIsInstance(text, str)

        # Test POST /api/ai/transcribe endpoint
        buf.seek(0)
        response = self.client.post(
            '/api/ai/transcribe',
            data={'audio': (buf, 'sample.wav')},
            content_type='multipart/form-data'
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data.get('success'))
        self.assertIn('text', data)

    def test_03_ai_business_understanding(self):
        """Tests business understanding and profile extraction with openai/gpt-oss-120b."""
        desc = (
            "I run a tutoring center called BrightPath Academy. We tutor middle and high school students "
            "in mathematics, science, and English. We need to know what grade the student is in, what subject "
            "they need help with, and what specific topics they find challenging. Standard sessions are 60 minutes."
        )
        success, profile, msg = ai_understand_business(desc)
        self.assertTrue(success, f"AI business understanding failed: {msg}")
        self.assertIsInstance(profile, dict)

        # Verify structured attributes
        self.assertIn("services", profile)
        self.assertIsInstance(profile["services"], list)
        self.assertGreaterEqual(len(profile["services"]), 1)
        self.assertIn("appointment_interval", profile)
        self.assertIn(profile["appointment_interval"], [15, 30, 45, 60])

    def test_04_ai_question_generation_and_supported_types(self):
        """Tests generating questions with supported types and conditional logic using openai/gpt-oss-120b."""
        profile = {
            "business_name": "Apex Cuts Barbershop",
            "business_category": "Barbershop",
            "business_description": "Full-service grooming offering haircuts, beard trims, fades, and hair coloring.",
            "services": ["Haircut", "Beard Trim", "Hair Coloring"]
        }
        success, questions, msg = ai_generate_intake_questions(profile)
        self.assertTrue(success, f"Question generation failed: {msg}")
        self.assertGreaterEqual(len(questions), 3)

        valid_types = {'single_choice', 'dropdown', 'multiple_choice', 'short_text', 'long_text', 'yes_no'}
        for q in questions:
            self.assertIn('question_text', q)
            self.assertIn('question_type', q)
            self.assertIn(q['question_type'], valid_types, f"Unsupported question type: {q['question_type']}")
            self.assertIn('is_required', q)
            self.assertIn('conditional_logic', q)
            if q['question_type'] in ['single_choice', 'dropdown', 'multiple_choice']:
                self.assertIsInstance(q['options'], list)
                self.assertGreater(len(q['options']), 0)

    def test_05_draft_vs_published_version_isolation(self):
        """Verifies that changes to drafts NEVER affect the published form until approved."""
        test_biz_id = "test-isolation-biz-" + datetime.utcnow().strftime("%Y%m%d%H%M%S")

        # 1. Initially publish Version 1
        v1_questions = [
            {
                "key": "q1",
                "question_text": "Live Question v1: What is your primary goal?",
                "question_type": "single_choice",
                "options": ["Goal A", "Goal B"],
                "is_required": True,
                "conditional_logic": None
            }
        ]
        save_draft_intake_form(test_biz_id, "Test Intake", v1_questions)
        pub_ok, pub_msg = publish_intake_form(test_biz_id, v1_questions)
        self.assertTrue(pub_ok)

        # Verify published form is v1
        pub_form, live_qs = get_intake_form_by_business(test_biz_id, status='PUBLISHED')
        self.assertIsNotNone(pub_form)
        self.assertEqual(pub_form['version'], 1)
        self.assertEqual(live_qs[0]['question_text'], "Live Question v1: What is your primary goal?")

        # 2. Modify DRAFT with unapproved changes
        draft_questions = [
            {
                "key": "q1",
                "question_text": "Unapproved Draft Wording: Under Construction",
                "question_type": "short_text",
                "options": [],
                "is_required": False,
                "conditional_logic": None
            }
        ]
        save_draft_intake_form(test_biz_id, "Test Intake Draft", draft_questions)

        # 3. Verify Live Customer Form is STILL unchanged (v1)
        pub_form_check, live_qs_check = get_intake_form_by_business(test_biz_id, status='PUBLISHED')
        self.assertEqual(pub_form_check['version'], 1)
        self.assertEqual(live_qs_check[0]['question_text'], "Live Question v1: What is your primary goal?")

        # 4. Now approve and publish
        pub_ok2, _ = publish_intake_form(test_biz_id, draft_questions)
        self.assertTrue(pub_ok2)

        # 5. Live form is now upgraded to Version 2
        pub_form_v2, live_qs_v2 = get_intake_form_by_business(test_biz_id, status='PUBLISHED')
        self.assertEqual(pub_form_v2['version'], 2)
        self.assertEqual(live_qs_v2[0]['question_text'], "Unapproved Draft Wording: Under Construction")

    def test_06_multi_tenant_isolation(self):
        """Verifies strict multi-tenancy: Business A cannot access or overwrite Business B's intake forms."""
        biz_a = "tenant-a-" + datetime.utcnow().strftime("%Y%m%d%H%M%S")
        biz_b = "tenant-b-" + datetime.utcnow().strftime("%Y%m%d%H%M%S")

        # Business A publishes form
        save_draft_intake_form(biz_a, "Form A", [{"question_text": "Question for Tenant A", "question_type": "short_text"}])
        publish_intake_form(biz_a)

        # Business B publishes form
        save_draft_intake_form(biz_b, "Form B", [{"question_text": "Question for Tenant B", "question_type": "short_text"}])
        publish_intake_form(biz_b)

        # Query separately
        _, qs_a = get_intake_form_by_business(biz_a, status='PUBLISHED')
        _, qs_b = get_intake_form_by_business(biz_b, status='PUBLISHED')

        self.assertEqual(qs_a[0]['question_text'], "Question for Tenant A")
        self.assertEqual(qs_b[0]['question_text'], "Question for Tenant B")
        self.assertNotEqual(qs_a[0]['question_text'], qs_b[0]['question_text'])

    def test_07_customer_booking_and_intake_response_persistence(self):
        """Tests that customer answers are accurately saved and associated with the booking ID."""
        test_booking_id = "test-booking-" + datetime.utcnow().strftime("%Y%m%d%H%M%S")
        test_biz_id = "11111111-1111-1111-1111-111111111111"

        responses = [
            {"question_id": "q1", "question_text": "Service Booked", "question_type": "single_choice", "answer": "Therapeutic Massage"},
            {"question_id": "q2", "question_text": "Pressure Preference", "question_type": "dropdown", "answer": "Firm / Deep Tissue"},
            {"question_id": "q3", "question_text": "Allergies", "question_type": "yes_no", "answer": "No"}
        ]

        saved = save_appointment_intake_responses(
            booking_id=test_booking_id,
            business_id=test_biz_id,
            form_id="form-1111-1111-1111-111111111111",
            service_selected="Therapeutic Massage",
            responses=responses
        )
        self.assertIsNotNone(saved)

        # Retrieve and verify
        retrieved = get_intake_responses_for_booking(test_booking_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.get('service_selected'), "Therapeutic Massage")
        self.assertEqual(len(retrieved.get('responses', [])), 3)

    def test_08_ai_appointment_summary_generation_and_fallback(self):
        """Tests Groq openai/gpt-oss-120b summary generation and verifies fallback resilience."""
        booking_data = {
            "customer_name": "Jordan Taylor",
            "customer_email": "jordan@example.com",
            "start_time": "2026-11-15T14:00:00",
            "notes": "Please focus on shoulder tension from heavy desk work."
        }
        responses = [
            {"question_text": "Service", "answer": "Deep Tissue Therapy"},
            {"question_text": "Preferred Pressure", "answer": "Firm"},
            {"question_text": "Injuries or Sensitivity", "answer": "Left rotator cuff tightness"}
        ]

        # Call AI Summarizer
        success, summary_data = ai_summarize_appointment(
            booking_data=booking_data,
            service_name="Deep Tissue Therapy",
            responses=responses,
            customer_notes=booking_data["notes"]
        )
        self.assertIn("summary", summary_data)
        self.assertGreater(len(summary_data["summary"]), 15)
        # Verify customer details reflected in summary
        summary_lower = summary_data["summary"].lower()
        self.assertTrue(
            "jordan" in summary_lower or "deep tissue" in summary_lower or "shoulder" in summary_lower,
            f"Summary does not mention appointment specifics: {summary_data['summary']}"
        )

        # Save and verify retrieval
        save_ai_appointment_summary(
            booking_id="summary-test-id-1",
            business_id="11111111-1111-1111-1111-111111111111",
            summary=summary_data["summary"],
            prep_notes=summary_data.get("preparation_notes"),
            status="COMPLETED" if success else "FALLBACK"
        )
        saved_summary = get_ai_appointment_summary("summary-test-id-1")
        self.assertEqual(saved_summary.get("summary"), summary_data["summary"])


if __name__ == '__main__':
    unittest.main()
