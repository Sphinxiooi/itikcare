"""Tests for the session-expiry endpoints and the template filter behind success cards."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from accounts.templatetags.itik_feedback import message_lines

User = get_user_model()


class SessionStatusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()

    def test_anonymous_gets_json_not_a_redirect(self):
        response = self.client.get(reverse("session_status"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"authenticated": False, "seconds_left": 0})

    def test_logged_in_reports_time_left(self):
        self.client.login(username="farmer1", password="pw12345")
        body = self.client.get(reverse("session_status")).json()
        self.assertTrue(body["authenticated"])
        self.assertGreater(body["seconds_left"], 0)
        self.assertLessEqual(body["seconds_left"], settings.SESSION_COOKIE_AGE)

    def test_polling_never_extends_the_session(self):
        self.client.login(username="farmer1", password="pw12345")
        session = self.client.session
        session.set_expiry(120)
        session.save()
        self.client.get(reverse("session_status"))
        body = self.client.get(reverse("session_status")).json()
        self.assertLessEqual(body["seconds_left"], 120)

    def test_status_is_not_cacheable(self):
        response = self.client.get(reverse("session_status"))
        self.assertIn("no-cache", response["Cache-Control"])

    def test_status_rejects_post(self):
        self.assertEqual(self.client.post(reverse("session_status")).status_code, 405)


class SessionExtendTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()

    def test_anonymous_gets_401(self):
        response = self.client.post(reverse("session_extend"))
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json()["authenticated"])

    def test_get_is_not_allowed(self):
        self.client.login(username="farmer1", password="pw12345")
        self.assertEqual(self.client.get(reverse("session_extend")).status_code, 405)

    def test_stay_signed_in_restarts_the_clock(self):
        self.client.login(username="farmer1", password="pw12345")
        session = self.client.session
        session.set_expiry(120)
        session.save()
        body = self.client.post(reverse("session_extend")).json()
        self.assertTrue(body["authenticated"])
        self.assertGreater(body["seconds_left"], 120)


class FeedbackAssetsTests(TestCase):
    def test_login_page_loads_validation_script_and_config(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "itik-feedback.js")
        self.assertContains(response, "itik-feedback.css")
        self.assertContains(response, reverse("session_status"))

    def test_logged_in_page_tells_the_script_who_is_signed_in(self):
        user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client.login(username="farmer1", password="pw12345")
        response = self.client.get(reverse("account_settings"))
        self.assertContains(response, "authenticated: true")
        self.assertContains(response, f'userId: "{user.pk}"')


class MessageLinesFilterTests(TestCase):
    def test_splits_title_and_steps_and_drops_blank_lines(self):
        self.assertEqual(message_lines("Saved.\nStep one\n\nStep two"), ["Saved.", "Step one", "Step two"])

    def test_single_line_is_just_a_title(self):
        self.assertEqual(message_lines("Saved."), ["Saved."])
