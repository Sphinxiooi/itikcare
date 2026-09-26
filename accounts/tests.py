import io
from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user, get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from accounts.models import PasswordResetCode
from farm.models import Flock

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
            "full_name": "New Farmer",
            "contact": "",
            "password1": "a-strong-passw0rd",
            "password2": "a-strong-passw0rd",
            "address": "San Isidro",
            "privacy_consent": "on",
        }
        data.update(overrides)
        return self.client.post("/accounts/signup/", data)

    @patch("accounts.views.geocode_address", return_value=(13.5, 123.2))
    def test_signup_with_resolvable_address_saves_latitude_and_longitude(self, mock_geocode):
        self._post(address="San Isidro")
        user = User.objects.get(username="new.farmer")
        mock_geocode.assert_called_once_with("San Isidro, Libmanan, Camarines Sur, Philippines")
        self.assertEqual(user.address, "San Isidro, Libmanan, Camarines Sur, Philippines")
        self.assertAlmostEqual(float(user.latitude), 13.5)
        self.assertAlmostEqual(float(user.longitude), 123.2)

    @patch("accounts.views.geocode_address", return_value=None)
    def test_signup_with_unresolvable_address_succeeds_with_null_coordinates(self, mock_geocode):
        response = self._post(address="Bagacay")
        self.assertRedirects(response, "/flock/", fetch_redirect_response=False)
        user = User.objects.get(username="new.farmer")
        self.assertEqual(user.address, "Bagacay, Libmanan, Camarines Sur, Philippines")
        self.assertIsNone(user.latitude)
        self.assertIsNone(user.longitude)

    @patch("accounts.views.geocode_address")
    def test_signup_without_address_fails_validation(self, mock_geocode):
        response = self._post(address="")
        self.assertEqual(response.status_code, 200)
        mock_geocode.assert_not_called()
        self.assertFalse(User.objects.filter(username="new.farmer").exists())

    @patch("accounts.views.geocode_address")
    def test_signup_with_address_outside_barangay_list_fails_validation(self, mock_geocode):
        response = self._post(address="Nowhere Land")
        self.assertEqual(response.status_code, 200)
        mock_geocode.assert_not_called()
        self.assertFalse(User.objects.filter(username="new.farmer").exists())


