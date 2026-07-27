import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from accounts.models import PasswordResetCode

User = get_user_model()


def _tiny_png(size_bytes=None):
    """A minimal valid PNG upload for AccountSettingsForm's avatar field -- Django's
    ImageField uses Pillow to validate the upload is actually an image, so a fake/
    empty file isn't enough. When size_bytes is given, pads the file past that size
    with a PNG comment chunk (real image data first, so it still opens with Pillow)."""
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buffer, format="PNG")
    content = buffer.getvalue()
    if size_bytes is not None and len(content) < size_bytes:
        content += b"\x00" * (size_bytes - len(content))
    return SimpleUploadedFile("avatar.png", content, content_type="image/png")


class SignupTests(TestCase):
    """Covers the farm-address question added to self-service signup — a required
    dropdown of Libmanan barangays (accounts.libmanan_barangays.BARANGAYS), expanded
    into a full geocodable string by SignupForm.clean_address. An unresolvable address
    must never block account creation (see accounts/forms.py's SignupForm docstring)."""

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
            "address": "San Isidro",
        }
        data.update(overrides)
        return self.client.post("/accounts/signup/", data)

    @patch("accounts.views.geocode_address", return_value=(13.5, 123.2))
    def test_signup_with_resolvable_address_saves_latitude_and_longitude(self, mock_geocode):
        self._post(address="San Isidro")
        user = User.objects.get(username="newfarmer")
        mock_geocode.assert_called_once_with("San Isidro, Libmanan, Camarines Sur, Philippines")
        self.assertEqual(user.address, "San Isidro, Libmanan, Camarines Sur, Philippines")
        self.assertAlmostEqual(float(user.latitude), 13.5)
        self.assertAlmostEqual(float(user.longitude), 123.2)

    @patch("accounts.views.geocode_address", return_value=None)
    def test_signup_with_unresolvable_address_succeeds_with_null_coordinates(self, mock_geocode):
        response = self._post(address="Bagacay")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        user = User.objects.get(username="newfarmer")
        self.assertEqual(user.address, "Bagacay, Libmanan, Camarines Sur, Philippines")
        self.assertIsNone(user.latitude)
        self.assertIsNone(user.longitude)

    @patch("accounts.views.geocode_address")
    def test_signup_without_address_fails_validation(self, mock_geocode):
        response = self._post(address="")
        self.assertEqual(response.status_code, 200)
        mock_geocode.assert_not_called()
        self.assertFalse(User.objects.filter(username="newfarmer").exists())

    @patch("accounts.views.geocode_address")
    def test_signup_with_address_outside_barangay_list_fails_validation(self, mock_geocode):
        response = self._post(address="Nowhere Land")
        self.assertEqual(response.status_code, 200)
        mock_geocode.assert_not_called()
        self.assertFalse(User.objects.filter(username="newfarmer").exists())


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
                "address": "San Isidro",
            },
        )

    @patch("accounts.views.geocode_address", return_value=None)
    @patch("accounts.views.call_command")
    def test_sixth_signup_attempt_in_a_day_from_same_ip_is_blocked(self, mock_call_command, mock_geocode):
        for i in range(5):
            response = self._post(f"ratelimitfarmer{i}")
            self.assertNotEqual(response.status_code, 403)

        response = self._post("ratelimitfarmer5")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username="ratelimitfarmer5").exists())


