"""Tests for the farmer-facing feedback layer: field error guidance and success cards."""

from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from .forms import DailyLogForm, FlockRegisterForm
from .models import DailyLog, Flock

User = get_user_model()

VALID_LOG = {
    "date": "2024-01-01",
    "flock_size": 240,
    "egg_count": 150,
    "feed_intake_kg": "40.0",
    "flock_age_weeks": 25,
    "temperature_c": "28.0",
    "humidity_pct": "75.0",
}


class FieldErrorGuidanceTests(TestCase):
    """Every error names the field and says how to fix it (not Django's bare defaults)."""

    def errors_for(self, **overrides):
        data = {**VALID_LOG, **overrides}
        form = DailyLogForm(data)
        form.is_valid()
        return form.errors

    def test_value_above_the_maximum_says_what_to_check(self):
        message = self.errors_for(egg_count=5000)["egg_count"][0]
        self.assertIn("egg count", message)
        self.assertIn("1000", message)
        self.assertIn("extra digit", message)

    def test_value_below_the_minimum_says_what_to_raise_it_to(self):
        message = self.errors_for(flock_size=0)["flock_size"][0]
        self.assertIn("flock size", message)
        self.assertIn("1 or more", message)

    def test_missing_value_gives_an_example(self):
        message = self.errors_for(egg_count="")["egg_count"][0]
        self.assertIn("Enter today's egg count", message)
        self.assertIn("85", message)  # the example number

    def test_non_numeric_value_says_digits_only(self):
        message = self.errors_for(temperature_c="hot")["temperature_c"][0]
        self.assertIn("temperature", message)
        self.assertIn("must be a number", message)

    def test_each_error_is_attached_to_its_own_field_only(self):
        errors = self.errors_for(egg_count=5000, humidity_pct=250)
        self.assertEqual(set(errors), {"egg_count", "humidity_pct"})

    def test_flock_register_form_uses_the_same_guidance(self):
        form = FlockRegisterForm({"started_on": "2024-01-01", "flock_size": 0, "flock_age_weeks": 5, "feed_intake_kg": "3"})
        self.assertFalse(form.is_valid())
        self.assertIn("1 or more", form.errors["flock_size"][0])


@override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
class SuccessCardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))

    @patch("farm.views.trigger_retrain")
    def test_saving_a_log_shows_a_success_card_with_next_steps(self, _retrain):
        response = self.client.post("/log-daily-data/", {**VALID_LOG, "confirmed": "1"}, follow=True)
        self.assertTrue(DailyLog.objects.filter(date=date(2024, 1, 1)).exists())
        html = response.content.decode()
        self.assertIn("itik-success", html)
        self.assertIn("Daily data saved.", html)
        self.assertIn("What happens next", html)
        self.assertIn("Farm Records", html)

    def test_a_plain_success_message_renders_as_a_card_without_a_next_list(self):
        # Any view's plain messages.success() is upgraded to the card too.
        response = self.client.post("/flock/toggle-caging/", {}, follow=True)
        html = response.content.decode()
        self.assertIn("itik-success", html)
        self.assertNotIn("What happens next", html)

    def test_invalid_log_shows_the_guidance_inline_and_keeps_the_page(self):
        response = self.client.post("/log-daily-data/", {**VALID_LOG, "egg_count": 5000})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "extra digit")
        self.assertFalse(DailyLog.objects.exists())

    def test_log_form_is_marked_as_a_draft_form(self):
        response = self.client.get("/log-daily-data/")
        self.assertContains(response, 'data-draft="daily-log"')
        self.assertContains(response, "itik-feedback.js")