class SignupRateLimitTests(TestCase):
    """Every signup POST runs a real `train_forecast_model` call (see accounts/views.py
    signup docstring) — an anonymous endpoint that trains a model on demand needs a hard
    per-IP cap before it's safe to expose publicly. Covers that the cap (5/day, see
    accounts/views.py signup's @ratelimit decorator) actually blocks the 6th attempt
    rather than just being decorative."""

    def setUp(self):
        cache.clear()

    def _post(self, username):
        # full_name is a single word here, so the generated username equals it.
        return self.client.post(
            "/accounts/signup/",
            {
                "full_name": username,
                "contact": "",
                "password1": "a-strong-passw0rd",
                "password2": "a-strong-passw0rd",
                "address": "San Isidro",
                "privacy_consent": "on",
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
        # templates/registration/password_reset_email.txt) -- pull the 6 digits out.
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
        # The styled HTML alternative carries the same code as the plain-text body.
        html_body, mimetype = mail.outbox[0].alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn(code, html_body)

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

    @patch("accounts.google_oauth.requests.get")
    @patch("accounts.google_oauth.requests.post")
    def test_deactivated_account_cannot_sign_in_via_google_sub_match(self, mock_post, mock_get):
        """A deactivated account (the normal /admin/ "Active" checkbox, not the
        self-service delete_account flow) must not be able to sign back in just
        because it still has a google_sub on file — see google_callback's docstring."""
        deactivated = User.objects.create_user(
            username="banned", email="banned@example.com", google_sub="google-sub-789",
        )
        deactivated.is_active = False
        deactivated.save(update_fields=["is_active"])
        self._mock_google_response(mock_post, mock_get, "google-sub-789", "banned@example.com", True)
        state = self._start_login()

        response = self.client.get(reverse("google_callback"), {"code": "auth-code", "state": state})

        self.assertRedirects(response, reverse("login"))
        self.assertFalse(get_user(self.client).is_authenticated)

    @patch("accounts.google_oauth.requests.get")
    @patch("accounts.google_oauth.requests.post")
    def test_deactivated_account_cannot_sign_in_via_email_match(self, mock_post, mock_get):
        """Same as above, but for the email-linking path (no google_sub on file yet) —
        a deactivated account must not be linkable or signed into either."""
        deactivated = User.objects.create_user(username="banned2", email="banned2@example.com")
        deactivated.is_active = False
        deactivated.save(update_fields=["is_active"])
        self._mock_google_response(mock_post, mock_get, "google-sub-999", "banned2@example.com", True)
        state = self._start_login()

        response = self.client.get(reverse("google_callback"), {"code": "auth-code", "state": state})

        self.assertRedirects(response, reverse("login"))
        self.assertFalse(get_user(self.client).is_authenticated)
        deactivated.refresh_from_db()
        self.assertIsNone(deactivated.google_sub)


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
        # A changed email runs accounts.contact.email_domain_accepts_mail (a real DNS
        # lookup) -- stub it so these tests never touch the network.
        dns_patcher = patch("accounts.forms.email_domain_accepts_mail", return_value=True)
        dns_patcher.start()
        self.addCleanup(dns_patcher.stop)

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


class AccountDeletionTests(TestCase):
    """Covers accounts/views.py's delete_account -- the "Danger Zone" self-service
    soft delete reachable from the full /account/settings/ page (templates/account/
    settings.html), and accounts/forms.py's AccountDeletionForm."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="farmerjuan",
            email="juan@example.com",
            password="a-strong-passw0rd",
            first_name="Juan",
            last_name="Dela Cruz",
            address="San Isidro, Libmanan, Camarines Sur, Philippines",
            latitude=13.5,
            longitude=123.2,
        )
        self.client.force_login(self.user)

    def test_correct_password_deletes_and_logs_out(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=timezone.now().date())
        response = self.client.post(
            reverse("delete_account"), {"confirmation": "a-strong-passw0rd"}
        )
        self.assertRedirects(response, reverse("login"))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(self.user.email, "")
        self.assertEqual(self.user.first_name, "")
        self.assertEqual(self.user.last_name, "")
        self.assertEqual(self.user.address, "")
        self.assertIsNone(self.user.latitude)
        self.assertIsNone(self.user.longitude)
        self.assertFalse(self.user.avatar)
        self.assertFalse(self.user.has_usable_password())
        # Flock/DailyLog history survives -- only the owner's personal info is scrubbed.
        self.assertTrue(Flock.objects.filter(pk=flock.pk, owner=self.user).exists())
        # Logged out: the session no longer carries an authenticated user.
        response = self.client.get(reverse("account_settings"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('account_settings')}")

    def test_wrong_password_is_rejected(self):
        response = self.client.post(reverse("delete_account"), {"confirmation": "not-the-password"})
        self.assertRedirects(response, reverse("account_settings"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.user.email, "juan@example.com")
        # Still logged in.
        self.assertTrue(get_user(self.client).is_authenticated)

    def test_google_only_account_confirms_with_username_instead_of_password(self):
        self.user.set_unusable_password()
        self.user.save()
        # Changing the password invalidates force_login's session auth hash (Django
        # re-checks it on every request) -- re-login so the delete POST below isn't
        # itself bounced to the login page before it ever reaches the view.
        self.client.force_login(self.user)
        response = self.client.post(reverse("delete_account"), {"confirmation": "farmerjuan"})
        self.assertRedirects(response, reverse("login"))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_google_only_account_rejects_wrong_username(self):
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.post(reverse("delete_account"), {"confirmation": "someoneelse"})
        self.assertRedirects(response, reverse("account_settings"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_google_sub_is_cleared_so_google_sign_in_cannot_reactivate_the_account(self):
        self.user.google_sub = "google-sub-123"
        self.user.save()
        self.client.post(reverse("delete_account"), {"confirmation": "a-strong-passw0rd"})
        self.user.refresh_from_db()
        self.assertIsNone(self.user.google_sub)

    def test_foundation_farmer_account_cannot_be_deleted(self):
        self.user.is_foundation_farmer = True
        self.user.save()
        response = self.client.post(
            reverse("delete_account"), {"confirmation": "a-strong-passw0rd"}
        )
        self.assertRedirects(response, reverse("account_settings"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.user.email, "juan@example.com")

    def test_logged_out_visitor_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.post(reverse("delete_account"), {"confirmation": "whatever"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_get_request_is_not_allowed(self):
        response = self.client.get(reverse("delete_account"))
        self.assertEqual(response.status_code, 405)


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


class ClientIpBehindProxyTests(TestCase):
    """Behind Railway's/nginx's reverse proxy REMOTE_ADDR is the proxy itself, so the
    rate limiter must key on the client address the proxy reports instead --
    otherwise the whole world would share one 5-signups-per-day bucket."""

    def setUp(self):
        cache.clear()

    def test_client_ip_uses_last_forwarded_hop_and_ignores_spoofed_entries(self):
        from django.test import RequestFactory

        from accounts.ratelimit import client_ip

        request = RequestFactory().get(
            "/", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.9", REMOTE_ADDR="10.0.0.1"
        )
        self.assertEqual(client_ip(request), "203.0.113.9")

    def test_client_ip_falls_back_to_remote_addr_without_the_header(self):
        from django.test import RequestFactory

        from accounts.ratelimit import client_ip

        request = RequestFactory().get("/", REMOTE_ADDR="198.51.100.7")
        self.assertEqual(client_ip(request), "198.51.100.7")

    @override_settings(RATELIMIT_IP_META_KEY="accounts.ratelimit.client_ip")
    @patch("accounts.views.geocode_address", return_value=None)
    @patch("accounts.views.call_command")
    def test_each_forwarded_client_gets_its_own_signup_bucket(self, mock_call_command, mock_geocode):
        def post(username, forwarded_ip):
            return self.client.post(
                "/accounts/signup/",
                {
                    "username": username,
                    "password1": "a-strong-passw0rd",
                    "password2": "a-strong-passw0rd",
                    "address": "San Isidro",
                },
                REMOTE_ADDR="10.0.0.1",  # the proxy, identical for every request
                HTTP_X_FORWARDED_FOR=forwarded_ip,
            )

        for i in range(5):
            self.assertNotEqual(post(f"proxyfarmer{i}", "203.0.113.1").status_code, 403)
        self.assertEqual(post("proxyfarmer5", "203.0.113.1").status_code, 403)
        # A different real client behind the same proxy is unaffected.
        self.assertNotEqual(post("otherclient", "203.0.113.2").status_code, 403)


class MediaServingTests(TestCase):
    """MEDIA_URL is served by Django's static view even with DEBUG=False (the test
    runner's default), since whitenoise never serves MEDIA_ROOT -- without this route
    every uploaded avatar would 404 in production."""

    def test_uploaded_file_is_served_with_debug_off(self):
        from pathlib import Path

        from django.conf import settings

        avatar_dir = Path(settings.MEDIA_ROOT) / "avatars"
        avatar_dir.mkdir(parents=True, exist_ok=True)
        probe = avatar_dir / "_media_route_probe.png"
        probe.write_bytes(b"probe-bytes")
        try:
            response = self.client.get("/media/avatars/_media_route_probe.png")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"probe-bytes")
        finally:
            probe.unlink(missing_ok=True)

    def test_path_traversal_is_rejected(self):
        response = self.client.get("/media/../settings.py")
        self.assertIn(response.status_code, (400, 404))  # never 200


class BrevoEmailBackendTests(TestCase):
    """accounts.email_backend.BrevoEmailBackend: exercised through Django's real
    send_mail()/EmailMessage with only the HTTP call mocked, so what's asserted is the
    exact request Brevo would receive for the app's two real emails."""

    def _response(self, status=201, text='{"messageId": "<abc@brevo>"}'):
        response = Mock()
        response.status_code = status
        response.text = text
        return response

    def _settings(self):
        return override_settings(
            EMAIL_BACKEND="accounts.email_backend.BrevoEmailBackend",
            BREVO_API_KEY="test-key",
            DEFAULT_FROM_EMAIL="ItikCare Farm <farm@example.com>",
        )

    def test_send_mail_posts_plain_text_message_to_brevo(self):
        from django.core.mail import send_mail

        with self._settings(), patch("accounts.email_backend.requests.post", return_value=self._response()) as post:
            sent = send_mail("ItikCare — reset your password", "Your code is: 123456", None, ["farmer@example.com"])

        self.assertEqual(sent, 1)
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(kwargs["headers"]["api-key"], "test-key")
        self.assertEqual(kwargs["json"], {
            "sender": {"email": "farm@example.com", "name": "ItikCare Farm"},
            "to": [{"email": "farmer@example.com"}],
            "subject": "ItikCare — reset your password",
            "textContent": "Your code is: 123456",
        })
        self.assertGreater(kwargs["timeout"], 0)

    def test_bare_from_address_has_no_name_and_html_alternative_is_sent(self):
        from django.core.mail import EmailMultiAlternatives

        with override_settings(
            EMAIL_BACKEND="accounts.email_backend.BrevoEmailBackend",
            BREVO_API_KEY="test-key",
            DEFAULT_FROM_EMAIL="farm@example.com",
        ), patch("accounts.email_backend.requests.post", return_value=self._response()) as post:
            message = EmailMultiAlternatives("Hi", "plain body", to=["a@example.com", "Bee <b@example.com>"])
            message.attach_alternative("<p>html body</p>", "text/html")
            message.send()

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["sender"], {"email": "farm@example.com"})
        self.assertEqual(payload["to"], [{"email": "a@example.com"}, {"email": "b@example.com", "name": "Bee"}])
        self.assertEqual(payload["textContent"], "plain body")
        self.assertEqual(payload["htmlContent"], "<p>html body</p>")

    def test_error_response_raises_without_leaking_the_api_key(self):
        from django.core.mail import send_mail

        from accounts.email_backend import BrevoAPIError

        bad = self._response(status=400, text='{"message": "Sender is not valid"}')
        with self._settings(), patch("accounts.email_backend.requests.post", return_value=bad):
            with self.assertRaises(BrevoAPIError) as ctx:
                send_mail("s", "b", None, ["farmer@example.com"])
        self.assertIn("400", str(ctx.exception))
        self.assertIn("Sender is not valid", str(ctx.exception))
        self.assertNotIn("test-key", str(ctx.exception))

    def test_fail_silently_swallows_errors_and_reports_zero_sent(self):
        from django.core.mail import send_mail

        with self._settings(), patch(
            "accounts.email_backend.requests.post", side_effect=requests.ConnectionError("down")
        ):
            sent = send_mail("s", "b", None, ["farmer@example.com"], fail_silently=True)
        self.assertEqual(sent, 0)

    def test_network_error_propagates_by_default(self):
        from django.core.mail import send_mail

        with self._settings(), patch(
            "accounts.email_backend.requests.post", side_effect=requests.ConnectionError("down")
        ):
            with self.assertRaises(requests.ConnectionError):
                send_mail("s", "b", None, ["farmer@example.com"])

    def test_missing_api_key_is_a_configuration_error(self):
        from django.core.exceptions import ImproperlyConfigured

        from accounts.email_backend import BrevoEmailBackend

        with override_settings(BREVO_API_KEY=None):
            with self.assertRaises(ImproperlyConfigured):
                BrevoEmailBackend()

    @patch("accounts.views.geocode_address", return_value=None)
    def test_password_reset_code_email_goes_out_through_brevo(self, mock_geocode):
        """End to end through the real reset view: the code a farmer would see in the
        console on localhost is the textContent Brevo is asked to send."""
        cache.clear()
        User.objects.create_user(username="brevofarmer", password="a-strong-passw0rd", email="farmer@example.com")
        with self._settings(), patch("accounts.email_backend.requests.post", return_value=self._response()) as post:
            self.client.post("/accounts/password-reset/", {"username": "brevofarmer"})

        post.assert_called_once()
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["to"], [{"email": "farmer@example.com"}])
        self.assertIn("reset your password", payload["subject"].lower())
        self.assertRegex(payload["textContent"], r"verification code is: \d{6}")


