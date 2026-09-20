"""Functional tests for the data logging module (itikcare-spec.md sections 3, 8, 9).

Covers the log_daily_data / farm_records / farm_record_edit views end-to-end,
including the edge cases CLAUDE.md's testing workflow calls out: no active flock,
first-ever entry with no historical data, and out-of-range manual input values.
"""

import json
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from forecasting.models import Forecast
from recommendations.models import Recommendation

from .models import DailyLog, DailyLogEdit, DailyLogReminder, Flock
from .services import (
    FARM_DAY_START_HOUR,
    RECORDS_CHART_MIN_LOGS,
    assign_caging_periods,
    build_next_day_forecasts,
    build_records_chart_data,
    build_records_summary,
    build_trend_chart_data,
    get_effective_coordinates,
    operational_today,
    resolve_trend_range,
)
from .weather import fetch_current_weather, fetch_historical_weather, geocode_address

User = get_user_model()

VALID_LOG_POST = {
    "date": "2024-01-01",
    "flock_size": 240,
    "egg_count": 150,
    "feed_intake_kg": "40.0",
    "flock_age_weeks": 25,
    "temperature_c": "28.0",
    "humidity_pct": "75.0",
    # log_daily_data never saves on a first submission (see LogDailyDataConfirmationTests
    # below for that flow in isolation) — most tests in this file only care about the
    # eventual save, so this fixture is pre-confirmed to reach it in one POST.
    "confirmed": "1",
}


