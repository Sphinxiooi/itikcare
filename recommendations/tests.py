"""Tests for the prescriptive rule engine (itikcare-spec.md section 6).

Split like ``forecasting/tests.py``: pure rule-evaluation logic is tested against
plain dicts with ``SimpleTestCase`` (no DB), and the DB-backed orchestration in
``engine.generate_recommendations`` gets its own ``TestCase``.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from farm.models import DailyLog, Flock
from forecasting.models import Forecast
from recommendations import rules
from recommendations.engine import generate_recommendations
from recommendations.models import Recommendation

User = get_user_model()


def _inputs(**overrides):
    """A baseline "nothing wrong" reading; tests override just the field under test."""
    base = {
        "flock_age_weeks": 32,  # inside the 28-35 plateau: no age rule fires
        "feed_intake_kg": 42.0,
        "flock_size": 250,  # -> 0.168 kg/bird/day, above both feed thresholds
        "temperature_c": 27.0,
        "humidity_pct": 70.0,
    }
    base.update(overrides)
    return base


class TemperatureRuleTests(SimpleTestCase):
    def test_normal_temperature_fires_low_confirmation(self):
        fired = rules.evaluate_rules(_inputs(temperature_c=29.0), ["temperature_c"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.LOW)

    def test_moderate_heat_fires_medium(self):
        fired = rules.evaluate_rules(_inputs(temperature_c=31.0), ["temperature_c"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.MEDIUM)

    def test_severe_heat_fires_high_not_moderate(self):
        fired = rules.evaluate_rules(_inputs(temperature_c=33.0), ["temperature_c"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.HIGH)


class HumidityRuleTests(SimpleTestCase):
    def test_normal_humidity_fires_low_confirmation(self):
        fired = rules.evaluate_rules(_inputs(humidity_pct=70.0), ["humidity_pct"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.LOW)

    def test_moderate_humidity_fires_medium(self):
        fired = rules.evaluate_rules(_inputs(humidity_pct=82.0), ["humidity_pct"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.MEDIUM)

    def test_severe_humidity_fires_high(self):
        fired = rules.evaluate_rules(_inputs(humidity_pct=90.0), ["humidity_pct"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.HIGH)


class FeedIntakeRuleTests(SimpleTestCase):
    def test_adequate_feed_fires_low_confirmation(self):
        # 42.0 / 250 = 0.168 kg/bird/day, above both thresholds.
        fired = rules.evaluate_rules(_inputs(), ["feed_intake_kg"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.LOW)

    def test_moderate_underfeeding_fires_medium(self):
        # 38.0 / 250 = 0.152 kg/bird/day: below moderate (0.160), above severe (0.150).
        fired = rules.evaluate_rules(_inputs(feed_intake_kg=38.0), ["feed_intake_kg"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.MEDIUM)

    def test_severe_underfeeding_fires_high(self):
        # 35.0 / 250 = 0.14 kg/bird/day: below severe threshold.
        fired = rules.evaluate_rules(_inputs(feed_intake_kg=35.0), ["feed_intake_kg"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.HIGH)


class FlockAgeRuleTests(SimpleTestCase):
    def test_plateau_fires_low_confirmation(self):
        fired = rules.evaluate_rules(_inputs(flock_age_weeks=30), ["flock_age_weeks"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.LOW)

    def test_pre_peak_fires_low(self):
        fired = rules.evaluate_rules(_inputs(flock_age_weeks=20), ["flock_age_weeks"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.LOW)

    def test_post_peak_decline_fires_medium(self):
        fired = rules.evaluate_rules(_inputs(flock_age_weeks=60), ["flock_age_weeks"])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].priority, Recommendation.Priority.MEDIUM)


class ImportanceOrderingTests(SimpleTestCase):
    def test_fired_rules_follow_importance_order_not_dict_order(self):
        # Both temperature and humidity are in stress; humidity is listed first in
        # importance_order, so it must come first in the output regardless of RULES
        # dict insertion order.
        inputs = _inputs(temperature_c=33.0, humidity_pct=90.0)
        fired = rules.evaluate_rules(inputs, ["humidity_pct", "temperature_c"])
        self.assertEqual([f.feature for f in fired], ["humidity_pct", "temperature_c"])

    def test_features_without_rules_are_skipped_silently(self):
        fired = rules.evaluate_rules(_inputs(), ["flock_size", "lag1", "roll3"])
        self.assertEqual(fired, [])


class GenerateRecommendationsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        self.log = DailyLog.objects.create(
            flock=self.flock,
            date=date(2024, 2, 1),
            flock_size=250,
            caging_period=1,
            flock_age_weeks=60,  # past-peak -> medium age recommendation
            egg_count=200,
            feed_intake_kg=35.0,  # severe underfeeding
            temperature_c=33.0,  # severe heat
            humidity_pct=70.0,  # in range -> low-priority confirmation
            recorded_by=self.user,
        )
        self.forecast = Forecast.objects.create(
            flock=self.flock,
            forecast_date=date(2024, 2, 2),
            predicted_daily_yield=200,
            predicted_tri_day_yield=600,
            feature_importances={
                "temperature_c": 0.40,
                "feed_intake_kg": 0.30,
                "flock_age_weeks": 0.15,
                "humidity_pct": 0.10,
                "flock_size": 0.05,
            },
            model_version="rf-test",
        )
        self.forecast.source_logs.set([self.log])

    def test_creates_recommendations_traceable_to_the_triggering_feature(self):
        created = generate_recommendations(self.forecast)
        by_feature = {r.triggered_by: r for r in created}

        self.assertEqual(
            set(by_feature), {"temperature_c", "feed_intake_kg", "flock_age_weeks", "humidity_pct"}
        )
        self.assertEqual(by_feature["temperature_c"].priority, Recommendation.Priority.HIGH)
        self.assertEqual(by_feature["feed_intake_kg"].priority, Recommendation.Priority.HIGH)
        self.assertEqual(by_feature["flock_age_weeks"].priority, Recommendation.Priority.MEDIUM)
        self.assertEqual(by_feature["humidity_pct"].priority, Recommendation.Priority.LOW)

    def test_output_order_matches_feature_importance_descending(self):
        created = generate_recommendations(self.forecast)
        self.assertEqual(
            [r.triggered_by for r in created],
            ["temperature_c", "feed_intake_kg", "flock_age_weeks", "humidity_pct"],
        )

    def test_regeneration_is_idempotent(self):
        generate_recommendations(self.forecast)
        generate_recommendations(self.forecast)
        self.assertEqual(self.forecast.recommendations.count(), 4)

    def test_no_source_logs_yields_no_recommendations(self):
        self.forecast.source_logs.clear()
        self.assertEqual(generate_recommendations(self.forecast), [])
