from datetime import date, timedelta

from django.test import Client, TestCase

from accounts.models import User
from farm.models import DailyLog, Flock
from farm.services import operational_today
from forecasting.models import Forecast
from recommendations.models import Recommendation

from .models import NotificationReceipt
from .services import build_notifications, get_notification_state


def make_log(flock, user, log_date, caging_period=1):
    return DailyLog.objects.create(
        flock=flock, date=log_date, flock_size=240, caging_period=caging_period,
        flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
        temperature_c="28.0", humidity_pct="75.0", recorded_by=user,
    )


class NotificationTestBase(TestCase):
    def setUp(self):
        # Fully filled-in profile by default, so the profile notification only shows
        # up in the tests that deliberately blank a field.
        self.user = User.objects.create_user(
            username="farmer1", password="pw12345", first_name="Juan", last_name="Cruz",
            email="juan@example.com", address="Libmanan", latitude="13.69", longitude="123.06",
            avatar="avatars/juan.png",
        )
        self.today = operational_today()

    def keys(self):
        return [n.key.split(":")[0] for n in build_notifications(self.user)]


class NotificationSourceTests(NotificationTestBase):
    def test_no_flock(self):
        self.assertEqual(self.keys(), ["no_flock"])

    def test_log_today_missing_for_caged_flock(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=self.today)
        self.assertIn("log_today_missing", self.keys())

    def test_log_today_missing_cleared_once_logged(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=self.today)
        make_log(flock, self.user, self.today)
        self.assertNotIn("log_today_missing", self.keys())

    def test_free_range_flock_gets_info_not_log_reminder(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=self.today, is_caged=False)
        keys = self.keys()
        self.assertIn("free_range", keys)
        self.assertNotIn("log_today_missing", keys)
        self.assertNotIn("missed_days", keys)

    def test_missed_days_counts_gaps_in_current_stretch(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        for days_ago in (5, 4, 1):  # days 3 and 2 ago are missing
            make_log(flock, self.user, self.today - timedelta(days=days_ago))
        missed = [n for n in build_notifications(self.user) if n.key.startswith("missed_days")]
        self.assertEqual(len(missed), 1)
        self.assertIn("2 recent days", missed[0].title)

    def test_missed_days_ignores_days_before_current_caging_period(self):
        # Caging period 2 began 2 days ago (after a free-range gap): the days before
        # it are an intentional gap (itikcare-spec.md section 10), not missed entries.
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        make_log(flock, self.user, self.today - timedelta(days=60), caging_period=1)
        make_log(flock, self.user, self.today - timedelta(days=2), caging_period=2)
        make_log(flock, self.user, self.today - timedelta(days=1), caging_period=2)
        self.assertNotIn("missed_days", self.keys())

    def test_missed_days_skipped_when_latest_log_is_past_the_gap_threshold(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        make_log(flock, self.user, self.today - timedelta(days=30))
        self.assertNotIn("missed_days", self.keys())

    def test_profile_incomplete_lists_missing_fields(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=self.today, is_caged=False)
        self.user.email = ""
        self.user.latitude = None
        self.user.save()
        profile = [n for n in build_notifications(self.user) if n.key.startswith("profile_incomplete")]
        self.assertEqual(len(profile), 1)
        self.assertIn("email", profile[0].message)
        self.assertIn("farm location", profile[0].message)
        self.assertNotIn("photo", profile[0].message)

    def test_urgent_recommendation_and_forecast_warmup(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        log = make_log(flock, self.user, self.today)
        forecast = Forecast.objects.create(
            flock=flock, forecast_date=self.today, predicted_daily_yield=150,
            predicted_tri_day_yield=450, feature_importances={"temperature_c": 0.4},
            model_version="test",
        )
        forecast.source_logs.add(log)
        Recommendation.objects.create(
            forecast=forecast, triggered_by="temperature_c", message="Add shade.",
            status="High", priority=Recommendation.Priority.HIGH,
        )
        keys = self.keys()
        self.assertIn("urgent_rec", keys)
        # One log so far: the new-farm warm-up notice shows too.
        self.assertIn("warmup", keys)

    def test_calm_recommendation_not_notified(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        log = make_log(flock, self.user, self.today)
        forecast = Forecast.objects.create(
            flock=flock, forecast_date=self.today, predicted_daily_yield=150,
            predicted_tri_day_yield=450, feature_importances={}, model_version="test",
        )
        forecast.source_logs.add(log)
        Recommendation.objects.create(
            forecast=forecast, triggered_by="temperature_c", message="All good.",
            priority=Recommendation.Priority.LOW,
        )
        self.assertNotIn("urgent_rec", self.keys())

    def test_actions_sorted_before_info(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=self.today)
        self.user.avatar = ""
        self.user.save()
        levels = [n.level for n in build_notifications(self.user)]
        self.assertEqual(levels, sorted(levels, key={"action": 0, "warning": 1, "info": 2}.get))


class NotificationReceiptTests(NotificationTestBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    def test_mark_all_read_clears_unread_count(self):
        self.assertEqual(get_notification_state(self.user)[1], 1)  # no_flock
        response = self.client.post("/notifications/read-all/")
        self.assertEqual(response.json(), {"unread_count": 0})
        visible, unread = get_notification_state(self.user)
        self.assertEqual((len(visible), unread), (1, 0))

    def test_dismiss_hides_only_that_key(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=self.today)
        self.user.email = ""
        self.user.save()
        key = f"log_today_missing:{self.today.isoformat()}"
        response = self.client.post("/notifications/dismiss/", {"key": key})
        self.assertEqual(response.status_code, 200)
        visible_keys = [n.key for n in get_notification_state(self.user)[0]]
        self.assertNotIn(key, visible_keys)
        self.assertTrue(any(k.startswith("profile_incomplete") for k in visible_keys))

    def test_yesterdays_dismissal_does_not_hide_today(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=self.today)
        yesterday = self.today - timedelta(days=1)
        NotificationReceipt.objects.create(
            user=self.user, key=f"log_today_missing:{yesterday.isoformat()}", dismissed_at="2026-01-01T00:00Z"
        )
        self.assertIn("log_today_missing", [n.key.split(":")[0] for n in get_notification_state(self.user)[0]])

    def test_views_reject_get_and_anonymous(self):
        self.assertEqual(self.client.get("/notifications/read-all/").status_code, 405)
        anonymous = Client()
        response = anonymous.post("/notifications/dismiss/", {"key": "no_flock"})
        self.assertEqual(response.status_code, 302)

    def test_bell_renders_in_header(self):
        response = self.client.get("/")
        self.assertContains(response, 'id="notif-bell"')
        self.assertContains(response, "Set up your flock")


class ForecastWarmupNotificationTests(NotificationTestBase):
    def test_warmup_notification_clears_after_a_full_week(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        for days_ago in range(5, -1, -1):
            make_log(flock, self.user, self.today - timedelta(days=days_ago))
        warmup = [n for n in build_notifications(self.user) if n.key.startswith("warmup")]
        self.assertEqual(len(warmup), 1)
        self.assertIn("6/7", warmup[0].message)

        make_log(flock, self.user, self.today - timedelta(days=6))
        self.assertNotIn("warmup", self.keys())
