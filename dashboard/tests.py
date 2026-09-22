from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from farm.models import DailyLog, Flock
from farm.services import operational_today
from forecasting.models import Forecast

User = get_user_model()


@override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
class DashboardIndexTests(TestCase):
    """Covers the "Next 3-Day Forecast" panel: 3 distinct day-by-day numbers
    (predicted_next_day1/2/3_yield), not the single predicted_tri_day_yield sum."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    def test_no_active_flock_shows_no_forecast_placeholder(self):
        response = self.client.get("/")
        self.assertContains(response, "No forecasts generated yet.")

    def test_freshly_registered_flock_shows_details_before_first_daily_log(self):
        """A brand-new flock has pending_* details staged at registration but no
        DailyLog yet -- the dashboard must show those details immediately rather than
        "—" placeholders until the farmer's first daily entry."""
        Flock.objects.create(
            owner=self.user, generation_number=1, started_on=date.today(),
            pending_flock_size=240, pending_flock_age_weeks=25, pending_feed_intake_kg=Decimal("40.0"),
        )
        response = self.client.get("/")
        self.assertEqual(response.context["current_age_weeks"], 25)
        self.assertContains(response, "240")

    def test_shows_three_next_day_forecasts(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        log = DailyLog.objects.create(
            flock=flock, date=date.today(), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        forecast = Forecast.objects.create(
            flock=flock, forecast_date=date.today(),
            predicted_daily_yield=Decimal("152.00"),
            predicted_tri_day_yield=Decimal("455.00"),
            predicted_next_day1_yield=Decimal("157.00"),
            predicted_next_day2_yield=Decimal("155.00"),
            predicted_next_day3_yield=Decimal("172.00"),
            feature_importances={"temperature_c": 0.4},
            model_version="rf-test",
        )
        forecast.source_logs.set([log])

        response = self.client.get("/")
        self.assertContains(response, "Next 3-Day Forecast")
        self.assertContains(response, "Tomorrow")
        self.assertContains(response, "157")
        self.assertContains(response, "155")
        self.assertContains(response, "172")


@override_settings(FARM_LATITUDE=None, FARM_LONGITUDE=None)
class DashboardDailyLogReminderBannerTests(TestCase):
    """The dashboard's "log today's data" banner is computed live from logged_today/
    flock_is_caged (see dashboard/views.py) -- it must show whenever there's an
    active, caged flock with no DailyLog for today, and never once one exists, or
    while the flock is free-range/nonexistent (nothing to log yet either way)."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    def test_banner_shown_when_active_caged_flock_has_no_log_today(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        response = self.client.get("/")
        self.assertContains(response, "haven't logged today's data yet")

    def test_banner_hidden_once_logged_today(self):
        # operational_today(), not date.today(): the dashboard's logged_today check
        # compares against the farm's current logging day, which rolls over at 6am
        # rather than midnight (farm.services.operational_today).
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=operational_today(), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        response = self.client.get("/")
        self.assertNotContains(response, "haven't logged today's data yet")

    def test_banner_hidden_for_free_range_flock(self):
        Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        response = self.client.get("/")
        self.assertNotContains(response, "haven't logged today's data yet")

    def test_banner_hidden_with_no_active_flock(self):
        response = self.client.get("/")
        self.assertNotContains(response, "haven't logged today's data yet")


class DashboardFlockAgeTests(TestCase):
    """Covers the "Flocks Age" card's current_age_weeks -- must be today_log.flock_age_weeks
    projected forward by calendar weeks elapsed, not the raw stale snapshot, since
    today_log can be several weeks old (itikcare-spec.md section 10)."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")

    @patch("farm.services.timezone")
    def test_age_card_projects_forward_from_a_stale_log(self, mock_timezone):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2023, 1, 1))
        DailyLog.objects.create(
            flock=flock, date=date(2024, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=94, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        mock_timezone.localdate.return_value = date(2024, 2, 12)  # exactly 6 weeks (42 days) later
        mock_timezone.localtime.return_value = datetime(2024, 2, 12, 12, 0)  # same day, past the 6am rollover
        response = self.client.get("/")
        self.assertEqual(response.context["current_age_weeks"], 100)
        self.assertContains(response, "100")


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
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
        log = DailyLog.objects.create(
            flock=flock, date=date.today(), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        forecast = Forecast.objects.create(
            flock=flock, forecast_date=date.today(),
            predicted_daily_yield=Decimal("152.00"),
            predicted_tri_day_yield=Decimal("455.00"),
            predicted_next_day1_yield=Decimal("157.00"),
            predicted_next_day2_yield=Decimal("155.00"),
            predicted_next_day3_yield=Decimal("172.00"),
            feature_importances={"temperature_c": 0.4},
            model_version="rf-test",
        )
        forecast.source_logs.set([log])

        response = self.client.get("/")
        self.assertContains(response, "free-range in the field")
        self.assertNotContains(response, "Next 3-Day Forecast")
        self.assertNotContains(response, "157.00")
        self.assertNotContains(response, "Recent Farm Records")

    def test_re_caging_the_flock_brings_the_summary_back(self):
        flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1), is_caged=False)
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


class RobotsTxtTests(TestCase):
    """A missing robots.txt would be treated as "allow all" anyway, but returning a
    real one avoids the 404 Search Console otherwise flags -- see dashboard.views.
    robots_txt's docstring."""

    def test_allows_crawling_and_needs_no_login(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertIn("Allow: /", response.content.decode())


class GoogleSiteVerificationTests(TestCase):
    """Google Search Console's "HTML file" verification method fetches this exact
    filename at the site root and checks its contents -- see dashboard.views.
    google_site_verification's docstring."""

    def test_serves_verification_file_and_needs_no_login(self):
        response = self.client.get("/google6c0aee7b83489d73.html")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertIn("google-site-verification: google6c0aee7b83489d73.html", response.content.decode())


class LandingPageSeoTagsTests(TestCase):
    """The anonymous landing page (not the dashboard.views.index the same URL renders
    for a logged-in farmer) is what a brand-name search like "itikcare" would surface
    -- covers the meta description/Open Graph tags that control how that result looks
    in a search snippet or a shared link preview."""

    def test_anonymous_visitor_gets_a_description_and_absolute_og_image(self):
        response = self.client.get("/")
        content = response.content.decode()
        self.assertIn('<meta name="description" content="ItikCare forecasts', content)
        self.assertIn('<meta property="og:title"', content)
        # og:image is read by an external crawler (not this browser), so it must be an
        # absolute URL -- {% static %} alone would render a host-relative one.
        self.assertIn('<meta property="og:image" content="http://testserver/static/images/hero-ducks.jpg">', content)
