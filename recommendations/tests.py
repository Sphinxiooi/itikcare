"""Tests for the prescriptive rule engine (itikcare-spec.md section 6, Annex-A-Rules-Table.pdf).

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
        self.assertEqual(rules.classify_flock_age(19), Priority.MEDIUM_LOW)  # boundary: >18 includes 19
        self.assertEqual(rules.classify_flock_age(18), Priority.LOW)  # boundary: <=18
        self.assertEqual(rules.classify_flock_age(10), Priority.LOW)


class TemperatureClassificationTests(SimpleTestCase):
    def test_tier_boundaries(self):
        self.assertEqual(rules.classify_temperature(34.0), Priority.HIGH)
        self.assertEqual(rules.classify_temperature(33.0), Priority.HIGH)
        self.assertEqual(rules.classify_temperature(31.0), Priority.MEDIUM_HIGH)
        self.assertEqual(rules.classify_temperature(28.0), Priority.MEDIUM_HIGH)  # boundary: >27 includes 28
        self.assertEqual(rules.classify_temperature(27.0), Priority.MEDIUM)  # boundary: >27 excludes 27
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
    """Feed-per-bird thresholds are age-tier-dependent (Annex A 1.4)."""

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

    def test_medium_low_age_tier_thresholds(self):
        # Annex A 1.4: MEDIUM-LOW age -> <110 underfed, 110-150 on track, >=150 overfed.
        self.assertEqual(rules.classify_feed(Priority.MEDIUM_LOW, 100), "underfed")
        self.assertEqual(rules.classify_feed(Priority.MEDIUM_LOW, 145), "on_track")  # was "overfed" pre-Annex (140 floor)
        self.assertEqual(rules.classify_feed(Priority.MEDIUM_LOW, 150), "overfed")

    def test_high_age_tier_thresholds(self):
        self.assertEqual(rules.classify_feed(Priority.HIGH, 80), "underfed")
        self.assertEqual(rules.classify_feed(Priority.HIGH, 100), "on_track")
        self.assertEqual(rules.classify_feed(Priority.HIGH, 130), "overfed")


class ImportanceForTests(SimpleTestCase):
    def test_raw_feature_key_returns_its_own_importance(self):
        self.assertEqual(rules.importance_for("feed_intake_kg", {"feed_intake_kg": 0.3}), 0.3)

    def test_temperature_and_humidity_keys_return_their_own_importance(self):
        importances = {"temperature_c": 0.1, "humidity_pct": 0.4}
        self.assertEqual(rules.importance_for("temperature_c", importances), 0.1)
        self.assertEqual(rules.importance_for("humidity_pct", importances), 0.4)

    def test_unknown_key_defaults_to_zero(self):
        self.assertEqual(rules.importance_for("flock_size", {}), 0.0)


class FeedRecommendationSlotTests(SimpleTestCase):
    """The feed_intake_kg slot: age tier x feed tier -> AF1-AF15 (Annex A 2.1)."""

    def _feed_rule(self, **overrides):
        fired = rules.evaluate_rules(_inputs(**overrides), {})
        return next(f for f in fired if f.feature == "feed_intake_kg")

    def test_low_age_underfed_matches_af1(self):
        # age 10wk (LOW tier), 40g/bird/day (< 70g underfed ceiling for LOW).
        rule = self._feed_rule(flock_age_weeks=10, feed_intake_kg=10.0, flock_size=250)
        self.assertEqual(rule.priority, Priority.HIGH)
        self.assertEqual(rule.status, "Underfed")
        self.assertIn("70–110 grams per bird every day", rule.action_text)
        self.assertIn("lay eggs late", rule.action_text)

    def test_high_age_overfed_matches_af15(self):
        # age 85wk (HIGH tier), 200g/bird/day (>= 130g overfed floor for HIGH).
        rule = self._feed_rule(flock_age_weeks=85, feed_intake_kg=50.0, flock_size=250)
        self.assertEqual(rule.priority, Priority.MEDIUM)
        self.assertEqual(rule.status, "Overfed")
        self.assertIn("replacing the flock", rule.action_text)

    def test_on_track_is_low_priority(self):
        rule = self._feed_rule()  # baseline: 168 g/bird/day at MEDIUM age -> on track
        self.assertEqual(rule.priority, Priority.LOW)
        self.assertEqual(rule.status, "On Track")
        self.assertIn("Keep the same feed during peak egg-laying time", rule.action_text)


class FlockAgeRecommendationSlotTests(SimpleTestCase):
    """The flock_age_weeks slot: age tier alone -> retirement/replacement text (2.6)."""

    def _age_rule(self, **overrides):
        fired = rules.evaluate_rules(_inputs(**overrides), {})
        return next(f for f in fired if f.feature == "flock_age_weeks")

    def test_high_tier_recommends_retirement(self):
        rule = self._age_rule(flock_age_weeks=85)
        self.assertEqual(rule.priority, Priority.HIGH)
        self.assertEqual(rule.status, "High")
        self.assertIn("Retire or cull the layers that are slowing down", rule.action_text)

    def test_medium_high_tier_recommends_raising_replacement(self):
        rule = self._age_rule(flock_age_weeks=60)
        self.assertEqual(rule.priority, Priority.MEDIUM_HIGH)
        self.assertEqual(rule.status, "Medium-High")
        self.assertIn("Start raising your replacement ducks now", rule.action_text)

    def test_low_tier_needs_no_action(self):
        rule = self._age_rule(flock_age_weeks=10)
        self.assertEqual(rule.priority, Priority.LOW)
        self.assertEqual(rule.status, "Low")
        self.assertIn("No need for retirement or replacement action yet", rule.action_text)


class TemperatureRecommendationSlotTests(SimpleTestCase):
    """The temperature_c slot: temperature tier x humidity tier -> temperature flag (2.2/2.4)."""

    def _temp_rule(self, **overrides):
        fired = rules.evaluate_rules(_inputs(**overrides), {})
        return next(f for f in fired if f.feature == "temperature_c")

    def test_matching_severe_heat_and_humidity_is_high(self):
        rule = self._temp_rule(temperature_c=34.0, humidity_pct=90.0)
        self.assertEqual(rule.priority, Priority.HIGH)
        self.assertIn("very high risk of heat stress", rule.action_text)

    def test_within_comfort_range_is_medium_priority_confirmation(self):
        rule = self._temp_rule(temperature_c=27.0, humidity_pct=60.0)
        self.assertEqual(rule.priority, Priority.MEDIUM)
        self.assertIn("This is a comfortable temperature", rule.action_text)

    def test_asymmetric_matrix_uses_both_readings(self):
        # temperature_c=20.0 -> MEDIUM_LOW tier; humidity_pct=90.0 -> HIGH tier.
        # Temperature flag matrix[(MEDIUM_LOW, HIGH)] = MEDIUM_HIGH -- driven by both
        # readings even though only temperature_c is the triggered_by key here.
        rule = self._temp_rule(temperature_c=20.0, humidity_pct=90.0)
        self.assertEqual(rule.priority, Priority.MEDIUM_HIGH)
        self.assertIn("heat stress is starting to rise", rule.action_text)


class HumidityRecommendationSlotTests(SimpleTestCase):
    """The humidity_pct slot: humidity tier x temperature tier -> humidity flag (2.3/2.5)."""

    def _humidity_rule(self, **overrides):
        fired = rules.evaluate_rules(_inputs(**overrides), {})
        return next(f for f in fired if f.feature == "humidity_pct")

    def test_matching_severe_heat_and_humidity_is_high(self):
        rule = self._humidity_rule(temperature_c=34.0, humidity_pct=90.0)
        self.assertEqual(rule.priority, Priority.HIGH)
        self.assertIn("too much moisture", rule.action_text)

    def test_within_comfort_range_is_medium_priority_confirmation(self):
        rule = self._humidity_rule(temperature_c=27.0, humidity_pct=60.0)
        self.assertEqual(rule.priority, Priority.MEDIUM)
        self.assertIn("This is an acceptable range", rule.action_text)

    def test_asymmetric_matrix_uses_both_readings(self):
        # humidity_pct=90.0 -> HIGH tier; temperature_c=20.0 -> MEDIUM_LOW tier.
        # Humidity flag matrix[(HIGH, MEDIUM_LOW)] = HIGH -- worse than the temperature
        # slot's own MEDIUM_HIGH result for the same reading pair (see the test above),
        # proving the two matrices are independently asymmetric, not read from one table.
        rule = self._humidity_rule(temperature_c=20.0, humidity_pct=90.0)
        self.assertEqual(rule.priority, Priority.HIGH)
        self.assertIn("too much moisture", rule.action_text)


class EvaluateRulesOrderingTests(SimpleTestCase):
    def test_fired_slots_are_sorted_by_importance_descending(self):
        # temperature_c (0.5) > humidity_pct (0.2) > feed_intake_kg (0.1) > flock_age_weeks (0.05).
        importances = {
            "temperature_c": 0.5,
            "humidity_pct": 0.2,
            "feed_intake_kg": 0.1,
            "flock_age_weeks": 0.05,
            "flock_size": 0.15,
        }
        fired = rules.evaluate_rules(_inputs(), importances)
        self.assertEqual(
            [f.feature for f in fired],
            ["temperature_c", "humidity_pct", "feed_intake_kg", "flock_age_weeks"],
        )

    def test_always_fires_exactly_four_slots(self):
        fired = rules.evaluate_rules(_inputs(), {})
        self.assertEqual(
            {f.feature for f in fired},
            {"feed_intake_kg", "flock_age_weeks", "temperature_c", "humidity_pct"},
        )


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

        self.assertEqual(
            set(by_feature), {"temperature_c", "humidity_pct", "feed_intake_kg", "flock_age_weeks"}
        )
        # temperature: temp HIGH tier x humidity MEDIUM tier -> temperature flag matrix -> HIGH.
        self.assertEqual(by_feature["temperature_c"].priority, Priority.HIGH)
        # humidity: humidity MEDIUM tier x temp HIGH tier -> humidity flag matrix -> MEDIUM.
        self.assertEqual(by_feature["humidity_pct"].priority, Priority.MEDIUM)
        # feed: MEDIUM_HIGH age tier, 80g/bird/day < 110g ceiling -> underfed -> HIGH.
        self.assertEqual(by_feature["feed_intake_kg"].priority, Priority.HIGH)
        # age: 60wk -> MEDIUM_HIGH tier.
        self.assertEqual(by_feature["flock_age_weeks"].priority, Priority.MEDIUM_HIGH)

    def test_output_order_matches_feature_importance_descending(self):
        # temperature_c (0.40) > feed (0.30) > age (0.15) > humidity_pct (0.10).
        created = generate_recommendations(self.forecast)
        self.assertEqual(
            [r.triggered_by for r in created],
            ["temperature_c", "feed_intake_kg", "flock_age_weeks", "humidity_pct"],
        )

    def test_status_learn_more_and_reading_summary_are_populated(self):
        created = generate_recommendations(self.forecast)
        by_feature = {r.triggered_by: r for r in created}

        feed_rec = by_feature["feed_intake_kg"]
        self.assertEqual(feed_rec.status, "Underfed")
        self.assertTrue(feed_rec.learn_more)
        self.assertIn("g/bird/day", feed_rec.reading_summary)

        temp_rec = by_feature["temperature_c"]
        self.assertEqual(temp_rec.status, "High")
        self.assertTrue(temp_rec.learn_more)
        self.assertIn("°C", temp_rec.reading_summary)

    def test_regeneration_is_idempotent(self):
        generate_recommendations(self.forecast)
        generate_recommendations(self.forecast)
        self.assertEqual(self.forecast.recommendations.count(), 4)

    def test_no_source_logs_yields_no_recommendations(self):
        self.forecast.source_logs.clear()
        self.assertEqual(generate_recommendations(self.forecast), [])
