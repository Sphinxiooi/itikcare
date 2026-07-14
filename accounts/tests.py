from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.test import TestCase, override_settings

User = get_user_model()


class SignupTests(TestCase):
    """Covers the farm-address question added to self-service signup — must stay
    optional, and an unresolvable address must never block account creation (see
    accounts/forms.py's SignupForm docstring)."""

    def setUp(self):
        # django_ratelimit's default cache backend is process-wide, not per-TestCase —
        # clear it so this class's own POSTs never inherit a stray count left behind by
        # SignupRateLimitTests below (or vice versa, whichever test runs first).
        cache.clear()

    def _post(self, **overrides):
        data = {
            "username": "newfarmer",
            "password1": "a-strong-passw0rd",
            "password2": "a-strong-passw0rd",
        }
        data.update(overrides)
        return self.client.post("/accounts/signup/", data)

    @patch("accounts.views.geocode_address", return_value=(13.5, 123.2))
    def test_signup_with_resolvable_address_saves_latitude_and_longitude(self, mock_geocode):
        self._post(address="Libmanan, Camarines Sur")
        user = User.objects.get(username="newfarmer")
        mock_geocode.assert_called_once_with("Libmanan, Camarines Sur")
        self.assertEqual(user.address, "Libmanan, Camarines Sur")
        self.assertAlmostEqual(float(user.latitude), 13.5)
        self.assertAlmostEqual(float(user.longitude), 123.2)

    @patch("accounts.views.geocode_address", return_value=None)
    def test_signup_with_unresolvable_address_succeeds_with_null_coordinates(self, mock_geocode):
        response = self._post(address="Nowhere Land")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        user = User.objects.get(username="newfarmer")
        self.assertEqual(user.address, "Nowhere Land")
        self.assertIsNone(user.latitude)
        self.assertIsNone(user.longitude)

    @patch("accounts.views.geocode_address")
    def test_signup_without_address_succeeds_with_null_coordinates(self, mock_geocode):
        response = self._post()
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        mock_geocode.assert_not_called()
        user = User.objects.get(username="newfarmer")
        self.assertEqual(user.address, "")
        self.assertIsNone(user.latitude)
        self.assertIsNone(user.longitude)


class SignupRateLimitTests(TestCase):
    """Every signup POST runs a real `train_forecast_model` call (see accounts/views.py
    signup docstring) — an anonymous endpoint that trains a model on demand needs a hard
    per-IP cap before it's safe to expose publicly. Covers that the cap (5/day, see
    accounts/views.py signup's @ratelimit decorator) actually blocks the 6th attempt
    rather than just being decorative."""

    def setUp(self):
        cache.clear()

    def _post(self, username):
        return self.client.post(
            "/accounts/signup/",
            {
                "username": username,
                "password1": "a-strong-passw0rd",
                "password2": "a-strong-passw0rd",
            },
        )

    @patch("accounts.views.call_command")
    def test_sixth_signup_attempt_in_a_day_from_same_ip_is_blocked(self, mock_call_command):
        for i in range(5):
            response = self._post(f"ratelimitfarmer{i}")
            self.assertNotEqual(response.status_code, 403)

        response = self._post("ratelimitfarmer5")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username="ratelimitfarmer5").exists())


