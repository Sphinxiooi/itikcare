"""Functional tests for the data logging module (itikcare-spec.md sections 3, 8, 9).

Covers the log_daily_data / farm_records / farm_record_edit views end-to-end,
including the edge cases CLAUDE.md's testing workflow calls out: no active flock,
first-ever entry with no historical data, and out-of-range manual input values.
"""

from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from forecasting.models import Forecast
from recommendations.models import Recommendation

from .models import DailyLog, DailyLogEdit, Flock

User = get_user_model()

VALID_LOG_POST = {
    "date": "2024-01-01",
    "flock_size": 240,
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

    @patch("farm.views.trigger_retrain")
    def test_first_ever_entry_requires_and_saves_an_explicit_flock_size(self, mock_trigger_retrain):
        """Edge case: first-ever entry for a flock, no historical DailyLog exists yet.

        There's no prior log to pre-fill flock_size from, so the farmer must type it in.
        """
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", VALID_LOG_POST)
        self.assertFalse(response.context and response.context.get("form") and response.context["form"].errors)
        log = DailyLog.objects.get(date=date(2024, 1, 1))
        self.assertEqual(log.flock_size, 240)
        self.assertEqual(log.caging_period, 1)
        # A flock's very first entry has no prior period to close -> no retrain triggered.
        mock_trigger_retrain.assert_not_called()

    def test_first_ever_entry_without_flock_size_is_rejected(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        post_data = {k: v for k, v in VALID_LOG_POST.items() if k != "flock_size"}
        response = self.client.post("/log-daily-data/", post_data)
        self.assertFalse(DailyLog.objects.exists())
        self.assertIn("flock_size", response.context["form"].errors)

    def test_get_prefills_flock_size_from_previous_log(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.context["form"].initial["flock_size"], 240)

    @patch("farm.views.date")
    def test_get_prefills_flock_age_advanced_by_calendar_weeks_since_last_log(self, mock_date):
        """A flock logged at 94 weeks that free-ranges for 6 calendar weeks should be
        pre-filled at 100 weeks on its next entry, not still 94 (itikcare-spec.md
        section 10 — the ducks keep aging during the gap even though nothing is logged)."""
        flock = Flock.objects.create(generation_number=1, started_on=date(2023, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=94, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        mock_date.today.return_value = date(2024, 2, 12)  # exactly 6 weeks (42 days) later
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.context["form"].initial["flock_age_weeks"], 100)

    @patch("farm.views.date")
    def test_get_prefills_flock_age_unchanged_for_a_same_day_entry(self, mock_date):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        mock_date.today.return_value = date(2024, 1, 3)  # 2 days later, under a full week
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.context["form"].initial["flock_age_weeks"], 25)

    def test_lowering_flock_size_on_a_later_entry_records_the_loss(self):
        """A farmer reporting dead/lost ducks just types a smaller flock_size."""
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": "2024-01-02", "flock_size": 238})
        log = DailyLog.objects.get(date=date(2024, 1, 2))
        self.assertEqual(log.flock_size, 238)

    def test_raising_flock_size_on_a_later_entry_records_the_addition(self):
        """A farmer restocking ducks just types a larger flock_size."""
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": "2024-01-02", "flock_size": 250})
        log = DailyLog.objects.get(date=date(2024, 1, 2))
        self.assertEqual(log.flock_size, 250)

    def test_flock_size_out_of_range_on_a_later_entry_is_rejected(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": "2024-01-02", "flock_size": 0})
        self.assertFalse(DailyLog.objects.filter(date=date(2024, 1, 2)).exists())
        self.assertIn("flock_size", response.context["form"].errors)

    @patch("farm.views.trigger_retrain")
    def test_small_gap_continues_the_same_caging_period(self, mock_trigger_retrain):
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
        # No period closed -> no retrain triggered.
        mock_trigger_retrain.assert_not_called()

    @patch("farm.views.trigger_retrain")
    def test_large_gap_starts_a_new_caging_period(self, mock_trigger_retrain):
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
        # The previous caging period just closed -> a retrain is triggered.
        mock_trigger_retrain.assert_called_once_with("caging_period_closed")

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


class FarmRecordDeleteTests(TestCase):
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

    def test_get_shows_confirmation_without_deleting(self):
        response = self.client.get(f"/farm-records/{self.log.pk}/delete/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(DailyLog.objects.filter(pk=self.log.pk).exists())

    def test_post_deletes_the_record(self):
        response = self.client.post(f"/farm-records/{self.log.pk}/delete/", follow=True)
        self.assertRedirects(response, "/farm-records/")
        self.assertFalse(DailyLog.objects.filter(pk=self.log.pk).exists())

    def test_deleting_a_record_also_removes_its_audit_history(self):
        DailyLogEdit.objects.create(
            daily_log=self.log, field_name="egg_count", old_value="150", new_value="175", changed_by=self.user,
        )
        self.client.post(f"/farm-records/{self.log.pk}/delete/")
        self.assertEqual(DailyLogEdit.objects.filter(daily_log_id=self.log.pk).count(), 0)

    def test_deleting_a_record_removes_its_same_date_forecast_and_recommendations(self):
        # generate_forecast always writes forecast_date == the source log's own date
        # (services.py's same-day nowcast), so this mirrors what a real forecast row
        # for self.log would look like without needing a trained model artifact.
        forecast = Forecast.objects.create(
            flock=self.flock, forecast_date=self.log.date, predicted_daily_yield="150.00",
            predicted_tri_day_yield="450.00", feature_importances={}, model_version="test",
        )
        forecast.source_logs.set([self.log])
        Recommendation.objects.create(forecast=forecast, triggered_by="egg_count", message="Test tip.")

        self.client.post(f"/farm-records/{self.log.pk}/delete/")

        self.assertFalse(Forecast.objects.filter(flock=self.flock, forecast_date=self.log.date).exists())
        self.assertEqual(Recommendation.objects.filter(forecast=forecast).count(), 0)

    def test_deleting_a_record_leaves_other_forecasts_that_only_used_it_as_a_prior(self):
        # A Forecast for a *different*, later date may have used self.log as a
        # lag1/roll3 prior via source_logs — that Forecast's own date still has real
        # DailyLog data behind it, so it must survive self.log's deletion.
        later_log = DailyLog.objects.create(
            flock=self.flock, date=date(2024, 1, 2), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=160, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        later_forecast = Forecast.objects.create(
            flock=self.flock, forecast_date=later_log.date, predicted_daily_yield="160.00",
            predicted_tri_day_yield="480.00", feature_importances={}, model_version="test",
        )
        later_forecast.source_logs.set([later_log, self.log])

        self.client.post(f"/farm-records/{self.log.pk}/delete/")

        self.assertTrue(Forecast.objects.filter(pk=later_forecast.pk).exists())


class FlockProfileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    def test_no_active_flock_shows_start_first_flock_form(self):
        response = self.client.get("/flock/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["active_flock"])

    def test_creating_first_flock_sets_generation_one_and_active(self):
        self.client.post("/flock/", {"started_on": "2024-01-01"})
        flock = Flock.objects.get()
        self.assertEqual(flock.generation_number, 1)
        self.assertTrue(flock.is_active)
        self.assertEqual(flock.started_on, date(2024, 1, 1))

    def test_profile_shows_latest_log_size_and_age(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self.client.get("/flock/")
        self.assertEqual(response.context["latest_log"].flock_size, 240)
        self.assertEqual(response.context["latest_log"].flock_age_weeks, 25)

    def test_editing_start_date_updates_the_active_flock(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        self.client.post("/flock/", {"started_on": "2024-02-15"})
        flock = Flock.objects.get()
        self.assertEqual(flock.started_on, date(2024, 2, 15))

    @patch("farm.views.trigger_retrain")
    def test_retiring_a_flock_deactivates_it_and_starts_the_next_generation(self, mock_trigger_retrain):
        old_flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        self.client.post("/flock/retire/", {"started_on": "2024-06-01"})
        old_flock.refresh_from_db()
        self.assertFalse(old_flock.is_active)
        new_flock = Flock.objects.get(is_active=True)
        self.assertEqual(new_flock.generation_number, 2)
        self.assertEqual(new_flock.started_on, date(2024, 6, 1))
        # Retirement closes out a whole generation's data -> a retrain is triggered.
        mock_trigger_retrain.assert_called_once_with("flock_retired")

    def test_first_entry_after_retirement_requires_flock_size_again(self):
        old_flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=old_flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        self.client.post("/flock/retire/", {"started_on": "2024-06-01"})

        # No previous DailyLog for the new flock, so flock_size isn't pre-filled.
        response = self.client.get("/log-daily-data/")
        self.assertNotIn("flock_size", response.context["form"].initial)

        post_data = {k: v for k, v in VALID_LOG_POST.items() if k != "flock_size"}
        response = self.client.post("/log-daily-data/", {**post_data, "date": "2024-06-01"})
        self.assertFalse(DailyLog.objects.filter(date=date(2024, 6, 1)).exists())
        self.assertIn("flock_size", response.context["form"].errors)
