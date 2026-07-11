"""Functional tests for the data logging module (itikcare-spec.md sections 3, 8, 9).

Covers the log_daily_data / farm_records / farm_record_edit views end-to-end,
including the edge cases CLAUDE.md's testing workflow calls out: no active flock,
first-ever entry with no historical data, and out-of-range manual input values.
"""

from datetime import date, timedelta
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from forecasting.models import Forecast
from recommendations.models import Recommendation

from .models import DailyLog, DailyLogEdit, Flock
from .weather import fetch_current_weather

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


@override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
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

    def test_free_range_flock_redirects_to_flock_profile_with_error(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        response = self.client.get("/log-daily-data/", follow=True)
        self.assertRedirects(response, "/flock/")
        messages = list(response.context["messages"])
        self.assertTrue(any("free-range" in str(m) for m in messages))

    def test_free_range_flock_post_does_not_create_log(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        response = self.client.post("/log-daily-data/", VALID_LOG_POST, follow=True)
        self.assertRedirects(response, "/flock/")
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

    @patch("farm.views.fetch_current_weather", return_value={"temperature_c": 30.5, "humidity_pct": 82.0})
    def test_get_prefills_temperature_and_humidity_when_weather_fetch_succeeds(self, mock_fetch):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.get("/log-daily-data/")
        form = response.context["form"]
        self.assertEqual(form.initial["temperature_c"], 30.5)
        self.assertEqual(form.initial["humidity_pct"], 82.0)
        self.assertIn("weather", form.fields["temperature_c"].help_text)
        self.assertIn("weather", form.fields["humidity_pct"].help_text)

    @patch("farm.views.fetch_current_weather", return_value=None)
    def test_get_leaves_temperature_and_humidity_blank_when_weather_fetch_fails(self, mock_fetch):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertNotIn("temperature_c", form.initial)
        self.assertNotIn("humidity_pct", form.initial)
        # help_text falls back to DailyLog's model-level default (unrelated to weather)
        # rather than being overwritten -- that only happens when the fetch succeeds.
        self.assertNotIn("weather", form.fields["temperature_c"].help_text.lower())
        self.assertNotIn("weather", form.fields["humidity_pct"].help_text.lower())

    @patch("farm.views.fetch_current_weather")
    def test_post_never_calls_weather_fetch(self, mock_fetch):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        self.client.post("/log-daily-data/", VALID_LOG_POST)
        mock_fetch.assert_not_called()
        log = DailyLog.objects.get(date=date(2024, 1, 1))
        self.assertEqual(str(log.temperature_c), "28.0")
        self.assertEqual(str(log.humidity_pct), "75.0")

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
        response = self.client.get("/farm-records/", {"range": "all"})
        logs = list(response.context["logs"])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].flock, new_flock)

    def test_default_range_shows_only_last_30_days(self):
        today = timezone.localdate()
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), is_active=True)
        DailyLog.objects.create(
            flock=flock, date=today - timedelta(days=10), flock_size=200, caging_period=1,
            flock_age_weeks=50, egg_count=140, feed_intake_kg="35.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        DailyLog.objects.create(
            flock=flock, date=today - timedelta(days=45), flock_size=200, caging_period=1,
            flock_age_weeks=48, egg_count=130, feed_intake_kg="34.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        response = self.client.get("/farm-records/")
        logs = list(response.context["logs"])
        self.assertEqual(len(logs), 1)
        self.assertEqual(response.context["selected_range"], "30")

    def test_range_filter_widens_results(self):
        today = timezone.localdate()
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), is_active=True)
        DailyLog.objects.create(
            flock=flock, date=today - timedelta(days=45), flock_size=200, caging_period=1,
            flock_age_weeks=48, egg_count=130, feed_intake_kg="34.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        response = self.client.get("/farm-records/", {"range": "90"})
        logs = list(response.context["logs"])
        self.assertEqual(len(logs), 1)

    def test_invalid_range_falls_back_to_default(self):
        response = self.client.get("/farm-records/", {"range": "bogus"})
        self.assertEqual(response.context["selected_range"], "30")


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


@override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
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

    @patch("farm.views.trigger_retrain")
    def test_new_generations_first_entry_continues_the_global_caging_period(self, mock_trigger_retrain):
        """A retired flock's first live entry must NOT restart caging_period at 1.

        caging_period is a globally unique segment marker (forecasting/pipeline.py
        groups training data by caging_period alone, with no notion of `flock`), so
        colliding with an earlier flock's period would silently merge two different
        generations' rows into one training segment across the generation-reset gap
        (itikcare-spec.md section 10).
        """
        old_flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=old_flock, date=date(2024, 1, 1), flock_size=240, caging_period=3,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        self.client.post("/flock/retire/", {"started_on": "2024-06-01"})

        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": "2024-06-01"})
        self.assertRedirects(response, "/")
        new_log = DailyLog.objects.get(date=date(2024, 6, 1))
        self.assertEqual(new_log.caging_period, 4)

    def test_toggle_caging_status_flips_a_caged_flock_to_free_range(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        self.assertTrue(flock.is_caged)

        response = self.client.post("/flock/toggle-caging/")
        self.assertRedirects(response, "/flock/")
        flock.refresh_from_db()
        self.assertFalse(flock.is_caged)

    def test_toggle_caging_status_flips_a_free_range_flock_back_to_caged(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), is_caged=False)

        self.client.post("/flock/toggle-caging/")
        flock.refresh_from_db()
        self.assertTrue(flock.is_caged)

    def test_toggle_caging_status_with_no_active_flock_shows_error(self):
        response = self.client.post("/flock/toggle-caging/", follow=True)
        self.assertRedirects(response, "/flock/")
        messages = list(response.context["messages"])
        self.assertTrue(any("No active flock" in str(m) for m in messages))

    def test_toggle_caging_status_rejects_get(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.get("/flock/toggle-caging/")
        self.assertEqual(response.status_code, 405)

    def test_profile_shows_flock_number_label_not_generation(self):
        Flock.objects.create(generation_number=2, started_on=date(2024, 1, 1))
        response = self.client.get("/flock/")
        self.assertContains(response, "Flock #2")
        self.assertNotContains(response, "Generation")

    def test_resume_caging_marks_flock_caged_and_stages_confirmed_count(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        response = self.client.post("/flock/resume-caging/", {"flock_size": 210}, follow=True)
        self.assertRedirects(response, "/flock/")
        flock.refresh_from_db()
        self.assertTrue(flock.is_caged)
        self.assertEqual(flock.pending_flock_size, 210)
        messages = list(response.context["messages"])
        self.assertTrue(any("210 ducks" in str(m) for m in messages))

    def test_resume_caging_with_no_active_flock_shows_error(self):
        response = self.client.post("/flock/resume-caging/", {"flock_size": 210}, follow=True)
        self.assertRedirects(response, "/flock/")
        messages = list(response.context["messages"])
        self.assertTrue(any("No free-range flock" in str(m) for m in messages))

    def test_resume_caging_on_an_already_caged_flock_makes_no_changes(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        self.assertTrue(flock.is_caged)
        response = self.client.post("/flock/resume-caging/", {"flock_size": 210}, follow=True)
        self.assertRedirects(response, "/flock/")
        flock.refresh_from_db()
        self.assertIsNone(flock.pending_flock_size)

    def test_resume_caging_rejects_invalid_flock_size(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        response = self.client.post("/flock/resume-caging/", {"flock_size": 0}, follow=True)
        flock.refresh_from_db()
        self.assertFalse(flock.is_caged)
        self.assertIsNone(flock.pending_flock_size)
        messages = list(response.context["messages"])
        self.assertTrue(messages)

    def test_resume_caging_rejects_get(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        response = self.client.get("/flock/resume-caging/")
        self.assertEqual(response.status_code, 405)


@override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
class ResumeCagingPrefillTests(TestCase):
    """Covers log_daily_data's flock_size prefill/clear interaction with
    Flock.pending_flock_size, set by the resume_caging view."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    def test_get_prefills_flock_size_from_pending_override_instead_of_previous_log(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), pending_flock_size=210)
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.context["form"].initial["flock_size"], 210)

    def test_get_prefills_flock_size_from_pending_override_on_a_first_ever_entry(self):
        Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), pending_flock_size=210)
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.context["form"].initial["flock_size"], 210)

    def test_saving_a_log_clears_the_pending_override(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), pending_flock_size=210)
        self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 210})
        flock.refresh_from_db()
        self.assertIsNone(flock.pending_flock_size)


@override_settings(FARM_LATITUDE=14.1, FARM_LONGITUDE=122.9)
class WeatherFetchTests(TestCase):
    """Covers farm.weather.fetch_current_weather in isolation (no view/DB involvement)."""

    @patch("farm.weather.requests.get")
    def test_successful_fetch_returns_rounded_temperature_and_humidity(self, mock_get):
        mock_get.return_value.json.return_value = {
            "current": {"temperature_2m": 29.34, "relative_humidity_2m": 81.6}
        }
        result = fetch_current_weather()
        self.assertEqual(result, {"temperature_c": 29.3, "humidity_pct": 81.6})

    @override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
    @patch("farm.weather.requests.get")
    def test_missing_coordinates_returns_none_without_calling_the_api(self, mock_get):
        self.assertIsNone(fetch_current_weather())
        mock_get.assert_not_called()

    @patch("farm.weather.requests.get", side_effect=requests.exceptions.Timeout)
    def test_timeout_returns_none(self, mock_get):
        self.assertIsNone(fetch_current_weather())

    @patch("farm.weather.requests.get", side_effect=requests.exceptions.ConnectionError)
    def test_connection_error_returns_none(self, mock_get):
        self.assertIsNone(fetch_current_weather())

    @patch("farm.weather.requests.get")
    def test_malformed_response_returns_none(self, mock_get):
        mock_get.return_value.json.return_value = {}
        self.assertIsNone(fetch_current_weather())