class PasswordResetFlowTests(TestCase):
    """Self-service signup has no admin to set an initial password, so a farmer who
    forgets theirs needs a working self-service reset (see accounts/forms.py
    SignupForm's email field docstring). Covers the full request-email -> follow-link
    -> set-new-password -> log-in-with-new-password path end to end."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="resetfarmer", email="resetfarmer@example.com", password="old-passw0rd",
        )

    def test_full_reset_flow_lets_user_log_in_with_new_password(self):
        response = self.client.post(reverse("password_reset"), {"email": "resetfarmer@example.com"})
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reset your password", mail.outbox[0].subject.lower())

        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        confirm_url = reverse("password_reset_confirm", kwargs={"uidb64": uid, "token": token})

        # PasswordResetConfirmView redirects a valid token-bearing GET to a URL with the
        # token swapped for the literal "set-password", stashing the real token in the
        # session — this is Django's own one-time-use-link mechanism, not app code.
        response = self.client.get(confirm_url, follow=True)
        self.assertEqual(response.status_code, 200)

        session_confirm_url = response.redirect_chain[-1][0]
        response = self.client.post(
            session_confirm_url,
            {"new_password1": "brand-new-passw0rd", "new_password2": "brand-new-passw0rd"},
        )
        self.assertRedirects(response, reverse("password_reset_complete"))

        self.assertFalse(self.client.login(username="resetfarmer", password="old-passw0rd"))
        self.assertTrue(self.client.login(username="resetfarmer", password="brand-new-passw0rd"))

    def test_unknown_email_does_not_error_and_sends_no_mail(self):
        response = self.client.post(reverse("password_reset"), {"email": "nobody@example.com"})
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)


@override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id", GOOGLE_OAUTH_CLIENT_SECRET="test-secret")
class GoogleSignInTests(TestCase):
    """Covers accounts/google_oauth.py + accounts/views.py's google_login/
    google_callback — the motivation being that a Google-authenticated account never
    has a local password to forget in the first place (see PasswordResetFlowTests
    above for the email-based path this complements)."""

    def setUp(self):
        cache.clear()

    def _mock_google_response(self, mock_post, mock_get, sub, email, email_verified):
        mock_post.return_value.json.return_value = {"access_token": "fake-access-token"}
        mock_post.return_value.raise_for_status = lambda: None
        mock_get.return_value.json.return_value = {
            "sub": sub, "email": email, "email_verified": email_verified, "name": "Farmer",
        }
        mock_get.return_value.raise_for_status = lambda: None

    def _start_login(self):
        """GET the redirect-to-Google view first, exactly like a real browser would,
        so the session has the `state` value google_callback checks against."""
        response = self.client.get(reverse("google_login"))
        self.assertEqual(response.status_code, 302)
        return self.client.session["google_oauth_state"]

    @patch("accounts.views.call_command")
    @patch("accounts.google_oauth.requests.get")
    @patch("accounts.google_oauth.requests.post")
    def test_new_google_account_is_created_and_logged_in(self, mock_post, mock_get, mock_call_command):
        self._mock_google_response(mock_post, mock_get, "google-sub-123", "newfarmer@example.com", True)
        state = self._start_login()

        response = self.client.get(reverse("google_callback"), {"code": "auth-code", "state": state})

        self.assertRedirects(response, reverse("dashboard"))
        user = User.objects.get(google_sub="google-sub-123")
        self.assertEqual(user.username, "newfarmer")
        self.assertEqual(user.email, "newfarmer@example.com")
        self.assertFalse(user.has_usable_password())
        mock_call_command.assert_called_once_with("train_forecast_model", owner_id=user.id, strict=True)

    @patch("accounts.google_oauth.requests.get")
    @patch("accounts.google_oauth.requests.post")
    def test_verified_email_links_to_existing_local_account(self, mock_post, mock_get):
        existing = User.objects.create_user(
            username="existingfarmer", email="linkme@example.com", password="whatever-pw123",
        )
        self._mock_google_response(mock_post, mock_get, "google-sub-456", "linkme@example.com", True)
        state = self._start_login()

        response = self.client.get(reverse("google_callback"), {"code": "auth-code", "state": state})

        self.assertRedirects(response, reverse("dashboard"))
        existing.refresh_from_db()
        self.assertEqual(existing.google_sub, "google-sub-456")
        self.assertEqual(User.objects.count(), 1)

    @patch("accounts.views.call_command")
    @patch("accounts.google_oauth.requests.get")
    @patch("accounts.google_oauth.requests.post")
    def test_unverified_email_does_not_link_and_creates_separate_account(
        self, mock_post, mock_get, mock_call_command,
    ):
        existing = User.objects.create_user(
            username="existingfarmer", email="unverified@example.com", password="whatever-pw123",
        )
        self._mock_google_response(mock_post, mock_get, "google-sub-789", "unverified@example.com", False)
        state = self._start_login()

        response = self.client.get(reverse("google_callback"), {"code": "auth-code", "state": state})

        self.assertRedirects(response, reverse("dashboard"))
        existing.refresh_from_db()
        self.assertIsNone(existing.google_sub)
        self.assertEqual(User.objects.count(), 2)

    @patch("accounts.google_oauth.requests.get")
    @patch("accounts.google_oauth.requests.post")
    def test_returning_google_user_reuses_the_same_account(self, mock_post, mock_get):
        self._mock_google_response(mock_post, mock_get, "google-sub-123", "newfarmer@example.com", True)
        with patch("accounts.views.call_command"):
            state = self._start_login()
            self.client.get(reverse("google_callback"), {"code": "auth-code", "state": state})
        self.client.logout()

        state = self._start_login()
        response = self.client.get(reverse("google_callback"), {"code": "auth-code-2", "state": state})

        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(User.objects.filter(google_sub="google-sub-123").count(), 1)

    def test_state_mismatch_is_rejected_without_creating_an_account(self):
        self._start_login()

        response = self.client.get(reverse("google_callback"), {"code": "auth-code", "state": "wrong-state"})

        self.assertRedirects(response, reverse("login"))
        self.assertEqual(User.objects.count(), 0)
