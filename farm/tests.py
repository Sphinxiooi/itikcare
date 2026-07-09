"""Functional tests for the data logging module (itikcare-spec.md sections 3, 8, 9).

Covers the log_daily_data / farm_records / farm_record_edit views end-to-end,
including the edge cases CLAUDE.md's testing workflow calls out: no active flock,
first-ever entry with no historical data, and out-of-range manual input values.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from .models import DailyLog, DailyLogEdit, Flock

User = get_user_model()

VALID_LOG_POST = {
    "date": "2024-01-01",
    "egg_count": 150,
    "feed_intake_kg": "40.0",
    "flock_age_weeks": 25,
    "temperature_c": "28.0",
    "humidity_pct": "75.0",
}


class LogDailyDataTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    def test_no_active_flock_redirects_with_error(self):
        response = self.client.get("/log-daily-data/", follow=True)
        self.assertRedirects(response, "/")
        messages = list(response.context["messages"])
        self.assertTrue(any("No active flock" in str(m) for m in messages))

    def test_no_active_flock_post_does_not_create_log(self):
        response = self.client.post("/log-daily-data/", VALID_LOG_POST, follow=True)
        self.assertRedirects(response, "/")
        self.assertEqual(DailyLog.objects.count(), 0)

    def test_first_ever_entry_requires_and_saves_an_explicit_flock_size(self):
        """Edge case: first-ever entry for a flock, no historical DailyLog exists yet.

        There's no prior log to carry flock_size forward from, so the form must collect
        it explicitly this one time (forms.DailyLogForm's require_flock_size=True path).
        """
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 240})
        self.assertFalse(response.context and response.context.get("form") and response.context["form"].errors)
        log = DailyLog.objects.get(date=date(2024, 1, 1))
        self.assertEqual(log.flock_size, 240)
        self.assertEqual(log.caging_period, 1)

    def test_first_ever_entry_without_flock_size_is_rejected(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", VALID_LOG_POST)
        self.assertFalse(DailyLog.objects.exists())
        self.assertIn("flock_size", response.context["form"].errors)

    def test_second_entry_carries_forward_flock_size_from_previous_log(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": "2024-01-02"})
        log = DailyLog.objects.get(date=date(2024, 1, 2))
        self.assertEqual(log.flock_size, 240)

    def test_small_gap_continues_the_same_caging_period(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=3,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        # 5-day gap: below the threshold, same caging period continues.
        self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": "2024-01-06"})
        log = DailyLog.objects.get(date=date(2024, 1, 6))
        self.assertEqual(log.caging_period, 3)

    def test_large_gap_starts_a_new_caging_period(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=3,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        # 60-day gap: implies a free-range-then-recage cycle happened in between.
        self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": "2024-03-01"})
        log = DailyLog.objects.get(date=date(2024, 3, 1))
        self.assertEqual(log.caging_period, 4)

    def test_extreme_temperature_above_range_is_rejected(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 240, "temperature_c": "60.0"})
        self.assertEqual(response.status_code, 200)  # re-rendered form, not redirected
        self.assertFalse(DailyLog.objects.exists())
        self.assertIn("temperature_c", response.context["form"].errors)

    def test_extreme_temperature_below_range_is_rejected(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 240, "temperature_c": "-5.0"})
        self.assertFalse(DailyLog.objects.exists())
        self.assertIn("temperature_c", response.context["form"].errors)

    def test_extreme_humidity_above_100_is_rejected(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 240, "humidity_pct": "150.0"})
        self.assertFalse(DailyLog.objects.exists())
        self.assertIn("humidity_pct", response.context["form"].errors)

    def test_extreme_egg_count_above_range_is_rejected(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 240, "egg_count": 5000})
        self.assertFalse(DailyLog.objects.exists())
        self.assertIn("egg_count", response.context["form"].errors)

    def test_zero_egg_count_is_accepted(self):
        """A total-loss day (e.g. severe heat stress) is a valid, if bad, reading."""
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 240, "egg_count": 0})
        self.assertTrue(DailyLog.objects.filter(egg_count=0).exists())

    def test_duplicate_date_for_same_flock_is_rejected(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self.client.post("/log-daily-data/", VALID_LOG_POST)
        self.assertEqual(DailyLog.objects.filter(date=date(2024, 1, 1)).count(), 1)
        self.assertIn("date", response.context["form"].errors)


class FarmRecordsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    def test_no_active_flock_shows_empty_list(self):
        response = self.client.get("/farm-records/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["logs"]), 0)

    def test_only_active_flocks_logs_are_listed(self):
        old_flock = Flock.objects.create(generation_number=1, started_on=date(2023, 1, 1), is_active=False)
        new_flock = Flock.objects.create(generation_number=2, started_on=date(2024, 1, 1), is_active=True)
        DailyLog.objects.create(
            flock=old_flock, date=date(2023, 6, 1), flock_size=200, caging_period=1,
            flock_age_weeks=50, egg_count=140, feed_intake_kg="35.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        DailyLog.objects.create(
            flock=new_flock, date=date(2024, 1, 1), flock_size=240, caging_period=2,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self.client.get("/farm-records/")
        logs = list(response.context["logs"])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].flock, new_flock)


class FarmRecordEditTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        self.log = DailyLog.objects.create(
            flock=self.flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )

    def _edit_post(self, **overrides):
        base = {
            "date": "2024-01-01", "flock_size": 240, "flock_age_weeks": 25,
            "egg_count": 150, "feed_intake_kg": "40.0", "temperature_c": "28.0",
            "humidity_pct": "75.0",
        }
        base.update(overrides)
        return self.client.post(f"/farm-records/{self.log.pk}/edit/", base, follow=True)

    def test_editing_a_field_creates_one_audit_row(self):
        self._edit_post(egg_count=175)
        self.log.refresh_from_db()
        self.assertEqual(self.log.egg_count, 175)
        edits = DailyLogEdit.objects.filter(daily_log=self.log)
        self.assertEqual(edits.count(), 1)
        edit = edits.first()
        self.assertEqual(edit.field_name, "egg_count")
        self.assertEqual(edit.old_value, "150")
        self.assertEqual(edit.new_value, "175")
        self.assertEqual(edit.changed_by, self.user)

    def test_editing_multiple_fields_creates_one_audit_row_per_field(self):
        self._edit_post(egg_count=175, temperature_c="30.0")
        edits = DailyLogEdit.objects.filter(daily_log=self.log)
        self.assertEqual(edits.count(), 2)
        self.assertEqual(set(edits.values_list("field_name", flat=True)), {"egg_count", "temperature_c"})

    def test_resubmitting_unchanged_values_creates_no_audit_rows(self):
        self._edit_post()  # same values as setUp
        self.assertEqual(DailyLogEdit.objects.filter(daily_log=self.log).count(), 0)

    def test_edit_out_of_range_value_is_rejected_and_not_silently_clamped(self):
        response = self._edit_post(temperature_c="99.0")
        self.log.refresh_from_db()
        self.assertEqual(str(self.log.temperature_c), "28.0")
        self.assertEqual(DailyLogEdit.objects.filter(daily_log=self.log).count(), 0)