class ContactHelperTests(TestCase):
    """accounts/contact.py -- phone normalizing, splitting the single "Email or phone
    number" signup field, the email-domain check, and username generation."""

    def test_normalize_ph_mobile_accepts_common_formats(self):
        from accounts.contact import normalize_ph_mobile

        for raw in ("09171234567", "0917 123 4567", "0917-123-4567", "+639171234567",
                    "639171234567", " +63 917 123 4567 "):
            self.assertEqual(normalize_ph_mobile(raw), "09171234567", raw)

    def test_normalize_ph_mobile_rejects_non_mobile_numbers(self):
        from accounts.contact import normalize_ph_mobile

        for raw in ("", None, "0917123456", "091712345678", "08171234567", "5551234",
                    "juan@example.com", "0917abc4567"):
            self.assertIsNone(normalize_ph_mobile(raw), raw)

    def test_split_contact_routes_email_and_phone(self):
        from accounts.contact import split_contact

        self.assertEqual(split_contact(""), ("", None))
        self.assertEqual(split_contact("  "), ("", None))
        self.assertEqual(split_contact("0917 123 4567"), ("", "09171234567"))
        self.assertEqual(split_contact("Juan@Gmail.com"), ("juan@gmail.com", None))

    def test_split_contact_rejects_junk(self):
        from django.core.exceptions import ValidationError

        from accounts.contact import split_contact

        for raw in ("juan", "12345", "juan@", "@gmail.com"):
            with self.assertRaises(ValidationError, msg=raw):
                split_contact(raw)

    def _mx(self, *exchanges):
        return [Mock(exchange=exchange) for exchange in exchanges]

    def test_email_domain_with_mx_record_accepts_mail(self):
        from accounts.contact import email_domain_accepts_mail

        with patch("dns.resolver.Resolver.resolve", return_value=self._mx("mx.gmail.com.")):
            self.assertTrue(email_domain_accepts_mail("juan@gmail.com"))

    def test_email_domain_that_does_not_exist_is_rejected(self):
        import dns.resolver

        from accounts.contact import email_domain_accepts_mail

        with patch("dns.resolver.Resolver.resolve", side_effect=dns.resolver.NXDOMAIN):
            self.assertFalse(email_domain_accepts_mail("juan@gmial.com"))

    def test_email_domain_with_null_mx_is_rejected(self):
        from accounts.contact import email_domain_accepts_mail

        with patch("dns.resolver.Resolver.resolve", return_value=self._mx(".")):
            self.assertFalse(email_domain_accepts_mail("juan@example.com"))

    def test_email_domain_with_no_mx_or_a_record_is_rejected(self):
        import dns.resolver

        from accounts.contact import email_domain_accepts_mail

        with patch("dns.resolver.Resolver.resolve", side_effect=dns.resolver.NoAnswer):
            self.assertFalse(email_domain_accepts_mail("juan@parked-domain.test"))

    def test_dns_timeout_never_blocks_signup(self):
        import dns.exception

        from accounts.contact import email_domain_accepts_mail

        with patch("dns.resolver.Resolver.resolve", side_effect=dns.exception.Timeout):
            self.assertTrue(email_domain_accepts_mail("juan@gmail.com"))

    def test_generate_unique_username_from_full_name_adds_suffix_on_collision(self):
        from accounts.contact import generate_unique_username

        self.assertEqual(generate_unique_username("Juan Dela Cruz"), "juan.delacruz")
        User.objects.create_user(username="juan.delacruz", password="x-strong-passw0rd")
        self.assertEqual(generate_unique_username("juan  dela cruz"), "juan.delacruz2")
        self.assertEqual(generate_unique_username("Jhanmae Peñaredondo"), "jhanmae.penaredondo")
        self.assertEqual(generate_unique_username(""), "farmer")


