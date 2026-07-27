"""Tests for the prescriptive rule engine (itikcare-spec.md section 6, rules-table.pdf).

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
Priority = Recommendation.Priority


def _inputs(**overrides):
    """A baseline reading landing in the middle of every tier: age 40wk (MEDIUM),
    140g/bird/day feed (on track for MEDIUM age's 115-160g range), 27C/60% (both
    MEDIUM/normal). Tests override just the field(s) under test.
    """
    base = {
        "flock_age_weeks": 40,
        "feed_intake_kg": 35.0,
        "flock_size": 250,  # -> 140 g/bird/day
        "temperature_c": 27.0,
        "humidity_pct": 60.0,
    }
    base.update(overrides)
    return base


class FlockAgeClassificationTests(SimpleTestCase):
    def test_tier_boundaries(self):
        self.assertEqual(rules.classify_flock_age(85), Priority.HIGH)
        self.assertEqual(rules.classify_flock_age(80), Priority.HIGH)
        self.assertEqual(rules.classify_flock_age(60), Priority.MEDIUM_HIGH)
        self.assertEqual(rules.classify_flock_age(52), Priority.MEDIUM)  # boundary: >52 excludes 52
        self.assertEqual(rules.classify_flock_age(40), Priority.MEDIUM)
        self.assertEqual(rules.classify_flock_age(26), Priority.MEDIUM_LOW)  # boundary: >26 excludes 26
        self.assertEqual(rules.classify_flock_age(22), Priority.MEDIUM_LOW)
        self.assertEqual(rules.classify_flock_age(19), Priority.LOW)  # boundary: <=19
        self.assertEqual(rules.classify_flock_age(10), Priority.LOW)


class TemperatureClassificationTests(SimpleTestCase):
    def test_tier_boundaries(self):
        self.assertEqual(rules.classify_temperature(34.0), Priority.HIGH)
        self.assertEqual(rules.classify_temperature(33.0), Priority.HIGH)
        self.assertEqual(rules.classify_temperature(31.0), Priority.MEDIUM_HIGH)
        self.assertEqual(rules.classify_temperature(30.0), Priority.MEDIUM_HIGH)
        self.assertEqual(rules.classify_temperature(27.0), Priority.MEDIUM)
        self.assertEqual(rules.classify_temperature(23.0), Priority.MEDIUM_LOW)  # boundary: >23 excludes 23
        self.assertEqual(rules.classify_temperature(20.0), Priority.MEDIUM_LOW)
        self.assertEqual(rules.classify_temperature(18.0), Priority.LOW)
        self.assertEqual(rules.classify_temperature(15.0), Priority.LOW)


class HumidityClassificationTests(SimpleTestCase):
    def test_tier_boundaries(self):
        self.assertEqual(rules.classify_humidity(90.0), Priority.HIGH)
        self.assertEqual(rules.classify_humidity(88.0), Priority.HIGH)
        self.assertEqual(rules.classify_humidity(80.0), Priority.MEDIUM_HIGH)
        self.assertEqual(rules.classify_humidity(70.0), Priority.MEDIUM)  # boundary: >70 excludes 70
        self.assertEqual(rules.classify_humidity(60.0), Priority.MEDIUM)
        self.assertEqual(rules.classify_humidity(50.0), Priority.MEDIUM)
        self.assertEqual(rules.classify_humidity(45.0), Priority.MEDIUM_LOW)
        self.assertEqual(rules.classify_humidity(40.0), Priority.LOW)  # boundary: >40 excludes 40
        self.assertEqual(rules.classify_humidity(30.0), Priority.LOW)


class FeedClassificationTests(SimpleTestCase):
    """Feed-per-bird thresholds are age-tier-dependent (rules-table.pdf 1.4)."""

    def test_low_age_tier_thresholds(self):
        self.assertEqual(rules.classify_feed(Priority.LOW, 50), "underfed")
        self.assertEqual(rules.classify_feed(Priority.LOW, 90), "on_track")
        self.assertEqual(rules.classify_feed(Priority.LOW, 130), "overfed")

    def test_medium_age_tier_thresholds(self):
        # Same 90g/bird/day reading that was "on track" at LOW age is "underfed" at
        # MEDIUM age -- proving thresholds actually shift per age tier, not a shared
        # single threshold.
        self.assertEqual(rules.classify_feed(Priority.MEDIUM, 90), "underfed")
        self.assertEqual(rules.classify_feed(Priority.MEDIUM, 140), "on_track")
        self.assertEqual(rules.classify_feed(Priority.MEDIUM, 170), "overfed")

    def test_high_age_tier_thresholds(self):
        self.assertEqual(rules.classify_feed(Priority.HIGH, 80), "underfed")
        self.assertEqual(rules.classify_feed(Priority.HIGH, 100), "on_track")
        self.assertEqual(rules.classify_feed(Priority.HIGH, 130), "overfed")


class ImportanceForTests(SimpleTestCase):
    def test_raw_feature_key_returns_its_own_importance(self):
        self.assertEqual(rules.importance_for("feed_intake_kg", {"feed_intake_kg": 0.3}), 0.3)

    def test_environment_key_returns_the_higher_of_temp_and_humidity(self):
        importances = {"temperature_c": 0.1, "humidity_pct": 0.4}
        self.assertEqual(rules.importance_for("environment", importances), 0.4)

    def test_unknown_key_defaults_to_zero(self):
        self.assertEqual(rules.importance_for("flock_size", {}), 0.0)


class FeedRecommendationSlotTests(SimpleTestCase):
    """The feed_intake_kg slot: age tier x feed tier -> AF1-AF15 (rules-table.pdf 2.1)."""

    def _feed_rule(self, **overrides):
        fired = rules.evaluate_rules(_inputs(**overrides), {})
        return next(f for f in fired if f.feature == "feed_intake_kg")

    def test_low_age_underfed_matches_af1(self):
        # age 10wk (LOW tier), 40g/bird/day (< 70g underfed ceiling for LOW).
        rule = self._feed_rule(flock_age_weeks=10, feed_intake_kg=10.0, flock_size=250)
        self.assertEqual(rule.priority, Priority.HIGH)
        self.assertIn("70-110 g/bird/day", rule.message)
        self.assertIn("delays growth and laying maturity", rule.message)

    def test_high_age_overfed_matches_af15(self):
        # age 85wk (HIGH tier), 200g/bird/day (>= 130g overfed floor for HIGH).
        rule = self._feed_rule(flock_age_weeks=85, feed_intake_kg=50.0, flock_size=250)
        self.assertEqual(rule.priority, Priority.MEDIUM)
        self.assertIn("evaluating flock replacement", rule.message)

    def test_on_track_is_low_priority(self):
        rule = self._feed_rule()  # baseline: 168 g/bird/day at MEDIUM age -> on track
        self.assertEqual(rule.priority, Priority.LOW)
        self.assertIn("Maintain current ration through peak production", rule.message)


class FlockAgeRecommendationSlotTests(SimpleTestCase):
    """The flock_age_weeks slot: age tier alone -> retirement/replacement text (2.6)."""

    def _age_rule(self, **overrides):
        fired = rules.evaluate_rules(_inputs(**overrides), {})
        return next(f for f in fired if f.feature == "flock_age_weeks")

    def test_high_tier_recommends_retirement(self):
        rule = self._age_rule(flock_age_weeks=85)
        self.assertEqual(rule.priority, Priority.HIGH)
        self.assertIn("Retire or cull declining layers", rule.message)

    def test_medium_high_tier_recommends_raising_replacement(self):
        rule = self._age_rule(flock_age_weeks=60)
        self.assertEqual(rule.priority, Priority.MEDIUM_HIGH)
        self.assertIn("Start raising a replacement cohort now", rule.message)

    def test_low_tier_needs_no_action(self):
        rule = self._age_rule(flock_age_weeks=10)
        self.assertEqual(rule.priority, Priority.LOW)
        self.assertIn("No retirement/replacement action relevant yet", rule.message)


class EnvironmentalRecommendationSlotTests(SimpleTestCase):
    """The environment slot merges temperature+humidity via two asymmetric matrices
    (2.2 temperature flag, 2.3 humidity flag) -- worse tier wins, both texts included.
    """

    def _env_rule(self, **overrides):
        fired = rules.evaluate_rules(_inputs(**overrides), {})
        return next(f for f in fired if f.feature == "environment")

    def test_matching_severe_heat_and_humidity_is_high(self):
        rule = self._env_rule(temperature_c=34.0, humidity_pct=90.0)
        self.assertEqual(rule.priority, Priority.HIGH)
        self.assertIn("Severe heat stress risk", rule.message)
        self.assertIn("Severe moisture stress", rule.message)

    def test_within_comfort_range_is_low_priority_confirmation(self):
        rule = self._env_rule(temperature_c=27.0, humidity_pct=60.0)
        self.assertEqual(rule.priority, Priority.MEDIUM)
        self.assertIn("Within acceptable comfort range", rule.message)
        self.assertIn("Within acceptable range", rule.message)

    def test_asymmetric_matrices_take_the_worse_of_the_two_flags(self):
        # temperature_c=20.0 -> MEDIUM_LOW tier; humidity_pct=90.0 -> HIGH tier.
        # Temperature flag matrix[(MEDIUM_LOW, HIGH)] = MEDIUM_HIGH, but humidity flag
        # matrix[(HIGH, MEDIUM_LOW)] = HIGH -- the two matrices disagree, and HIGH (the
        # more severe of the two) must win, with both texts present in the message.
        rule = self._env_rule(temperature_c=20.0, humidity_pct=90.0)
        self.assertEqual(rule.priority, Priority.HIGH)
        self.assertIn("Elevated heat stress", rule.message)
        self.assertIn("Severe moisture stress", rule.message)


class EvaluateRulesOrderingTests(SimpleTestCase):
    def test_fired_slots_are_sorted_by_importance_descending(self):
        # environment (max(temp, humidity) = 0.5) > feed_intake_kg (0.1) > flock_age_weeks (0.05).
        importances = {
            "temperature_c": 0.5,
            "humidity_pct": 0.2,
            "feed_intake_kg": 0.1,
            "flock_age_weeks": 0.05,
            "flock_size": 0.15,
        }
        fired = rules.evaluate_rules(_inputs(), importances)
        self.assertEqual(
            [f.feature for f in fired], ["environment", "feed_intake_kg", "flock_age_weeks"]
        )

    def test_always_fires_exactly_three_slots(self):
        fired = rules.evaluate_rules(_inputs(), {})
        self.assertEqual({f.feature for f in fired}, {"feed_intake_kg", "flock_age_weeks", "environment"})


class GenerateRecommendationsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        self.log = DailyLog.objects.create(
            flock=self.flock,
            date=date(2024, 2, 1),
            flock_size=250,
            caging_period=1,
            flock_age_weeks=60,  # MEDIUM_HIGH tier
            egg_count=200,
            feed_intake_kg=20.0,  # 80 g/bird/day -> underfed at MEDIUM_HIGH's 110g ceiling
            temperature_c=33.0,  # HIGH tier
            humidity_pct=70.0,  # MEDIUM/normal tier
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

        self.assertEqual(set(by_feature), {"environment", "feed_intake_kg", "flock_age_weeks"})
        # environment: temp HIGH tier + humidity MEDIUM tier -> worse (HIGH) wins.
        self.assertEqual(by_feature["environment"].priority, Priority.HIGH)
        # feed: MEDIUM_HIGH age tier, 80g/bird/day < 110g ceiling -> underfed -> HIGH.
        self.assertEqual(by_feature["feed_intake_kg"].priority, Priority.HIGH)
        # age: 60wk -> MEDIUM_HIGH tier.
        self.assertEqual(by_feature["flock_age_weeks"].priority, Priority.MEDIUM_HIGH)

    def test_output_order_matches_feature_importance_descending(self):
        # environment = max(temp 0.40, humidity 0.10) = 0.40 > feed 0.30 > age 0.15.
        created = generate_recommendations(self.forecast)
        self.assertEqual(
            [r.triggered_by for r in created], ["environment", "feed_intake_kg", "flock_age_weeks"]
        )

    def test_regeneration_is_idempotent(self):
        generate_recommendations(self.forecast)
        generate_recommendations(self.forecast)
        self.assertEqual(self.forecast.recommendations.count(), 3)

    def test_no_source_logs_yields_no_recommendations(self):
        self.forecast.source_logs.clear()
        self.assertEqual(generate_recommendations(self.forecast), [])