class PasswordResetFlowTests(TestCase):
    """Self-service signup has no admin to set an initial password, so a farmer who
    forgets theirs needs a working self-service reset (see accounts/forms.py
    SignupForm's email field docstring).

    Covers the username -> emailed 6-digit code -> set-new-password -> log-in-with-
    new-password path end to end. Replaces the old tokenized-link flow, which broke in
    practice once the email was opened on a device other than the one running the
    (localhost) dev server.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="resetfarmer", email="resetfarmer@example.com", password="old-passw0rd",
        )

    def _extract_code(self, message):
        # The email body is "Your verification code is: 123456" (see
        # templates/registration/password_reset_email.html) -- pull the 6 digits out.
        for line in message.body.splitlines():
            if "verification code is" in line:
                return line.strip().split()[-1]
        raise AssertionError(f"No verification code found in email body: {message.body!r}")

    def test_full_reset_flow_lets_user_log_in_with_new_password(self):
        response = self.client.post(reverse("password_reset"), {"username": "resetfarmer"})
        self.assertRedirects(response, reverse("password_reset_verify"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reset your password", mail.outbox[0].subject.lower())

        code = self._extract_code(mail.outbox[0])
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

        response = self.client.post(
            reverse("password_reset_verify"),
            {
                "code": code,
                "new_password1": "brand-new-passw0rd",
                "new_password2": "brand-new-passw0rd",
            },
        )
        self.assertRedirects(response, reverse("password_reset_complete"))

        self.assertFalse(self.client.login(username="resetfarmer", password="old-passw0rd"))
        self.assertTrue(self.client.login(username="resetfarmer", password="brand-new-passw0rd"))

    def test_unknown_username_does_not_error_and_sends_no_mail(self):
        response = self.client.post(reverse("password_reset"), {"username": "nobody"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_username_with_no_email_sends_no_mail(self):
        User.objects.create_user(username="noemailfarmer", password="old-passw0rd")
        response = self.client.post(reverse("password_reset"), {"username": "noemailfarmer"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_wrong_code_locks_after_max_attempts(self):
        self.client.post(reverse("password_reset"), {"username": "resetfarmer"})
        code = self._extract_code(mail.outbox[0])
        wrong_code = "000000" if code != "000000" else "111111"

        for _ in range(4):
            response = self.client.post(
                reverse("password_reset_verify"),
                {
                    "code": wrong_code,
                    "new_password1": "brand-new-passw0rd",
                    "new_password2": "brand-new-passw0rd",
                },
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post(
            reverse("password_reset_verify"),
            {
                "code": wrong_code,
                "new_password1": "brand-new-passw0rd",
                "new_password2": "brand-new-passw0rd",
            },
        )
        self.assertRedirects(response, reverse("password_reset"))
        self.assertFalse(PasswordResetCode.objects.filter(user=self.user).exists())

        # The real code no longer works either -- it was deleted once attempts ran out.
        response = self.client.post(
            reverse("password_reset_verify"),
            {
                "code": code,
                "new_password1": "brand-new-passw0rd",
                "new_password2": "brand-new-passw0rd",
            },
        )
        self.assertRedirects(response, reverse("password_reset"))
        self.assertFalse(self.client.login(username="resetfarmer", password="brand-new-passw0rd"))

    def test_expired_code_is_rejected(self):
        self.client.post(reverse("password_reset"), {"username": "resetfarmer"})
        code = self._extract_code(mail.outbox[0])
        PasswordResetCode.objects.filter(user=self.user).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        response = self.client.post(
            reverse("password_reset_verify"),
            {
                "code": code,
                "new_password1": "brand-new-passw0rd",
                "new_password2": "brand-new-passw0rd",
            },
        )
        self.assertRedirects(response, reverse("password_reset"))
        self.assertFalse(self.client.login(username="resetfarmer", password="brand-new-passw0rd"))


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
        """A brand-new Google account has no name/address at all, so it's sent to
        Account Settings to fill those in — not the dashboard (see
        accounts/views.py's google_callback docstring)."""
        self._mock_google_response(mock_post, mock_get, "google-sub-123", "newfarmer@example.com", True)
        state = self._start_login()

        response = self.client.get(reverse("google_callback"), {"code": "auth-code", "state": state})

        self.assertRedirects(response, reverse("account_settings"))
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

        # New account, same as test_new_google_account_is_created_and_logged_in above.
        self.assertRedirects(response, reverse("account_settings"))
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

    @patch("accounts.google_oauth.requests.get")
    @patch("accounts.google_oauth.requests.post")
    def test_login_via_google_returns_to_the_next_url_instead_of_the_dashboard(
        self, mock_post, mock_get,
    ):
        """A farmer redirected to /accounts/login/?next=<protected page> who then
        chooses "Sign in with Google" must land back on that page, not always the
        dashboard — same as the plain username/password login already does. Only
        applies to a *returning* Google sign-in: a brand-new one is always sent to
        Account Settings first (see test_new_google_account_is_created_and_logged_in),
        so this account is pre-linked to google-sub-123 to test the returning path.
        """
        User.objects.create_user(
            username="newfarmer", email="newfarmer@example.com", google_sub="google-sub-123",
        )
        self._mock_google_response(mock_post, mock_get, "google-sub-123", "newfarmer@example.com", True)
        next_url = reverse("log_daily_data")

        response = self.client.get(reverse("google_login"), {"next": next_url})
        self.assertEqual(response.status_code, 302)
        state = self.client.session["google_oauth_state"]

        response = self.client.get(reverse("google_callback"), {"code": "auth-code", "state": state})

        self.assertRedirects(response, next_url, fetch_redirect_response=False)

    @patch("accounts.google_oauth.requests.get")
    @patch("accounts.google_oauth.requests.post")
    def test_login_via_google_ignores_an_off_site_next_url(self, mock_post, mock_get):
        """A `?next=` pointing off-site must never be honored -- otherwise Google
        sign-in could be turned into an open redirect. Uses a returning account (see
        test_login_via_google_returns_to_the_next_url_instead_of_the_dashboard above)
        so the assertion is about the off-site `next` being rejected, not about the
        separate brand-new-account-goes-to-Account-Settings behavior."""
        User.objects.create_user(
            username="newfarmer", email="newfarmer@example.com", google_sub="google-sub-123",
        )
        self._mock_google_response(mock_post, mock_get, "google-sub-123", "newfarmer@example.com", True)

        response = self.client.get(reverse("google_login"), {"next": "https://evil.example/phish"})
        self.assertEqual(response.status_code, 302)
        state = self.client.session["google_oauth_state"]

        response = self.client.get(reverse("google_callback"), {"code": "auth-code", "state": state})

        self.assertRedirects(response, reverse("dashboard"))


class AccountSettingsTests(TestCase):
    """Covers accounts/views.py's account_settings — the personal-data box reachable
    from the header avatar (templates/base.html), letting a farmer edit their own
    name/username/email/farm address/profile picture (accounts/forms.py's
    AccountSettingsForm)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="farmerjuan", email="juan@example.com", password="a-strong-passw0rd",
        )
        self.client.force_login(self.user)

    def _post(self, **overrides):
        data = {
            "first_name": "",
            "last_name": "",
            "username": "farmerjuan",
            "email": "juan@example.com",
            "address": "",
        }
        data.update(overrides)
        return self.client.post(reverse("account_settings"), data)

    def test_get_renders_both_the_personal_info_and_flock_status_boxes(self):
        response = self.client.get(reverse("account_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Personal Information")
        # No flock registered yet in this test's fixture -- _flock_profile_panel.html
        # falls back to its "Register Flock" box, proving it rendered at all.
        self.assertContains(response, "Register Flock")

    @patch("accounts.views.geocode_address", return_value=(13.5, 123.2))
    def test_updating_name_email_and_address_persists_and_regeocodes(self, mock_geocode):
        response = self._post(
            first_name="Juan", last_name="Dela Cruz",
            email="juan.delacruz@example.com", address="San Isidro",
        )
        self.assertRedirects(response, reverse("account_settings"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Juan")
        self.assertEqual(self.user.last_name, "Dela Cruz")
        self.assertEqual(self.user.email, "juan.delacruz@example.com")
        self.assertEqual(self.user.address, "San Isidro, Libmanan, Camarines Sur, Philippines")
        mock_geocode.assert_called_once_with("San Isidro, Libmanan, Camarines Sur, Philippines")
        self.assertAlmostEqual(float(self.user.latitude), 13.5)
        self.assertAlmostEqual(float(self.user.longitude), 123.2)

    @patch("accounts.views.geocode_address")
    def test_unchanged_address_does_not_trigger_a_geocode_call(self, mock_geocode):
        self._post(first_name="Juan")
        mock_geocode.assert_not_called()

    def test_existing_address_pre_selects_the_matching_barangay_on_the_form(self):
        self.user.address = "San Isidro, Libmanan, Camarines Sur, Philippines"
        self.user.save(update_fields=["address"])
        response = self.client.get(reverse("account_settings"))
        self.assertEqual(response.context["account_form"]["address"].value(), "San Isidro")

    def test_email_already_used_by_another_account_is_rejected(self):
        User.objects.create_user(username="other", email="taken@example.com", password="whatever-pw123")
        response = self._post(email="taken@example.com")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "juan@example.com")

    def test_can_keep_own_existing_email_unchanged(self):
        response = self._post(email="juan@example.com")
        self.assertRedirects(response, reverse("account_settings"))

    def test_oversized_avatar_upload_is_rejected(self):
        response = self._post(avatar=_tiny_png(size_bytes=6 * 1024 * 1024))
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)

    def test_valid_avatar_upload_is_saved(self):
        response = self._post(avatar=_tiny_png())
        self.assertRedirects(response, reverse("account_settings"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.avatar)

    def test_logged_out_visitor_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("account_settings"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)


class LoginIdentifierTests(TestCase):
    """Covers accounts/auth_backends.py's UsernameEmailOrFullNameBackend and
    StyledAuthenticationForm's ambiguity check — logging in with a username, email, or
    full name, all through the same single login field."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="farmerjuan",
            email="juan@example.com",
            password="a-strong-passw0rd",
            first_name="Juan",
            last_name="Dela Cruz",
        )

    def _login(self, identifier, password="a-strong-passw0rd"):
        return self.client.post(
            reverse("login"), {"username": identifier, "password": password}
        )

    def test_login_with_username_still_works(self):
        response = self._login("farmerjuan")
        self.assertRedirects(response, reverse("dashboard"))

    def test_login_with_email_works(self):
        response = self._login("juan@example.com")
        self.assertRedirects(response, reverse("dashboard"))

    def test_login_with_email_is_case_insensitive(self):
        response = self._login("JUAN@EXAMPLE.COM")
        self.assertRedirects(response, reverse("dashboard"))

    def test_login_with_full_name_works_when_unambiguous(self):
        response = self._login("Juan Dela Cruz")
        self.assertRedirects(response, reverse("dashboard"))

    def test_login_with_shared_full_name_is_rejected_as_ambiguous(self):
        User.objects.create_user(
            username="farmerjuan2",
            password="another-strong-pw2",
            first_name="Juan",
            last_name="Dela Cruz",
        )
        response = self._login("Juan Dela Cruz")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "matches more than one account")
        # Neither account was logged in.
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_wrong_password_still_fails(self):
        response = self._login("farmerjuan", password="wrong-password")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