@patch("accounts.views.call_command")
@patch("accounts.views.geocode_address", return_value=None)
@patch("accounts.forms.email_domain_accepts_mail", return_value=True)
class SignupContactAndConsentTests(TestCase):
    """SignupForm's full name / "Email or phone number" / privacy consent fields."""

    def setUp(self):
        cache.clear()

    def _post(self, **overrides):
        data = {
            "full_name": "Juan Dela Cruz",
            "contact": "",
            "password1": "a-strong-passw0rd",
            "password2": "a-strong-passw0rd",
            "address": "San Isidro",
            "privacy_consent": "on",
        }
        data.update(overrides)
        return self.client.post(reverse("signup"), data)

    def test_full_name_and_phone_create_account_with_generated_username(self, *mocks):
        response = self._post(contact="0917 123 4567")
        self.assertRedirects(response, reverse("flock_profile"), fetch_redirect_response=False)
        user = User.objects.get(phone_number="09171234567")
        self.assertEqual(user.username, "juan.delacruz")
        self.assertEqual(user.first_name, "Juan")
        self.assertEqual(user.last_name, "Dela Cruz")
        self.assertEqual(user.email, "")
        self.assertIsNotNone(user.privacy_consented_at)

    def test_email_goes_into_email_field(self, *mocks):
        self._post(contact="Juan@Gmail.com")
        user = User.objects.get(username="juan.delacruz")
        self.assertEqual(user.email, "juan@gmail.com")
        self.assertIsNone(user.phone_number)

    def test_blank_contact_still_creates_account(self, *mocks):
        self._post()
        self._post(full_name="Maria Santos")
        # Two blank phone numbers must not collide on the unique index (stored as NULL).
        self.assertEqual(User.objects.filter(phone_number__isnull=True).count(), 2)
        self.assertEqual(
            User.objects.get(username="juan.delacruz").missing_profile_items, ["email_or_phone"]
        )

    def test_junk_contact_is_rejected(self, *mocks):
        response = self._post(contact="juan")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid email or an 11-digit mobile number")
        self.assertFalse(User.objects.exists())

    def test_email_whose_domain_cannot_receive_mail_is_rejected(self, mock_dns, *mocks):
        mock_dns.return_value = False
        response = self._post(contact="juan@gmial.com")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "doesn&#x27;t seem to exist")
        self.assertFalse(User.objects.exists())

    def test_duplicate_phone_or_email_is_rejected(self, *mocks):
        User.objects.create_user(
            username="taken", password="x-strong-passw0rd",
            email="taken@gmail.com", phone_number="09171234567",
        )
        response = self._post(contact="+63 917 123 4567")
        self.assertContains(response, "mobile number is already in use")
        response = self._post(contact="TAKEN@gmail.com")
        self.assertContains(response, "email is already in use")
        self.assertEqual(User.objects.count(), 1)

    def test_signup_without_privacy_consent_is_rejected(self, *mocks):
        response = self._post(privacy_consent="")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please agree to the Privacy Notice")
        self.assertFalse(User.objects.exists())

    def test_blank_full_name_is_rejected(self, *mocks):
        response = self._post(full_name="   ")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())

    def test_signup_page_labels(self, *mocks):
        response = self.client.get(reverse("signup"))
        self.assertContains(response, "Full name")
        self.assertContains(response, "Email or phone number")
        self.assertNotContains(response, "(optional)")
        self.assertContains(response, reverse("privacy_notice"))


