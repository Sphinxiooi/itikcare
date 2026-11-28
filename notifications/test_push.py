"""Phone notifications (Web Push) and the installable-app files. No test contacts a real push service."""

import base64
import os
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.staticfiles import finders
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from pywebpush import WebPushException

from accounts.models import User
from farm.models import DailyLogReminder, Flock
from farm.services import operational_today

from .management.commands.generate_vapid_keys import new_key_pair
from .models import PushSubscription
from .push import missing_log_payload, send_missing_log_push, send_push

ENDPOINT = "https://fcm.googleapis.com/fcm/send/test-phone"
WITH_KEYS = override_settings(VAPID_PUBLIC_KEY="public-key", VAPID_PRIVATE_KEY="private-key")


def make_farmer(username="pushfarmer", email="push@example.com", **flock_kwargs):
    user = User.objects.create_user(username=username, password="pw12345", email=email)
    Flock.objects.create(owner=user, generation_number=1, started_on=operational_today(), **flock_kwargs)
    return user


def subscribe(user, endpoint=ENDPOINT, language="en"):
    return PushSubscription.objects.create(
        user=user, endpoint=endpoint, p256dh="p256dh", auth="auth", language=language
    )


def gone(status):
    # A real requests Response with a 4xx status is falsy, so mimic that
    response = Mock(status_code=status)
    response.__bool__ = Mock(return_value=False)
    return WebPushException("rejected", response=response)


@WITH_KEYS
class PushSubscriptionViewTests(TestCase):
    def setUp(self):
        self.user = make_farmer()
        self.client.force_login(self.user)

    def post(self, name, **data):
        return self.client.post(reverse(name), data)

    def subscribe_request(self, endpoint=ENDPOINT):
        return self.post("push_subscribe", endpoint=endpoint, p256dh="p256dh", auth="auth")

    def test_subscribe_saves_phone_and_status_reports_it(self):
        self.assertFalse(self.post("push_status", endpoint=ENDPOINT).json()["subscribed"])
        self.assertEqual(self.subscribe_request().status_code, 200)
        self.assertEqual(PushSubscription.objects.get().user, self.user)
        self.assertTrue(self.post("push_status", endpoint=ENDPOINT).json()["subscribed"])

    def test_same_phone_moves_to_the_latest_farmer(self):
        self.subscribe_request()
        other = make_farmer("secondfarmer")
        self.client.force_login(other)
        self.assertFalse(self.post("push_status", endpoint=ENDPOINT).json()["subscribed"])
        self.subscribe_request()
        self.assertEqual(PushSubscription.objects.get().user, other)

    def test_refuses_addresses_that_are_not_a_push_service(self):
        for endpoint in (
            "http://fcm.googleapis.com/fcm/send/x",
            "https://example.com/fcm/send/x",
            "https://fcm.googleapis.com.example.com/x",
            "https://fcm.googleapis.com@127.0.0.1/x",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.subscribe_request(endpoint).status_code, 400)
        self.assertFalse(PushSubscription.objects.exists())

    def test_refuses_missing_keys(self):
        self.assertEqual(self.post("push_subscribe", endpoint=ENDPOINT).status_code, 400)

    @override_settings(VAPID_PRIVATE_KEY="")
    def test_refuses_when_server_has_no_keys(self):
        self.assertEqual(self.subscribe_request().status_code, 503)

    def test_unsubscribe_removes_only_own_phone(self):
        subscribe(make_farmer("secondfarmer"))
        self.post("push_unsubscribe", endpoint=ENDPOINT)
        self.assertEqual(PushSubscription.objects.count(), 1)
        PushSubscription.objects.update(user=self.user)
        self.post("push_unsubscribe", endpoint=ENDPOINT)
        self.assertFalse(PushSubscription.objects.exists())

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.subscribe_request().status_code, 302)
        self.assertFalse(PushSubscription.objects.exists())

    @patch("notifications.push.webpush")
    def test_test_notification_goes_to_this_phone(self, webpush):
        subscribe(self.user)
        self.assertEqual(self.post("push_test", endpoint=ENDPOINT).status_code, 200)
        self.assertIn("Test: ", webpush.call_args.kwargs["data"])

    @patch("notifications.push.webpush")
    def test_cannot_send_a_test_to_someone_elses_phone(self, webpush):
        subscribe(make_farmer("secondfarmer"))
        self.assertEqual(self.post("push_test", endpoint=ENDPOINT).status_code, 400)
        webpush.assert_not_called()

    def test_deleting_account_removes_phones(self):
        subscribe(self.user)
        self.client.post(reverse("delete_account"), {"confirmation": "pw12345"})
        self.assertFalse(PushSubscription.objects.exists())

    def test_settings_page_has_the_switch_and_public_key(self):
        response = self.client.get(reverse("account_settings"))
        self.assertContains(response, 'id="push-toggle"')
        self.assertContains(response, 'data-public-key="public-key"')
        self.assertNotContains(response, "private-key")