@override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
class LogDailyDataTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    def test_no_active_flock_redirects_with_error(self):
        response = self.client.get("/log-daily-data/", follow=True)
        self.assertRedirects(response, "/flock/")
        messages = list(response.context["messages"])
        self.assertTrue(any("No active flock" in str(m) for m in messages))

    def test_no_active_flock_post_does_not_create_log(self):
        response = self.client.post("/log-daily-data/", VALID_LOG_POST, follow=True)
        self.assertRedirects(response, "/flock/")
        self.assertEqual(DailyLog.objects.count(), 0)

    def test_free_range_flock_redirects_to_flock_profile_with_error(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        response = self.client.get("/log-daily-data/", follow=True)
        self.assertRedirects(response, "/flock/")
        messages = list(response.context["messages"])
        self.assertTrue(any("free-range" in str(m) for m in messages))

    def test_free_range_flock_post_does_not_create_log(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        response = self.client.post("/log-daily-data/", VALID_LOG_POST, follow=True)
        self.assertRedirects(response, "/flock/")
        self.assertEqual(DailyLog.objects.count(), 0)

    @patch("farm.views.trigger_retrain")
    def test_first_ever_entry_requires_and_saves_an_explicit_flock_size(self, mock_trigger_retrain):
        """Edge case: first-ever entry for a flock, no historical DailyLog exists yet.

        There's no prior log to pre-fill flock_size from, so the farmer must type it in.
        """
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", VALID_LOG_POST)
        self.assertFalse(response.context and response.context.get("form") and response.context["form"].errors)
        log = DailyLog.objects.get(date=date(2024, 1, 1))
        self.assertEqual(log.flock_size, 240)
        self.assertEqual(log.caging_period, 1)
        # A flock's very first entry has no prior period to close -> no retrain triggered.
        mock_trigger_retrain.assert_not_called()

    def test_first_ever_entry_without_flock_size_is_rejected(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        post_data = {k: v for k, v in VALID_LOG_POST.items() if k != "flock_size"}
        response = self.client.post("/log-daily-data/", post_data)
        self.assertFalse(DailyLog.objects.exists())
        self.assertIn("flock_size", response.context["form"].errors)

    def test_get_prefills_flock_size_from_previous_log(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.context["form"].initial["flock_size"], 240)

    def test_get_never_prefills_temperature_and_humidity(self):
        # Temperature and humidity are always entered by the farmer from their own
        # thermometer/hygrometer reading -- the form leaves them blank.
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertNotIn("temperature_c", form.initial)
        self.assertNotIn("humidity_pct", form.initial)

    @patch("farm.services.timezone")
    def test_get_prefills_flock_age_advanced_by_calendar_weeks_since_last_log(self, mock_timezone):
        """A flock logged at 94 weeks that free-ranges for 6 calendar weeks should be
        pre-filled at 100 weeks on its next entry, not still 94 (itikcare-spec.md
        section 10 — the ducks keep aging during the gap even though nothing is logged)."""
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2023, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=94, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        mock_timezone.localdate.return_value = date(2024, 2, 12)  # exactly 6 weeks (42 days) later
        mock_timezone.localtime.return_value = datetime(2024, 2, 12, 12, 0)  # same day, past the 6am rollover
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.context["form"].initial["flock_age_weeks"], 100)

    @patch("farm.services.timezone")
    def test_get_prefills_flock_age_unchanged_for_a_same_day_entry(self, mock_timezone):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        mock_timezone.localdate.return_value = date(2024, 1, 3)  # 2 days later, under a full week
        mock_timezone.localtime.return_value = datetime(2024, 1, 3, 12, 0)  # same day, past the 6am rollover
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.context["form"].initial["flock_age_weeks"], 25)

    def test_lowering_flock_size_on_a_later_entry_records_the_loss(self):
        """A farmer reporting dead/lost ducks just types a smaller flock_size."""
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
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
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": "2024-01-02", "flock_size": 250})
        log = DailyLog.objects.get(date=date(2024, 1, 2))
        self.assertEqual(log.flock_size, 250)

    def test_flock_size_out_of_range_on_a_later_entry_is_rejected(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
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
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
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
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
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
        mock_trigger_retrain.assert_called_once_with("caging_period_closed", self.user.id)

    def test_extreme_temperature_above_range_is_rejected(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 240, "temperature_c": "60.0"})
        self.assertEqual(response.status_code, 200)  # re-rendered form, not redirected
        self.assertFalse(DailyLog.objects.exists())
        self.assertIn("temperature_c", response.context["form"].errors)

    def test_extreme_temperature_below_range_is_rejected(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 240, "temperature_c": "-5.0"})
        self.assertFalse(DailyLog.objects.exists())
        self.assertIn("temperature_c", response.context["form"].errors)

    def test_extreme_humidity_above_100_is_rejected(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 240, "humidity_pct": "150.0"})
        self.assertFalse(DailyLog.objects.exists())
        self.assertIn("humidity_pct", response.context["form"].errors)

    def test_extreme_egg_count_above_range_is_rejected(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 240, "egg_count": 5000})
        self.assertFalse(DailyLog.objects.exists())
        self.assertIn("egg_count", response.context["form"].errors)

    def test_zero_egg_count_is_accepted(self):
        """A total-loss day (e.g. severe heat stress) is a valid, if bad, reading."""
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        self.client.post("/log-daily-data/", {**VALID_LOG_POST, "flock_size": 240, "egg_count": 0})
        self.assertTrue(DailyLog.objects.filter(egg_count=0).exists())

    def test_duplicate_date_for_same_flock_is_rejected(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self.client.post("/log-daily-data/", VALID_LOG_POST)
        self.assertEqual(DailyLog.objects.filter(date=date(2024, 1, 1)).count(), 1)
        self.assertIn("date", response.context["form"].errors)

    def test_future_date_is_rejected(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        # operational_today(), not timezone.localdate(): the logging day rolls over at
        # 6am, not midnight, so this must use the same "today" the view itself checks
        # against, or this test would be wall-clock-flaky in the midnight-6am window.
        tomorrow = operational_today() + timedelta(days=1)
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": tomorrow.isoformat()})
        self.assertFalse(DailyLog.objects.filter(date=tomorrow).exists())
        self.assertIn("date", response.context["form"].errors)

    def test_todays_date_is_accepted(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        today = operational_today()
        self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": today.isoformat()})
        self.assertTrue(DailyLog.objects.filter(date=today).exists())

    @patch("farm.views.generate_forecast")
    def test_todays_entry_generates_forecast(self, mock_generate_forecast):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        today = operational_today()
        self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": today.isoformat()})
        log = DailyLog.objects.get(date=today)
        mock_generate_forecast.assert_called_once_with(log)

    @patch("farm.views.generate_forecast")
    def test_backdated_entry_skips_forecast_generation(self, mock_generate_forecast):
        """A backfilled missed day (VALID_LOG_POST's date is always in the past relative
        to whenever these tests run) must still save as ordinary history, but must never
        generate its own same-day Forecast -- see log_daily_data's docstring."""
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", VALID_LOG_POST, follow=True)
        self.assertTrue(DailyLog.objects.filter(date=date(2024, 1, 1)).exists())
        mock_generate_forecast.assert_not_called()
        messages = list(response.context["messages"])
        self.assertTrue(any("won't generate its own forecast" in str(m) for m in messages))

    def test_date_before_flock_started_is_rejected(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": "2023-12-31"})
        self.assertFalse(DailyLog.objects.filter(date=date(2023, 12, 31)).exists())
        self.assertIn("date", response.context["form"].errors)


@override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
class LogDailyDataConfirmationTests(TestCase):
    """Covers log_daily_data's two-step confirm flow and anomaly warnings in isolation
    (LogDailyDataTests above always posts VALID_LOG_POST's baked-in confirmed=1, which
    skips straight past this)."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))

    def _unconfirmed_post(self, **overrides):
        post_data = {k: v for k, v in VALID_LOG_POST.items() if k != "confirmed"}
        post_data.update(overrides)
        return self.client.post("/log-daily-data/", post_data)

    def test_first_submission_shows_confirmation_screen_without_saving(self):
        response = self._unconfirmed_post()
        self.assertFalse(DailyLog.objects.exists())
        self.assertTrue(response.context["confirm_mode"])
        self.assertEqual(response.context["anomaly_warnings"], [])

    def test_confirmed_resubmission_saves(self):
        self._unconfirmed_post()
        self.assertFalse(DailyLog.objects.exists())
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "confirmed": "1"})
        self.assertRedirects(response, "/")
        self.assertTrue(DailyLog.objects.filter(date=date(2024, 1, 1)).exists())

    def test_edit_click_returns_to_editable_form_without_saving(self):
        response = self.client.post(
            "/log-daily-data/", {**VALID_LOG_POST, "confirmed": "1", "edit": "1", "egg_count": 999}
        )
        self.assertFalse(DailyLog.objects.exists())
        self.assertNotIn("confirm_mode", response.context or {})
        self.assertEqual(response.context["form"]["egg_count"].value(), "999")

    def _log_history(self, n, egg_count, flock_size=240):
        for i in range(n):
            DailyLog.objects.create(
                flock=self.flock, date=date(2024, 1, 1) + timedelta(days=i), flock_size=flock_size, caging_period=1,
                flock_age_weeks=25, egg_count=egg_count, feed_intake_kg="40.0",
                temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
            )

    def test_egg_count_far_above_flocks_average_is_flagged(self):
        self._log_history(5, egg_count=350)
        response = self._unconfirmed_post(date="2024-01-10", egg_count=700)
        self.assertFalse(DailyLog.objects.filter(date=date(2024, 1, 10)).exists())
        self.assertTrue(response.context["confirm_mode"])
        warnings = response.context["anomaly_warnings"]
        self.assertTrue(any("Egg count" in w for w in warnings))

    def test_confirming_past_an_anomaly_warning_saves_anyway(self):
        self._log_history(5, egg_count=350)
        response = self.client.post(
            "/log-daily-data/", {**VALID_LOG_POST, "date": "2024-01-10", "egg_count": 700, "confirmed": "1"}
        )
        self.assertRedirects(response, "/")
        self.assertTrue(DailyLog.objects.filter(date=date(2024, 1, 10), egg_count=700).exists())

    def test_typical_value_with_enough_history_is_not_flagged(self):
        self._log_history(5, egg_count=350, flock_size=400)
        response = self._unconfirmed_post(date="2024-01-10", egg_count=360, flock_size=400)
        self.assertEqual(response.context["anomaly_warnings"], [])

    def test_fewer_than_minimum_history_never_flags(self):
        self._log_history(3, egg_count=350)
        response = self._unconfirmed_post(date="2024-01-10", egg_count=700, flock_size=800)
        self.assertEqual(response.context["anomaly_warnings"], [])

    def test_egg_count_above_flock_size_is_flagged_even_without_history(self):
        response = self._unconfirmed_post(date="2024-01-10", egg_count=500, flock_size=400)
        self.assertFalse(DailyLog.objects.filter(date=date(2024, 1, 10)).exists())
        warnings = response.context["anomaly_warnings"]
        self.assertTrue(any("more eggs than ducks" in w for w in warnings))

    def test_egg_count_equal_to_flock_size_is_not_flagged(self):
        response = self._unconfirmed_post(date="2024-01-10", egg_count=400, flock_size=400)
        self.assertEqual(response.context["anomaly_warnings"], [])


class WeatherForDateViewTests(TestCase):
    """Covers the weather_for_date JSON endpoint backing log_daily_data.html's
    date-change auto-fill (see farm.weather.fetch_historical_weather's own tests for
    the archive/forecast fallback logic that lives behind this view)."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()

    def test_unauthenticated_request_redirects_to_login(self):
        response = self.client.get("/weather-for-date/?date=2024-01-01")
        self.assertRedirects(response, "/accounts/login/?next=/weather-for-date/%3Fdate%3D2024-01-01")

    @patch("farm.views.fetch_historical_weather")
    def test_past_date_returns_the_fetched_weather_as_json(self, mock_fetch):
        # self.user has no latitude/longitude of their own (accounts.User's
        # latitude/longitude, captured at signup, is left unset here) -- so
        # get_effective_coordinates falls through to (None, None), same as any
        # pre-existing farmer without a saved location; fetch_historical_weather
        # itself is the layer that then falls back to settings.FARM_LATITUDE/
        # FARM_LONGITUDE, which this test isn't exercising since fetch itself is mocked.
        self.client.login(username="farmer1", password="pw12345")
        mock_fetch.return_value = {"temperature_c": 29.5, "humidity_pct": 81.0}
        past_date = date.today() - timedelta(days=5)
        response = self.client.get(f"/weather-for-date/?date={past_date.isoformat()}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"temperature_c": 29.5, "humidity_pct": 81.0})
        mock_fetch.assert_called_once_with(past_date, None, None)

    @patch("farm.weather.requests.get")
    def test_today_returns_null_without_calling_the_api(self, mock_get):
        # Today's own "no lookup" rule lives inside fetch_historical_weather (see its
        # tests), not as separate view-level logic -- this exercises the real function
        # end-to-end through the view instead of stubbing it out, to confirm the view
        # doesn't add its own (redundant, and possibly inconsistent) date check.
        self.client.login(username="farmer1", password="pw12345")
        response = self.client.get(f"/weather-for-date/?date={date.today().isoformat()}")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(json.loads(response.content))
        mock_get.assert_not_called()

    @patch("farm.views.fetch_historical_weather")
    def test_missing_date_returns_null_without_calling_fetch(self, mock_fetch):
        self.client.login(username="farmer1", password="pw12345")
        response = self.client.get("/weather-for-date/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(json.loads(response.content))
        mock_fetch.assert_not_called()

    @patch("farm.views.fetch_historical_weather")
    def test_malformed_date_returns_null_without_calling_fetch(self, mock_fetch):
        self.client.login(username="farmer1", password="pw12345")
        response = self.client.get("/weather-for-date/?date=not-a-date")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(json.loads(response.content))
        mock_fetch.assert_not_called()

    @patch("farm.views.fetch_historical_weather", return_value=None)
    def test_lookup_miss_returns_null(self, mock_fetch):
        self.client.login(username="farmer1", password="pw12345")
        past_date = date.today() - timedelta(days=5)
        response = self.client.get(f"/weather-for-date/?date={past_date.isoformat()}")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(json.loads(response.content))


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
        old_flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2023, 1, 1), is_active=False)
        new_flock = Flock.objects.create(owner=self.user, generation_number=2, started_on=date(2024, 1, 1), is_active=True)
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

    def test_default_shows_all_months(self):
        today = timezone.localdate()
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_active=True)
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
        self.assertEqual(len(logs), 2)
        self.assertEqual(response.context["selected_month"], "all")

    def test_locked_record_shows_locked_indicator_instead_of_edit_link(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_active=True)
        log = DailyLog.objects.create(
            flock=flock, date=timezone.localdate(), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user, is_locked=True,
        )
        response = self.client.get("/farm-records/")
        self.assertContains(response, "Locked")
        self.assertNotContains(response, f"/farm-records/{log.pk}/edit/")

    def test_flock_dropdown_lists_all_owners_flocks_most_recent_first(self):
        old_flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2023, 1, 1), is_active=False)
        new_flock = Flock.objects.create(owner=self.user, generation_number=2, started_on=date(2024, 1, 1), is_active=True)
        response = self.client.get("/farm-records/")
        self.assertEqual(
            list(response.context["flock_choices"].keys()),
            [str(new_flock.id), str(old_flock.id)],
        )

    def test_falls_back_to_most_recent_flock_when_none_active(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2023, 1, 1), is_active=False)
        newer_flock = Flock.objects.create(owner=self.user, generation_number=2, started_on=date(2024, 1, 1), is_active=False)
        DailyLog.objects.create(
            flock=newer_flock, date=date(2024, 1, 2), flock_size=200, caging_period=1,
            flock_age_weeks=50, egg_count=140, feed_intake_kg="35.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        response = self.client.get("/farm-records/")
        self.assertEqual(response.context["selected_flock_id"], str(newer_flock.id))
        self.assertEqual(len(response.context["logs"]), 1)

    def test_flock_query_param_switches_displayed_flock(self):
        old_flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2023, 1, 1), is_active=False)
        Flock.objects.create(owner=self.user, generation_number=2, started_on=date(2024, 1, 1), is_active=True)
        DailyLog.objects.create(
            flock=old_flock, date=date(2023, 6, 1), flock_size=200, caging_period=1,
            flock_age_weeks=50, egg_count=140, feed_intake_kg="35.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        response = self.client.get("/farm-records/", {"flock": old_flock.id})
        logs = list(response.context["logs"])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].flock, old_flock)

    def test_flock_query_param_for_another_owner_is_ignored(self):
        other_user = User.objects.create_user(username="farmer2", password="pw12345")
        other_flock = Flock.objects.create(owner=other_user, generation_number=1, started_on=date(2024, 1, 1), is_active=True)
        response = self.client.get("/farm-records/", {"flock": other_flock.id})
        self.assertNotEqual(response.context["selected_flock_id"], str(other_flock.id))
        self.assertEqual(len(response.context["logs"]), 0)

    def test_month_dropdown_options_are_scoped_to_selected_flock(self):
        flock_a = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2023, 1, 1), is_active=False)
        flock_b = Flock.objects.create(owner=self.user, generation_number=2, started_on=date(2024, 1, 1), is_active=True)
        DailyLog.objects.create(
            flock=flock_a, date=date(2023, 6, 1), flock_size=200, caging_period=1,
            flock_age_weeks=50, egg_count=140, feed_intake_kg="35.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        DailyLog.objects.create(
            flock=flock_b, date=timezone.localdate(), flock_size=200, caging_period=2,
            flock_age_weeks=50, egg_count=140, feed_intake_kg="35.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        response_a = self.client.get("/farm-records/", {"flock": flock_a.id})
        response_b = self.client.get("/farm-records/", {"flock": flock_b.id})
        self.assertEqual(list(response_a.context["month_choices"].keys()), ["all", "2023-06"])
        self.assertEqual(
            list(response_b.context["month_choices"].keys()),
            ["all", timezone.localdate().strftime("%Y-%m")],
        )

    def test_month_query_param_narrows_results(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_active=True)
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=200, caging_period=1,
            flock_age_weeks=50, egg_count=140, feed_intake_kg="35.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        DailyLog.objects.create(
            flock=flock, date=date(2024, 3, 1), flock_size=200, caging_period=2,
            flock_age_weeks=55, egg_count=145, feed_intake_kg="36.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        response = self.client.get("/farm-records/", {"month": "2024-01"})
        logs = list(response.context["logs"])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].date, date(2024, 1, 1))

    def test_invalid_month_falls_back_to_all(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_active=True)
        response = self.client.get("/farm-records/", {"month": "bogus"})
        self.assertEqual(response.context["selected_month"], "all")

    def test_switching_flock_resets_stale_month_to_all(self):
        flock_a = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2023, 1, 1), is_active=False)
        flock_b = Flock.objects.create(owner=self.user, generation_number=2, started_on=date(2024, 1, 1), is_active=True)
        DailyLog.objects.create(
            flock=flock_a, date=date(2023, 6, 1), flock_size=200, caging_period=1,
            flock_age_weeks=50, egg_count=140, feed_intake_kg="35.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        DailyLog.objects.create(
            flock=flock_b, date=timezone.localdate(), flock_size=200, caging_period=2,
            flock_age_weeks=50, egg_count=140, feed_intake_kg="35.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        # 2023-06 is only valid for flock_a; requesting it against flock_b should fall back to "all".
        response = self.client.get("/farm-records/", {"flock": flock_b.id, "month": "2023-06"})
        self.assertEqual(response.context["selected_month"], "all")
        self.assertEqual(len(response.context["logs"]), 1)

    def test_flock_dropdown_labels_use_flock_number_not_generation(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_active=True)
        response = self.client.get("/farm-records/")
        self.assertEqual(response.context["flock_choices"][str(flock.id)], "Flock #1 (active)")

    def test_retired_flocks_records_are_visible_but_show_read_only_not_edit_link(self):
        old_flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2023, 1, 1), is_active=False)
        log = DailyLog.objects.create(
            flock=old_flock, date=date(2023, 6, 1), flock_size=200, caging_period=1,
            flock_age_weeks=50, egg_count=140, feed_intake_kg="35.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        response = self.client.get("/farm-records/", {"flock": old_flock.id})
        self.assertContains(response, "Read-only")
        self.assertNotContains(response, f"/farm-records/{log.pk}/edit/")


class FarmRecordEditTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
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
        self._edit_post(temperature_c="99.0")
        self.log.refresh_from_db()
        self.assertEqual(str(self.log.temperature_c), "28.0")
        self.assertEqual(DailyLogEdit.objects.filter(daily_log=self.log).count(), 0)

    def test_edit_future_date_is_rejected(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        self._edit_post(date=tomorrow.isoformat())
        self.log.refresh_from_db()
        self.assertEqual(self.log.date, date(2024, 1, 1))
        self.assertEqual(DailyLogEdit.objects.filter(daily_log=self.log).count(), 0)

    def test_locked_record_get_redirects_with_error(self):
        self.log.is_locked = True
        self.log.save(update_fields=["is_locked"])
        response = self.client.get(f"/farm-records/{self.log.pk}/edit/", follow=True)
        self.assertRedirects(response, "/farm-records/")
        self.assertContains(response, "can no longer be edited or deleted")

    def test_locked_record_post_is_blocked(self):
        self.log.is_locked = True
        self.log.save(update_fields=["is_locked"])
        self._edit_post(egg_count=999)
        self.log.refresh_from_db()
        self.assertEqual(self.log.egg_count, 150)
        self.assertEqual(DailyLogEdit.objects.filter(daily_log=self.log).count(), 0)

    def test_retired_flock_record_get_redirects_with_error(self):
        self.flock.is_active = False
        self.flock.save(update_fields=["is_active"])
        response = self.client.get(f"/farm-records/{self.log.pk}/edit/", follow=True)
        self.assertRedirects(response, "/farm-records/")
        self.assertContains(response, "retired flock")

    def test_retired_flock_record_post_is_blocked(self):
        self.flock.is_active = False
        self.flock.save(update_fields=["is_active"])
        self._edit_post(egg_count=999)
        self.log.refresh_from_db()
        self.assertEqual(self.log.egg_count, 150)
        self.assertEqual(DailyLogEdit.objects.filter(daily_log=self.log).count(), 0)

    def test_editing_date_onto_an_existing_record_is_rejected_cleanly(self):
        """Moving a record's date onto a day this flock already has a log for must
        surface as a form error, not a 500 from the unique (flock, date) constraint."""
        DailyLog.objects.create(
            flock=self.flock, date=date(2024, 1, 2), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=160, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self._edit_post(date="2024-01-02")
        self.assertEqual(response.status_code, 200)
        self.assertIn("date", response.context["form"].errors)
        self.log.refresh_from_db()
        self.assertEqual(self.log.date, date(2024, 1, 1))
        self.assertEqual(DailyLogEdit.objects.filter(daily_log=self.log).count(), 0)

    def test_editing_date_across_a_gap_recomputes_caging_period(self):
        """If an edit moves a date across a >CAGING_PERIOD_GAP_DAYS gap, the stored
        caging_period (derived from the gap to the previous log) is recomputed to match."""
        DailyLog.objects.create(
            flock=self.flock, date=date(2024, 1, 3), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=160, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        # self.log sits at 2024-01-01 in caging_period 1. Move it 40 days out, well past
        # the 14-day boundary from the 2024-01-03 log, so it lands in a new period.
        self._edit_post(date="2024-02-12")
        self.log.refresh_from_db()
        self.assertEqual(self.log.date, date(2024, 2, 12))
        self.assertEqual(self.log.caging_period, 2)


class FarmRecordDeleteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
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

    def test_locked_record_get_redirects_with_error(self):
        self.log.is_locked = True
        self.log.save(update_fields=["is_locked"])
        response = self.client.get(f"/farm-records/{self.log.pk}/delete/", follow=True)
        self.assertRedirects(response, "/farm-records/")
        self.assertTrue(DailyLog.objects.filter(pk=self.log.pk).exists())

    def test_locked_record_post_is_blocked(self):
        self.log.is_locked = True
        self.log.save(update_fields=["is_locked"])
        self.client.post(f"/farm-records/{self.log.pk}/delete/")
        self.assertTrue(DailyLog.objects.filter(pk=self.log.pk).exists())

    def test_retired_flock_record_get_redirects_with_error(self):
        self.flock.is_active = False
        self.flock.save(update_fields=["is_active"])
        response = self.client.get(f"/farm-records/{self.log.pk}/delete/", follow=True)
        self.assertRedirects(response, "/farm-records/")
        self.assertTrue(DailyLog.objects.filter(pk=self.log.pk).exists())

    def test_retired_flock_record_post_is_blocked(self):
        self.flock.is_active = False
        self.flock.save(update_fields=["is_active"])
        self.client.post(f"/farm-records/{self.log.pk}/delete/")
        self.assertTrue(DailyLog.objects.filter(pk=self.log.pk).exists())

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
        self.client.post("/flock/", {
            "started_on": date.today().isoformat(),
            "flock_size": 240, "flock_age_weeks": 25, "feed_intake_kg": "40.0",
        })
        flock = Flock.objects.get()
        self.assertEqual(flock.generation_number, 1)
        self.assertTrue(flock.is_active)
        self.assertEqual(flock.started_on, date.today())
        self.assertEqual(flock.pending_flock_size, 240)
        self.assertEqual(flock.pending_flock_age_weeks, 25)
        self.assertEqual(str(flock.pending_feed_intake_kg), "40.00")

    def test_creating_flock_can_be_backdated_to_before_today(self):
        """A farmer who's already been raising this flock for a while and is only
        just starting to use the app should be able to record its real start date,
        not be forced to register it as if it started today."""
        started_on = date.today() - timedelta(days=7)
        self.client.post("/flock/", {
            "started_on": started_on.isoformat(),
            "flock_size": 240, "flock_age_weeks": 25, "feed_intake_kg": "40.0",
        })
        flock = Flock.objects.get()
        self.assertEqual(flock.started_on, started_on)

    def test_creating_flock_rejects_future_start_date(self):
        response = self.client.post("/flock/", {
            "started_on": (date.today() + timedelta(days=1)).isoformat(),
            "flock_size": 240, "flock_age_weeks": 25, "feed_intake_kg": "40.0",
        })
        self.assertFalse(Flock.objects.exists())
        self.assertContains(response, "can&#x27;t be in the future")

    def test_profile_shows_latest_log_size_and_age(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self.client.get("/flock/")
        self.assertEqual(response.context["latest_log"].flock_size, 240)
        self.assertEqual(response.context["latest_log"].flock_age_weeks, 25)

    def test_profile_shows_registered_details_before_any_daily_log_exists(self):
        """A freshly registered flock has no DailyLog yet, but its confirmed
        pending_* details should show immediately rather than waiting for the first
        entry (previously showed "—" placeholders until then)."""
        self.client.post("/flock/", {
            "started_on": date.today().isoformat(),
            "flock_size": 240, "flock_age_weeks": 25, "feed_intake_kg": "40.0",
        })
        response = self.client.get("/flock/")
        self.assertIsNone(response.context["latest_log"])
        self.assertEqual(response.context["current_age_weeks"], 25)
        self.assertContains(response, "240 ducks")

    @patch("farm.services.timezone")
    def test_profile_current_age_is_projected_forward_to_today(self, mock_timezone):
        """Average Age on the profile card must reflect calendar weeks elapsed since
        the latest log, not that log's stale snapshot (same rule as the log_daily_data
        prefill — itikcare-spec.md section 10)."""
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2023, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=94, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        mock_timezone.localdate.return_value = date(2024, 2, 12)  # exactly 6 weeks (42 days) later
        response = self.client.get("/flock/")
        self.assertEqual(response.context["latest_log"].flock_age_weeks, 94)
        self.assertEqual(response.context["current_age_weeks"], 100)

    @patch("farm.views.trigger_retrain")
    def test_retiring_a_flock_deactivates_it_and_clears_the_active_flock(self, mock_trigger_retrain):
        old_flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        self.client.post("/flock/retire/")
        old_flock.refresh_from_db()
        self.assertFalse(old_flock.is_active)
        # No replacement flock is created — the farmer registers the next one
        # explicitly, same as a brand-new farm with no flock at all.
        self.assertFalse(Flock.objects.filter(is_active=True).exists())
        self.assertIsNone(self.client.get("/flock/").context["active_flock"])
        # Retirement closes out a whole generation's data -> a retrain is triggered.
        mock_trigger_retrain.assert_called_once_with("flock_retired", self.user.id)

    def test_retiring_requires_post(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.get("/flock/retire/")
        self.assertEqual(response.status_code, 405)

    def test_registering_after_retirement_continues_generation_numbering(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_active=False)
        self.client.post("/flock/", {
            "started_on": date.today().isoformat(),
            "flock_size": 200, "flock_age_weeks": 1, "feed_intake_kg": "30.0",
        })
        new_flock = Flock.objects.get(is_active=True)
        self.assertEqual(new_flock.generation_number, 2)
        self.assertEqual(new_flock.started_on, date.today())
        self.assertEqual(new_flock.pending_flock_size, 200)

    def test_first_entry_after_retirement_prefills_from_registration(self):
        old_flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=old_flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        self.client.post("/flock/retire/")
        self.client.post("/flock/", {
            "started_on": date.today().isoformat(),
            "flock_size": 200, "flock_age_weeks": 1, "feed_intake_kg": "30.0",
        })

        # No previous DailyLog for the new flock, but the values confirmed at
        # registration (pending_flock_size etc.) pre-fill the first entry instead.
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.context["form"].initial["flock_size"], 200)
        self.assertEqual(response.context["form"].initial["flock_age_weeks"], 1)
        self.assertEqual(response.context["form"].initial["feed_intake_kg"], Decimal("30.0"))

        # Registration set the new flock's started_on to today, so the first live
        # entry for it must be dated today or later too (see date-range test above).
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": date.today().isoformat()})
        self.assertRedirects(response, "/")
        new_flock = Flock.objects.get(is_active=True)
        new_flock.refresh_from_db()
        self.assertIsNone(new_flock.pending_flock_size)
        self.assertIsNone(new_flock.pending_flock_age_weeks)
        self.assertIsNone(new_flock.pending_feed_intake_kg)

    def test_new_generations_first_entry_continues_the_global_caging_period(self):
        """A retired flock's first live entry must NOT restart caging_period at 1.

        caging_period is a segment marker unique per owner (log_daily_data scopes the
        Max("caging_period") lookup to flock__owner=request.user), so colliding with
        an earlier flock's period would merge two different generations' rows into one
        training segment across the generation-reset gap (itikcare-spec.md section 10)
        — pipeline.py's segment_key additionally prefixes by flock_id as a second,
        independent guard against exactly this.
        """
        old_flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=old_flock, date=date(2024, 1, 1), flock_size=240, caging_period=3,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        self.client.post("/flock/retire/")
        self.client.post("/flock/", {
            "started_on": date.today().isoformat(),
            "flock_size": 240, "flock_age_weeks": 25, "feed_intake_kg": "40.0",
        })

        # Registration set the new flock's started_on to today, so the first live
        # entry for it must be dated today or later too (see date-range test above).
        today = date.today()
        response = self.client.post("/log-daily-data/", {**VALID_LOG_POST, "date": today.isoformat()})
        self.assertRedirects(response, "/")
        new_log = DailyLog.objects.get(date=today)
        self.assertEqual(new_log.caging_period, 4)

    def test_toggle_caging_status_flips_a_caged_flock_to_free_range(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        self.assertTrue(flock.is_caged)

        response = self.client.post("/flock/toggle-caging/")
        self.assertRedirects(response, "/flock/")
        flock.refresh_from_db()
        self.assertFalse(flock.is_caged)

    def test_toggle_caging_status_flips_a_free_range_flock_back_to_caged(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_caged=False)

        self.client.post("/flock/toggle-caging/")
        flock.refresh_from_db()
        self.assertTrue(flock.is_caged)

    def test_toggle_caging_status_with_no_active_flock_shows_error(self):
        response = self.client.post("/flock/toggle-caging/", follow=True)
        self.assertRedirects(response, "/flock/")
        messages = list(response.context["messages"])
        self.assertTrue(any("No active flock" in str(m) for m in messages))

    def test_toggle_caging_status_rejects_get(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.get("/flock/toggle-caging/")
        self.assertEqual(response.status_code, 405)

    def test_profile_shows_flock_number_label_not_generation(self):
        Flock.objects.create(owner=self.user, generation_number=2, started_on=date(2024, 1, 1))
        response = self.client.get("/flock/")
        self.assertContains(response, "Flock #2")
        self.assertNotContains(response, "Generation")

    def test_resume_caging_marks_flock_caged_and_stages_confirmed_count(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
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
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        self.assertTrue(flock.is_caged)
        response = self.client.post("/flock/resume-caging/", {"flock_size": 210}, follow=True)
        self.assertRedirects(response, "/flock/")
        flock.refresh_from_db()
        self.assertIsNone(flock.pending_flock_size)

    def test_resume_caging_rejects_invalid_flock_size(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        response = self.client.post("/flock/resume-caging/", {"flock_size": 0}, follow=True)
        flock.refresh_from_db()
        self.assertFalse(flock.is_caged)
        self.assertIsNone(flock.pending_flock_size)
        messages = list(response.context["messages"])
        self.assertTrue(messages)

    def test_resume_caging_rejects_get(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
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
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), pending_flock_size=210)
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.context["form"].initial["flock_size"], 210)

    def test_get_prefills_flock_size_from_pending_override_on_a_first_ever_entry(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), pending_flock_size=210)
        response = self.client.get("/log-daily-data/")
        self.assertEqual(response.context["form"].initial["flock_size"], 210)

    def test_saving_a_log_clears_the_pending_override(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), pending_flock_size=210)
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

    @patch("farm.weather.requests.get")
    def test_explicit_coordinates_take_priority_over_settings(self, mock_get):
        mock_get.return_value.json.return_value = {
            "current": {"temperature_2m": 29.34, "relative_humidity_2m": 81.6}
        }
        fetch_current_weather(latitude=13.5, longitude=123.2)
        called_params = mock_get.call_args.kwargs["params"]
        self.assertEqual(called_params["latitude"], 13.5)
        self.assertEqual(called_params["longitude"], 123.2)


def _fake_response(json_data):
    """A requests.Response stand-in whose .json() returns json_data and whose
    raise_for_status() is a no-op — used to build per-call side_effect lists for
    geocode_address's one-request-per-word behavior, and for
    fetch_historical_weather's archive/forecast fallback tests below."""
    response = Mock()
    response.json.return_value = json_data
    response.raise_for_status.return_value = None
    return response


@override_settings(FARM_LATITUDE=14.1, FARM_LONGITUDE=122.9)
class HistoricalWeatherFetchTests(TestCase):
    """Covers farm.weather.fetch_historical_weather in isolation (no view/DB
    involvement), including its archive-API-first/forecast-endpoint-fallback tiers."""

    def setUp(self):
        self.past_date = date.today() - timedelta(days=10)

    @patch("farm.weather.requests.get")
    def test_today_returns_none_without_calling_the_api(self, mock_get):
        self.assertIsNone(fetch_historical_weather(date.today()))
        mock_get.assert_not_called()

    @patch("farm.weather.requests.get")
    def test_future_date_returns_none_without_calling_the_api(self, mock_get):
        self.assertIsNone(fetch_historical_weather(date.today() + timedelta(days=1)))
        mock_get.assert_not_called()

    @override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
    @patch("farm.weather.requests.get")
    def test_missing_coordinates_returns_none_without_calling_the_api(self, mock_get):
        self.assertIsNone(fetch_historical_weather(self.past_date))
        mock_get.assert_not_called()

    @patch("farm.weather.requests.get")
    def test_archive_hit_returns_rounded_average_without_forecast_fallback(self, mock_get):
        mock_get.return_value = _fake_response({
            "hourly": {
                "temperature_2m": [28.0, 30.0],
                "relative_humidity_2m": [80.0, 84.0],
            }
        })
        result = fetch_historical_weather(self.past_date)
        self.assertEqual(result, {"temperature_c": 29.0, "humidity_pct": 82.0})
        mock_get.assert_called_once()
        self.assertIn("archive-api.open-meteo.com", mock_get.call_args.args[0])

    @patch("farm.weather.requests.get")
    def test_archive_lag_falls_back_to_forecast_endpoint(self, mock_get):
        # The archive endpoint hasn't caught up to this date yet -- an all-null hourly
        # series, the same shape Open-Meteo actually returns for a not-yet-available
        # date -- so the second call (the regular forecast endpoint's own date-range
        # query) should be tried next, and its result used instead.
        mock_get.side_effect = [
            _fake_response({"hourly": {"temperature_2m": [None, None], "relative_humidity_2m": [None, None]}}),
            _fake_response({"hourly": {"temperature_2m": [26.0], "relative_humidity_2m": [88.0]}}),
        ]
        result = fetch_historical_weather(self.past_date)
        self.assertEqual(result, {"temperature_c": 26.0, "humidity_pct": 88.0})
        self.assertEqual(mock_get.call_count, 2)
        first_url = mock_get.call_args_list[0].args[0]
        second_url = mock_get.call_args_list[1].args[0]
        self.assertIn("archive-api.open-meteo.com", first_url)
        self.assertIn("api.open-meteo.com/v1/forecast", second_url)

    @patch("farm.weather.requests.get", side_effect=requests.exceptions.Timeout)
    def test_both_endpoints_failing_returns_none(self, mock_get):
        self.assertIsNone(fetch_historical_weather(self.past_date))
        self.assertEqual(mock_get.call_count, 2)


class GeocodeAddressTests(TestCase):
    """Covers farm.weather.geocode_address in isolation (no view/DB involvement).

    geocode_address queries once per word in the address (see its docstring for why),
    so tests with more than one word mock requests.get with a side_effect list — one
    response per word, in address order — rather than a single return_value.
    """

    def test_blank_address_returns_none_without_calling_the_api(self):
        with patch("farm.weather.requests.get") as mock_get:
            self.assertIsNone(geocode_address(""))
            mock_get.assert_not_called()

    @patch("farm.weather.requests.get")
    def test_single_word_address_trusts_the_only_result(self, mock_get):
        mock_get.return_value.json.return_value = {
            "results": [{"latitude": 13.72, "longitude": 123.02, "name": "Libmanan"}]
        }
        self.assertEqual(geocode_address("Libmanan"), (13.72, 123.02))

    @patch("farm.weather.requests.get")
    def test_multi_word_address_picks_the_result_corroborated_by_another_word(self, mock_get):
        # "Libmanan, Camarines Sur" is queried word by word ("Libmanan", "Camarines",
        # "Sur"). "Libmanan" resolves unambiguously; the other two words have no
        # gazetteer entry of their own. The province name in Libmanan's admin2 is what
        # ties the words together and makes the match trustworthy.
        mock_get.side_effect = [
            _fake_response({
                "results": [{
                    "latitude": 13.6928, "longitude": 123.0596, "name": "Libmanan",
                    "admin1": "Bicol Region", "admin2": "Province of Camarines Sur",
                    "admin3": "Municipality of Libmanan",
                }]
            }),
            _fake_response({"results": []}),
            _fake_response({"results": []}),
        ]
        self.assertEqual(geocode_address("Libmanan, Camarines Sur"), (13.6928, 123.0596))

    @patch("farm.weather.requests.get")
    def test_ambiguous_word_without_corroboration_returns_none(self, mock_get):
        # Regression test: a bare "Patag" query has several same-named PH results,
        # including a barangay in Bulacan ~300km from this app's farmers in Camarines
        # Sur. Without a second word in the address to confirm which one is meant, none
        # of them can be trusted.
        mock_get.return_value.json.return_value = {
            "results": [
                {
                    "latitude": 14.83333, "longitude": 120.96667, "name": "Patag",
                    "admin1": "Central Luzon", "admin2": "Province of Bulacan",
                    "admin3": "Santa Maria",
                },
                {
                    "latitude": 13.73333, "longitude": 123.05, "name": "Patag",
                    "admin1": "Bicol Region", "admin2": "Province of Camarines Sur",
                    "admin3": "Municipality of Libmanan",
                },
            ]
        }
        self.assertIsNone(geocode_address("Patag"))

    @patch("farm.weather.requests.get")
    def test_no_results_returns_none(self, mock_get):
        mock_get.return_value.json.return_value = {}
        self.assertIsNone(geocode_address("Nowhere Land"))

    @patch("farm.weather.requests.get", side_effect=requests.exceptions.Timeout)
    def test_timeout_returns_none(self, mock_get):
        self.assertIsNone(geocode_address("Libmanan"))

    @patch("farm.weather.requests.get", side_effect=requests.exceptions.ConnectionError)
    def test_connection_error_returns_none(self, mock_get):
        self.assertIsNone(geocode_address("Libmanan"))


class GetEffectiveCoordinatesTests(TestCase):
    def test_returns_owners_own_coordinates_when_set(self):
        owner = User.objects.create_user(username="located", password="pw", latitude=13.5, longitude=123.2)
        lat, lon = get_effective_coordinates(owner)
        self.assertAlmostEqual(float(lat), 13.5)
        self.assertAlmostEqual(float(lon), 123.2)

    def test_returns_none_none_when_owner_has_no_coordinates(self):
        owner = User.objects.create_user(username="unlocated", password="pw")
        self.assertEqual(get_effective_coordinates(owner), (None, None))


class OperationalTodayTests(TestCase):
    """Covers operational_today() in isolation, pinning `now` explicitly so these never
    depend on the real wall clock — the whole point of the function is behavior that
    only shows up in the midnight-FARM_DAY_START_HOUR window, which a real-clock test
    would only exercise on the rare run that happens to land there."""

    def test_before_6am_rolls_back_to_yesterday(self):
        now = timezone.make_aware(datetime(2024, 6, 15, 3, 0))
        self.assertEqual(operational_today(now=now), date(2024, 6, 14))

    def test_just_before_6am_still_rolls_back_to_yesterday(self):
        now = timezone.make_aware(datetime(2024, 6, 15, 5, 59))
        self.assertEqual(operational_today(now=now), date(2024, 6, 14))

    def test_at_6am_sharp_rolls_forward_to_today(self):
        now = timezone.make_aware(datetime(2024, 6, 15, 6, 0))
        self.assertEqual(operational_today(now=now), date(2024, 6, 15))

    def test_late_evening_is_still_todays_calendar_date(self):
        now = timezone.make_aware(datetime(2024, 6, 15, 23, 0))
        self.assertEqual(operational_today(now=now), date(2024, 6, 15))

    def test_farm_day_start_hour_matches_the_documented_6am_boundary(self):
        # A guard, not a tautology: if this constant is ever tuned, the four tests
        # above stop meaning what their names say unless updated alongside it.
        self.assertEqual(FARM_DAY_START_HOUR, 6)


class AssignCagingPeriodsTests(TestCase):
    """Covers services.assign_caging_periods in isolation — the single source of truth
    shared by log_daily_data's single-row path and the CSV import's whole-batch path."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))

    def test_first_ever_import_for_a_fresh_flock_starts_at_one(self):
        dates = [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]
        periods = assign_caging_periods(self.flock, self.user, dates)
        self.assertEqual(periods, [1, 1, 1])

    def test_first_ever_import_continues_the_owners_overall_max(self):
        other_flock = Flock.objects.create(owner=self.user, generation_number=2, started_on=date(2023, 1, 1))
        DailyLog.objects.create(
            flock=other_flock, date=date(2023, 6, 1), flock_size=200, caging_period=5,
            flock_age_weeks=50, egg_count=140, feed_intake_kg="35.0",
            temperature_c="27.0", humidity_pct="70.0", recorded_by=self.user,
        )
        periods = assign_caging_periods(self.flock, self.user, [date(2024, 1, 1)])
        self.assertEqual(periods, [6])

    def test_internal_gap_within_the_batch_splits_into_two_periods(self):
        dates = [date(2024, 1, 1), date(2024, 1, 2), date(2024, 6, 1), date(2024, 6, 2)]
        periods = assign_caging_periods(self.flock, self.user, dates)
        self.assertEqual(periods, [1, 1, 2, 2])

    def test_batch_bridges_from_the_flocks_existing_last_log(self):
        DailyLog.objects.create(
            flock=self.flock, date=date(2024, 1, 1), flock_size=240, caging_period=3,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        # Small gap: continues period 3.
        periods = assign_caging_periods(self.flock, self.user, [date(2024, 1, 5)])
        self.assertEqual(periods, [3])
        # Large gap: starts period 4.
        periods = assign_caging_periods(self.flock, self.user, [date(2024, 6, 1)])
        self.assertEqual(periods, [4])


class TrendChartHelpersTests(TestCase):
    """Covers services.build_trend_chart_data/build_next_day_forecasts/resolve_trend_range
    — the implementation shared by the dashboard and the Forecast & Recommendations page's
    Egg Yield Trend chart (dashboard/views.py, forecasting/views.py)."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))

    def test_resolve_trend_range_falls_back_to_seven_on_invalid_value(self):
        self.assertEqual(resolve_trend_range("30"), "30")
        self.assertEqual(resolve_trend_range("all"), "all")
        self.assertEqual(resolve_trend_range("bogus"), "7")
        self.assertEqual(resolve_trend_range(None), "7")

    def test_build_next_day_forecasts_returns_empty_list_for_no_forecast(self):
        self.assertEqual(build_next_day_forecasts(None), [])

    def test_build_next_day_forecasts_derives_three_distinct_days(self):
        forecast = Forecast.objects.create(
            flock=self.flock, forecast_date=date(2024, 1, 10),
            predicted_daily_yield=Decimal("150.00"), predicted_tri_day_yield=Decimal("450.00"),
            predicted_next_day1_yield=Decimal("151.00"), predicted_next_day2_yield=Decimal("152.00"),
            predicted_next_day3_yield=Decimal("153.00"),
            feature_importances={"temperature_c": 0.4}, model_version="rf-test",
        )
        days = build_next_day_forecasts(forecast)
        self.assertEqual([d["date"] for d in days], [date(2024, 1, 11), date(2024, 1, 12), date(2024, 1, 13)])
        self.assertEqual([d["value"] for d in days], [Decimal("151.00"), Decimal("152.00"), Decimal("153.00")])
        self.assertEqual([d["is_tomorrow"] for d in days], [True, False, False])

    def test_uncaged_flock_returns_no_trend_data_regardless_of_range(self):
        DailyLog.objects.create(
            flock=self.flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        data = build_trend_chart_data(self.flock, False, "all", [])
        self.assertEqual(data["trend_logs"], [])
        self.assertEqual(data["trend_actual_json"], "[]")

    def test_actual_and_predicted_align_by_date_and_future_forecast_is_dashed_from_the_right_index(self):
        log1 = DailyLog.objects.create(
            flock=self.flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        log2 = DailyLog.objects.create(
            flock=self.flock, date=date(2024, 1, 2), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=155, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        forecast = Forecast.objects.create(
            flock=self.flock, forecast_date=date(2024, 1, 2),
            predicted_daily_yield=Decimal("152.00"), predicted_tri_day_yield=Decimal("450.00"),
            predicted_next_day1_yield=Decimal("157.00"), predicted_next_day2_yield=Decimal("158.00"),
            predicted_next_day3_yield=Decimal("159.00"),
            feature_importances={"temperature_c": 0.4}, model_version="rf-test",
        )
        forecast.source_logs.set([log1, log2])

        next_day_forecasts = build_next_day_forecasts(forecast)
        data = build_trend_chart_data(self.flock, True, "all", next_day_forecasts)

        self.assertEqual(json.loads(data["trend_actual_json"]), [150.0, 155.0, None, None, None])
        self.assertEqual(json.loads(data["trend_predicted_json"]), [None, 152.0, 157.0, 158.0, 159.0])
        # The 2 real logged days come first (index 0, 1); the dashed forecast tail starts
        # right after, at index 2.
        self.assertEqual(data["trend_future_start_index"], 2)


class RecordsChartHelpersTests(TestCase):
    """Covers services.build_records_chart_data/build_records_summary — the Farm
    Records page's chart panels and stat tiles (farm/views.py::farm_records)."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))

    def _make_log(self, day, egg_count, feed_intake_kg, flock_size=200, temperature_c="28.0", humidity_pct="75.0"):
        return DailyLog.objects.create(
            flock=self.flock, date=date(2024, 1, day), flock_size=flock_size, caging_period=1,
            flock_age_weeks=25, egg_count=egg_count, feed_intake_kg=feed_intake_kg,
            temperature_c=temperature_c, humidity_pct=humidity_pct, recorded_by=self.user,
        )

    def test_chart_data_is_ordered_oldest_to_newest_regardless_of_input_order(self):
        log2 = self._make_log(2, egg_count=155, feed_intake_kg="40.0")
        log1 = self._make_log(1, egg_count=150, feed_intake_kg="40.0")
        data = build_records_chart_data([log2, log1])
        self.assertEqual(json.loads(data["records_chart_labels_json"]), ["Jan 1", "Jan 2"])
        self.assertEqual(json.loads(data["records_chart_egg_yield_json"]), [150, 155])

    def test_feed_per_bird_grams_matches_recommendations_rules_formula(self):
        # 40kg feed / 200 birds * 1000 = 200 g/bird/day (recommendations/rules.py's
        # _feed_per_bird_grams formula, mirrored in farm/services.py so the two
        # numbers can never silently drift apart).
        log = self._make_log(1, egg_count=150, feed_intake_kg="40.0", flock_size=200)
        data = build_records_chart_data([log])
        self.assertEqual(json.loads(data["records_chart_feed_per_bird_json"]), [200.0])

    def test_summary_reports_latest_flock_size_and_averages(self):
        self._make_log(1, egg_count=150, feed_intake_kg="40.0", flock_size=200)
        log2 = self._make_log(2, egg_count=160, feed_intake_kg="44.0", flock_size=198)
        summary = build_records_summary([log2, DailyLog.objects.get(date=date(2024, 1, 1))], [])
        self.assertEqual(summary["avg_egg_yield"], 155.0)
        self.assertEqual(summary["current_flock_size"], 198)  # from the latest-dated log, not input order

    def test_summary_delta_is_none_without_a_previous_month(self):
        self._make_log(1, egg_count=150, feed_intake_kg="40.0")
        log2 = self._make_log(2, egg_count=160, feed_intake_kg="40.0")
        summary = build_records_summary([log2], [])
        self.assertIsNone(summary["egg_yield_delta_pct"])

    def test_summary_delta_is_computed_against_previous_month_average(self):
        current = self._make_log(2, egg_count=165, feed_intake_kg="40.0")
        previous = DailyLog.objects.create(
            flock=self.flock, date=date(2023, 12, 1), flock_size=200, caging_period=1,
            flock_age_weeks=24, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        summary = build_records_summary([current], [previous])
        # (165 - 150) / 150 * 100 = 10.0%
        self.assertEqual(summary["egg_yield_delta_pct"], 10.0)


class FarmRecordsChartAvailabilityTests(TestCase):
    """Covers the farm_records view's records_charts_available gate: the charts/stat
    tiles must only appear once there's enough data for them to mean anything (a
    single point has no trend) — see RECORDS_CHART_MIN_LOGS."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    def _make_log(self, day):
        return DailyLog.objects.create(
            flock=self.flock, date=date(2024, 1, day), flock_size=200, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )

    def test_charts_hidden_below_the_minimum_log_count(self):
        self.assertEqual(RECORDS_CHART_MIN_LOGS, 2)
        self._make_log(1)
        response = self.client.get("/farm-records/")
        self.assertFalse(response.context["records_charts_available"])
        self.assertNotIn("avg_egg_yield", response.context)

    def test_charts_shown_at_the_minimum_log_count(self):
        self._make_log(1)
        self._make_log(2)
        response = self.client.get("/farm-records/")
        self.assertTrue(response.context["records_charts_available"])
        self.assertIn("avg_egg_yield", response.context)
        self.assertEqual(json.loads(response.context["records_chart_egg_yield_json"]), [150, 150])


class BackfillLockedDailyLogsCommandTests(TestCase):
    """Covers the one-off backfill_locked_daily_logs command: it should lock exactly
    the DailyLogs of owners whose models/ directory already holds a trained artifact,
    and leave everything else untouched, per DailyLog.is_locked's help_text."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.models_dir = Path(self.tmpdir.name)

        self.trained_user = User.objects.create_user(username="trainedfarmer", password="pw12345")
        self.trained_flock = Flock.objects.create(
            owner=self.trained_user, generation_number=1, started_on=date(2024, 1, 1),
        )
        self.trained_log = DailyLog.objects.create(
            flock=self.trained_flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.trained_user,
        )

        self.untrained_user = User.objects.create_user(username="untrainedfarmer", password="pw12345")
        self.untrained_flock = Flock.objects.create(
            owner=self.untrained_user, generation_number=1, started_on=date(2024, 1, 1),
        )
        self.untrained_log = DailyLog.objects.create(
            flock=self.untrained_flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.untrained_user,
        )

        (self.models_dir / f"forecast_model_{self.trained_user.id}.joblib").write_bytes(b"stub")
        # Legacy pre-multi-tenancy artifact -- must never be matched/attributed to anyone.
        (self.models_dir / "forecast_model.joblib").write_bytes(b"stub")

    def _run(self, dry_run=False):
        with patch("farm.management.commands.backfill_locked_daily_logs.MODEL_DIR", self.models_dir):
            call_command("backfill_locked_daily_logs", dry_run=dry_run)

    def test_locks_only_owners_with_a_trained_model_artifact(self):
        self._run()
        self.trained_log.refresh_from_db()
        self.untrained_log.refresh_from_db()
        self.assertTrue(self.trained_log.is_locked)
        self.assertFalse(self.untrained_log.is_locked)

    def test_dry_run_reports_without_locking(self):
        self._run(dry_run=True)
        self.trained_log.refresh_from_db()
        self.assertFalse(self.trained_log.is_locked)


class DailyLogModelValidationTests(TestCase):
    """DailyLog.clean() is the model-level backstop for the date-range rule
    DailyLogForm/DailyLogEditForm.clean_date() already enforce — it must hold for
    any full_clean() caller that bypasses those forms, e.g. the Django admin or
    import_daily_logs."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))

    def _log(self, **overrides):
        base = dict(
            flock=self.flock, date=date(2024, 1, 5), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        base.update(overrides)
        return DailyLog(**base)

    def test_future_date_rejected_by_full_clean(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        with self.assertRaises(ValidationError) as ctx:
            self._log(date=tomorrow).full_clean()
        self.assertIn("date", ctx.exception.message_dict)

    def test_date_before_flock_start_rejected_by_full_clean(self):
        with self.assertRaises(ValidationError) as ctx:
            self._log(date=date(2023, 12, 31)).full_clean()
        self.assertIn("date", ctx.exception.message_dict)

    def test_valid_date_passes_full_clean(self):
        self._log().full_clean()  # should not raise

    def test_new_log_for_a_retired_flock_rejected_by_full_clean(self):
        self.flock.is_active = False
        self.flock.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError) as ctx:
            self._log().full_clean()
        self.assertIn("flock", ctx.exception.message_dict)

    def test_retired_flock_guard_can_be_bypassed_for_the_historical_import(self):
        self.flock.is_active = False
        self.flock.save(update_fields=["is_active"])
        log = self._log()
        log._allow_inactive_flock = True
        log.full_clean()  # should not raise

    def test_editing_an_existing_retired_flock_log_still_passes_full_clean(self):
        """The guard only blocks *creating* a log for a retired flock — re-cleaning an
        existing row (the admin edit path) must still work."""
        log = self._log()
        log.save()
        self.flock.is_active = False
        self.flock.save(update_fields=["is_active"])
        log.egg_count = 175
        log.full_clean()  # not adding -> retired-flock guard doesn't apply


class DailyLogAdminTests(TestCase):
    """Staff editing DailyLog via /admin/ must be held to the same rules as the
    farmer-facing views: locked (model-trained) rows are immutable, and any change
    to an unlocked row writes a DailyLogEdit audit row — see FarmRecordEditTests for
    the equivalent farmer-facing behavior this mirrors."""

    def setUp(self):
        self.staff = User.objects.create_superuser(username="admin1", password="pw12345", email="admin1@example.com")
        self.client = Client()
        self.client.login(username="admin1", password="pw12345")
        self.farmer = User.objects.create_user(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.farmer, generation_number=1, started_on=date(2024, 1, 1))
        self.log = DailyLog.objects.create(
            flock=self.flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.farmer,
        )

    def _change_url(self):
        return f"/admin/farm/dailylog/{self.log.pk}/change/"

    def _change_post(self, **overrides):
        base = {
            "flock": self.flock.pk, "date": "2024-01-01", "flock_size": 240,
            "caging_period": 1, "flock_age_weeks": 25, "egg_count": 150,
            "feed_intake_kg": "40.0", "temperature_c": "28.0", "humidity_pct": "75.0",
            "recorded_by": self.farmer.pk, "is_locked": "",
        }
        base.update(overrides)
        return self.client.post(self._change_url(), base, follow=True)

    def test_editing_a_field_via_admin_creates_an_audit_row(self):
        self._change_post(egg_count=175)
        self.log.refresh_from_db()
        self.assertEqual(self.log.egg_count, 175)
        edits = DailyLogEdit.objects.filter(daily_log=self.log)
        self.assertEqual(edits.count(), 1)
        edit = edits.first()
        self.assertEqual(edit.field_name, "egg_count")
        self.assertEqual(edit.old_value, "150")
        self.assertEqual(edit.new_value, "175")
        self.assertEqual(edit.changed_by, self.staff)

    def test_resubmitting_unchanged_values_via_admin_creates_no_audit_rows(self):
        self._change_post()
        self.assertEqual(DailyLogEdit.objects.filter(daily_log=self.log).count(), 0)

    def test_locked_record_fields_are_readonly_in_admin(self):
        self.log.is_locked = True
        self.log.save(update_fields=["is_locked"])
        response = self.client.get(self._change_url())
        # A readonly changeform has no editable "egg_count" input for the field.
        self.assertNotContains(response, 'name="egg_count"')

    def test_locked_record_post_is_blocked_by_admin(self):
        self.log.is_locked = True
        self.log.save(update_fields=["is_locked"])
        self._change_post(egg_count=999)
        self.log.refresh_from_db()
        self.assertEqual(self.log.egg_count, 150)
        self.assertEqual(DailyLogEdit.objects.filter(daily_log=self.log).count(), 0)

    def test_locked_record_has_no_delete_action_in_admin(self):
        self.log.is_locked = True
        self.log.save(update_fields=["is_locked"])
        response = self.client.get(self._change_url())
        self.assertNotContains(response, "Delete")

    def test_unlocked_record_can_still_be_deleted_via_admin(self):
        self.client.post(f"/admin/farm/dailylog/{self.log.pk}/delete/", {"post": "yes"}, follow=True)
        self.assertFalse(DailyLog.objects.filter(pk=self.log.pk).exists())


class SendDailyLogRemindersCommandTests(TestCase):
    """Covers send_daily_log_reminders: it should email + record a reminder only
    for an owner whose active flock is caged and has no DailyLog for today, and be
    a no-op (no email, no DailyLogReminder row) for every other flock state, plus
    stay idempotent if run twice in the same day (DailyLogReminder's unique
    constraint on owner+reminder_date)."""

    def setUp(self):
        # operational_today(), matching what the command itself now uses (rolls over
        # at 6am, not midnight — see farm.services.operational_today).
        self.today = operational_today()

    def _make_owner(self, username, **flock_kwargs):
        owner = User.objects.create_user(username=username, password="pw12345", email=f"{username}@example.com")
        flock = Flock.objects.create(
            owner=owner, generation_number=1, started_on=date(2024, 1, 1), **flock_kwargs
        )
        return owner, flock

    def _log_today(self, owner, flock):
        DailyLog.objects.create(
            flock=flock, date=self.today, flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=owner,
        )

    def test_sends_and_records_reminder_for_eligible_owner(self):
        owner, flock = self._make_owner("farmer1")
        call_command("send_daily_log_reminders")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [owner.email])
        reminders = DailyLogReminder.objects.filter(owner=owner, reminder_date=self.today)
        self.assertEqual(reminders.count(), 1)
        self.assertEqual(reminders.first().flock, flock)

    def test_no_reminder_when_owner_has_no_flock(self):
        User.objects.create_user(username="noflockfarmer", password="pw12345", email="noflock@example.com")
        call_command("send_daily_log_reminders")
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(DailyLogReminder.objects.count(), 0)

    def test_no_reminder_for_retired_flock(self):
        self._make_owner("retiredfarmer", is_active=False)
        call_command("send_daily_log_reminders")
        self.assertEqual(len(mail.outbox), 0)

    def test_no_reminder_for_free_range_flock(self):
        self._make_owner("freerangefarmer", is_caged=False)
        call_command("send_daily_log_reminders")
        self.assertEqual(len(mail.outbox), 0)

    def test_no_reminder_when_already_logged_today(self):
        owner, flock = self._make_owner("loggedfarmer")
        self._log_today(owner, flock)
        call_command("send_daily_log_reminders")
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(DailyLogReminder.objects.count(), 0)

    def test_rerunning_same_day_does_not_double_send(self):
        self._make_owner("farmer1")
        call_command("send_daily_log_reminders")
        call_command("send_daily_log_reminders")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(DailyLogReminder.objects.count(), 1)


class ExportFarmDataCommandTests(TestCase):
    """export_farm_data: only the chosen owners' rows (plus referenced accounts), with
    avatars blanked and every foreign key still resolvable inside the fixture."""

    def setUp(self):
        self.admin = User.objects.create_user(username="admin_rec", password="pw12345")
        self.owner = User.objects.create_user(username="farmer_a", password="pw12345", avatar="avatars/a.png")
        self.other = User.objects.create_user(username="farmer_b", password="pw12345")
        self.owner_flock = Flock.objects.create(owner=self.owner, generation_number=1, started_on=date(2024, 1, 1))
        self.other_flock = Flock.objects.create(owner=self.other, generation_number=1, started_on=date(2024, 1, 1))
        log_kwargs = dict(
            flock_size=240, caging_period=1, flock_age_weeks=30, egg_count=150,
            feed_intake_kg=Decimal("30.0"), temperature_c=Decimal("28.0"),
            humidity_pct=Decimal("70.0"),
        )
        # Recorded by a third account (like the admin who imported the historical CSV).
        self.owner_log = DailyLog.objects.create(
            flock=self.owner_flock, date=date(2024, 1, 1), recorded_by=self.admin, **log_kwargs
        )
        DailyLog.objects.create(
            flock=self.other_flock, date=date(2024, 1, 1), recorded_by=self.other, **log_kwargs
        )

    def _export(self, owner_ids):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "farm_data.json"
            call_command("export_farm_data", owner_ids=owner_ids, output=str(output), stdout=Mock())
            return json.loads(output.read_text(encoding="utf-8"))

    def test_exports_only_chosen_owner_and_referenced_accounts(self):
        rows = self._export([self.owner.pk])
        by_model = {}
        for row in rows:
            by_model.setdefault(row["model"], []).append(row["pk"])
        self.assertEqual(sorted(by_model["accounts.user"]), sorted([self.owner.pk, self.admin.pk]))
        self.assertEqual(by_model["farm.flock"], [self.owner_flock.pk])
        self.assertEqual(by_model["farm.dailylog"], [self.owner_log.pk])

    def test_avatar_is_blanked_and_database_row_untouched(self):
        rows = self._export([self.owner.pk])
        owner_row = next(r for r in rows if r["model"] == "accounts.user" and r["pk"] == self.owner.pk)
        self.assertEqual(owner_row["fields"]["avatar"], "")
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.avatar.name, "avatars/a.png")

    def test_unknown_owner_id_is_rejected(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self._export([999999])
