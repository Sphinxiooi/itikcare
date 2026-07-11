from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from farm.models import DailyLog, Flock
from forecasting.models import Forecast

User = get_user_model()


@override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
class DashboardIndexTests(TestCase):
    """Covers the "3-Day Egg Yield Outlook" panel (see farm diagnostics: every
    Forecast is a same-day nowcast, so this panel must show the single
    predicted_tri_day_yield stat rather than iterating non-existent future-dated
    Forecast rows)."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    def test_no_active_flock_shows_no_forecast_placeholder(self):
        response = self.client.get("/")
        self.assertContains(response, "No forecasts generated yet.")

    def test_shows_tri_day_yield_stat_for_todays_forecast(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1))
        log = DailyLog.objects.create(
            flock=flock, date=date.today(), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        forecast = Forecast.objects.create(
            flock=flock, forecast_date=date.today(),
            predicted_daily_yield=Decimal("152.00"),
            predicted_tri_day_yield=Decimal("455.00"),
            feature_importances={"temperature_c": 0.4},
            model_version="rf-test",
        )
        forecast.source_logs.set([log])

        response = self.client.get("/")
        self.assertContains(response, "3-Day Egg Yield Outlook")
        self.assertContains(response, "455.00")
        self.assertNotContains(response, "Next 3-day Forecast")


class DashboardCurrentWeatherTests(TestCase):
    """Covers the header's live-weather guidance panel (dashboard/views.py's
    current_weather), which is independent of active_flock/flock_is_caged -- distinct
    from today_log, which shows the last *submitted* DailyLog and can go stale."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    @patch("dashboard.views.fetch_current_weather", return_value={"temperature_c": 30.5, "humidity_pct": 82.0})
    def test_shows_live_weather_when_fetch_succeeds(self, mock_fetch):
        response = self.client.get("/")
        self.assertContains(response, "30.5")
        self.assertContains(response, "82.0")
        self.assertContains(response, "current weather in your area")

    @patch("dashboard.views.fetch_current_weather", return_value=None)
    def test_hides_weather_panel_when_fetch_fails(self, mock_fetch):
        response = self.client.get("/")
        self.assertNotContains(response, "current weather in your area")

    @patch("dashboard.views.fetch_current_weather", return_value={"temperature_c": 30.5, "humidity_pct": 82.0})
    def test_shows_live_weather_even_with_no_active_flock(self, mock_fetch):
        response = self.client.get("/")
        self.assertContains(response, "30.5")


@override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
class DashboardFreeRangeTests(TestCase):
    """While a flock is free-range in the field (is_caged=False), the dashboard must
    show nothing but a status notice — no stale KPIs/forecast/trend/records from
    before the flock went out to the field."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    def test_free_range_flock_shows_banner_and_hides_forecast_data(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        log = DailyLog.objects.create(
            flock=flock, date=date.today(), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        forecast = Forecast.objects.create(
            flock=flock, forecast_date=date.today(),
            predicted_daily_yield=Decimal("152.00"),
            predicted_tri_day_yield=Decimal("455.00"),
            feature_importances={"temperature_c": 0.4},
            model_version="rf-test",
        )
        forecast.source_logs.set([log])

        response = self.client.get("/")
        self.assertContains(response, "free-range in the field")
        self.assertNotContains(response, "3-Day Egg Yield Outlook")
        self.assertNotContains(response, "455.00")
        self.assertNotContains(response, "Recent Farm Records")

    def test_re_caging_the_flock_brings_the_summary_back(self):
        flock = Flock.objects.create(generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        DailyLog.objects.create(
            flock=flock, date=date.today(), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )

        flock.is_caged = True
        flock.save(update_fields=["is_caged"])

        response = self.client.get("/")
        self.assertNotContains(response, "free-range in the field")
        self.assertContains(response, "Recent Farm Records")