class ForgivingLoginTests(TestCase):
    """accounts/auth_backends.py's full name / first name / phone login paths, all
    case-insensitive (the existing username/email/full-name cases stay covered by
    LoginIdentifierTests above)."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="juan.delacruz",
            password="a-strong-passw0rd",
            first_name="Juan",
            last_name="Dela Cruz",
            phone_number="09171234567",
        )

    def _login(self, identifier, password="a-strong-passw0rd"):
        return self.client.post(reverse("login"), {"username": identifier, "password": password})

    def test_every_identifier_logs_in(self):
        for identifier in ("JUAN DELA CRUZ", "juan  dela cruz", "juan", "Juan",
                           "09171234567", "+639171234567", "0917 123 4567", "Juan.DelaCruz"):
            self.client.logout()
            response = self._login(identifier)
            self.assertRedirects(response, reverse("dashboard"), msg_prefix=identifier)

    def test_first_name_matches_a_multi_word_first_name(self):
        User.objects.create_user(
            username="maria", password="a-strong-passw0rd",
            first_name="Maria Clara", last_name="Santos",
        )
        # Stored first name is "Maria Clara", but typing just "maria" still finds her.
        response = self._login("maria")
        self.assertRedirects(response, reverse("dashboard"))

    def test_shared_first_name_is_ambiguous_but_full_name_still_works(self):
        User.objects.create_user(
            username="juan.santos", password="another-strong-pw2",
            first_name="Juan", last_name="Santos",
        )
        response = self._login("juan")
        self.assertContains(response, "matches more than one account")
        self.assertNotIn("_auth_user_id", self.client.session)

        response = self._login("Juan Dela Cruz")
        self.assertRedirects(response, reverse("dashboard"))

    def test_correct_phone_with_wrong_password_fails(self):
        response = self._login("09171234567", password="wrong-password")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_page_label(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Name, email, or phone number")


class PasswordResetLookupTests(TestCase):
    """Reset step 1 accepts anything the login form does -- signup no longer shows the
    farmer a username (accounts/views.py's request_reset_code)."""

    def setUp(self):
        cache.clear()
        User.objects.create_user(
            username="juan.delacruz", email="juan@gmail.com", password="old-passw0rd",
            first_name="Juan", last_name="Dela Cruz", phone_number="09171234567",
        )

    def test_name_email_or_phone_all_send_the_code(self):
        for identifier in ("Juan Dela Cruz", "juan", "JUAN@gmail.com", "0917 123 4567"):
            mail.outbox.clear()
            cache.clear()
            response = self.client.post(reverse("password_reset"), {"username": identifier})
            self.assertRedirects(response, reverse("password_reset_verify"), msg_prefix=identifier)
            self.assertEqual(len(mail.outbox), 1, identifier)
            self.assertEqual(mail.outbox[0].to, ["juan@gmail.com"])


class AccountSettingsContactTests(TestCase):
    """AccountSettingsForm's separate mobile number field."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="juan.delacruz", password="a-strong-passw0rd", first_name="Juan",
        )
        self.client.force_login(self.user)

    def _post(self, **overrides):
        data = {
            "first_name": "Juan", "last_name": "", "username": "juan.delacruz",
            "email": "", "phone_number": "", "address": "",
        }
        data.update(overrides)
        return self.client.post(reverse("account_settings"), data)

    def test_adding_a_phone_number_normalizes_it(self):
        self._post(phone_number="+63 917 123 4567")
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, "09171234567")

    def test_clearing_the_phone_number_stores_null(self):
        self.user.phone_number = "09171234567"
        self.user.save()
        self._post(phone_number="")
        self.user.refresh_from_db()
        self.assertIsNone(self.user.phone_number)

    def test_invalid_or_taken_phone_is_rejected(self):
        User.objects.create_user(username="other", password="x-strong-passw0rd", phone_number="09998887777")
        response = self._post(phone_number="12345")
        self.assertContains(response, "11-digit mobile number")
        response = self._post(phone_number="09998887777")
        self.assertContains(response, "mobile number is already in use")
        self.user.refresh_from_db()
        self.assertIsNone(self.user.phone_number)

    @patch("accounts.forms.email_domain_accepts_mail", return_value=False)
    def test_new_email_on_a_dead_domain_is_rejected(self, mock_dns):
        response = self._post(email="juan@gmial.com")
        self.assertContains(response, "seem to exist")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")


class MissingProfileItemsTests(TestCase):
    """User.missing_profile_items -- what the notification bell reminds a farmer about."""

    def test_complete_profile_has_nothing_missing(self):
        user = User(
            username="a", first_name="Juan", email="juan@gmail.com", phone_number="09171234567",
            address="San Isidro, Libmanan, Camarines Sur, Philippines",
            privacy_consented_at=timezone.now(),
        )
        self.assertEqual(user.missing_profile_items, [])

    def test_phone_only_still_nudges_for_email(self):
        user = User(
            username="a", first_name="Juan", phone_number="09171234567",
            address="San Isidro, Libmanan, Camarines Sur, Philippines",
            privacy_consented_at=timezone.now(),
        )
        self.assertEqual(user.missing_profile_items, ["email"])

    def test_empty_profile_lists_everything(self):
        user = User(username="a")
        self.assertEqual(
            user.missing_profile_items,
            ["email_or_phone", "full_name", "address", "privacy_consent"],
        )


class PrivacyNoticeTests(TestCase):
    def test_privacy_notice_is_public(self):
        response = self.client.get(reverse("privacy_notice"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data Privacy Act of 2012")