class SendPushTests(TestCase):
    def setUp(self):
        self.user = make_farmer()

    def test_real_encryption_and_signing_with_generated_keys(self):
        """Uses the real pywebpush with only the network call replaced."""
        public, private = new_key_pair()
        phone_key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        encode = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")
        subscription = PushSubscription.objects.create(
            user=self.user, endpoint=ENDPOINT, p256dh=encode(phone_key), auth=encode(os.urandom(16))
        )
        with override_settings(VAPID_PUBLIC_KEY=public, VAPID_PRIVATE_KEY=private):
            with patch("requests.post", return_value=Mock(status_code=201)) as post:
                self.assertTrue(send_push(subscription, {"title": "Hello"}))
        self.assertEqual(post.call_args.args[0], ENDPOINT)
        headers = {k.lower(): v for k, v in post.call_args.kwargs["headers"].items()}
        self.assertIn(f"k={public}", headers["authorization"])
        self.assertNotIn(b"Hello", post.call_args.kwargs["data"])

    @WITH_KEYS
    def test_expired_phone_is_deleted(self):
        for status in (404, 410):
            with self.subTest(status=status):
                subscription = subscribe(self.user)
                with patch("notifications.push.webpush", side_effect=gone(status)):
                    self.assertFalse(send_push(subscription, {}))
                self.assertFalse(PushSubscription.objects.exists())

    @WITH_KEYS
    def test_temporary_failure_keeps_the_phone(self):
        subscription = subscribe(self.user)
        for error in (gone(500), ConnectionError("offline")):
            with patch("notifications.push.webpush", side_effect=error), self.assertLogs("notifications.push"):
                self.assertFalse(send_push(subscription, {}))
        self.assertTrue(PushSubscription.objects.exists())

    @override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")
    def test_nothing_sent_without_server_keys(self):
        subscribe(self.user)
        with patch("notifications.push.webpush") as webpush:
            self.assertEqual(send_missing_log_push(self.user, operational_today()), 0)
        webpush.assert_not_called()

    def test_message_uses_the_saved_language(self):
        today = operational_today()
        self.assertEqual(missing_log_payload("en", today)["title"], "Today's log is missing")
        self.assertEqual(missing_log_payload("fil", today)["title"], "Wala pa ang tala ngayong araw")
        self.assertEqual(missing_log_payload("en", today)["url"], reverse("log_daily_data"))


@WITH_KEYS
class ReminderCommandPushTests(TestCase):
    def run_command(self):
        call_command("send_daily_log_reminders", stdout=Mock())

    @patch("notifications.push.webpush")
    def test_sends_email_and_phone_notification_once_a_day(self, webpush):
        subscribe(make_farmer())
        self.run_command()
        self.run_command()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(webpush.call_count, 1)
        reminder = DailyLogReminder.objects.get()
        self.assertTrue(reminder.email_sent)
        self.assertIsNotNone(reminder.push_sent_at)

    @patch("notifications.push.webpush")
    def test_farmer_without_email_still_gets_phone_notification(self, webpush):
        subscribe(make_farmer(email=""))
        self.run_command()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(webpush.call_count, 1)
        self.assertFalse(DailyLogReminder.objects.get().email_sent)

    @patch("notifications.push.webpush")
    def test_failed_email_does_not_block_push_and_is_retried(self, webpush):
        subscribe(make_farmer())
        with patch("farm.management.commands.send_daily_log_reminders.send_mail", side_effect=OSError), \
                self.assertLogs("farm.management.commands.send_daily_log_reminders"):
            self.run_command()
        self.assertFalse(DailyLogReminder.objects.get().email_sent)
        self.run_command()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(webpush.call_count, 1)
        self.assertTrue(DailyLogReminder.objects.get().email_sent)

    def test_failed_push_is_retried_without_a_second_email(self):
        subscribe(make_farmer())
        with patch("notifications.push.webpush", side_effect=gone(500)), self.assertLogs("notifications.push"):
            self.run_command()
        self.assertIsNone(DailyLogReminder.objects.get().push_sent_at)
        with patch("notifications.push.webpush") as webpush:
            self.run_command()
        self.assertEqual(webpush.call_count, 1)
        self.assertEqual(len(mail.outbox), 1)

    @patch("notifications.push.webpush")
    def test_no_notification_for_free_range_flock(self, webpush):
        subscribe(make_farmer(is_caged=False))
        self.run_command()
        webpush.assert_not_called()

    @patch("notifications.push.webpush")
    def test_deactivated_account_is_not_reminded(self, webpush):
        user = make_farmer()
        subscribe(user)
        user.is_active = False
        user.save()
        self.run_command()
        webpush.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)


class InstallableAppTests(TestCase):
    def test_manifest_lists_existing_icons(self):
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        manifest = response.json()
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], reverse("dashboard"))
        for icon in manifest["icons"]:
            self.assertIsNotNone(finders.find(icon["src"].removeprefix("/static/")), icon["src"])

    def test_service_worker_is_served_from_the_root(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response["Content-Type"], "application/javascript")
        self.assertContains(response, "showNotification")
        self.assertContains(response, "badge-96.png")

    def test_public_pages_link_the_manifest(self):
        for name in ("login", "signup"):
            self.assertContains(self.client.get(reverse(name)), "manifest.webmanifest")
