"""Unit tests for the pure forecasting pipeline logic (no DB required), plus the
DB-backed forecast-generation orchestration in services.py.

The pipeline tests target the two places where caging-period boundaries matter — the
tri-day target construction and the chronological split — since those are the
spec-section-10 rules the model's defensibility rests on.
"""

import random
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import joblib
import numpy as np
import pandas as pd
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, SimpleTestCase, TestCase

from farm.models import DailyLog, Flock
from forecasting import pipeline as ml
from forecasting import services
from forecasting.models import Forecast

User = get_user_model()


def _records(start, caging_period, n, egg_start=100, flock_id=1):
    """A run of `n` consecutive daily rows in one caging period."""
    return [
        {
            "date": start + timedelta(days=i),
            "flock_id": flock_id,
            "caging_period": caging_period,
            "flock_size": 250,
            "flock_age_weeks": 40,
            "feed_intake_kg": 38.5,
            "temperature_c": 26.0,
            "humidity_pct": 80.0,
            "egg_count": egg_start + i,
        }
        for i in range(n)
    ]


class TriDayTargetTests(SimpleTestCase):
    def test_forward_window_sums_three_consecutive_days(self):
        df = ml.build_feature_frame(_records(date(2024, 2, 1), 1, 5, egg_start=100))
        tri = ml.add_tri_day_target(df).sort_values("date").reset_index(drop=True)
        # First row: 100 + 101 + 102 = 303.
        self.assertEqual(tri.loc[0, ml.TRI_DAY_TARGET], 303)
        # Last two rows of the segment have no complete forward window -> dropped.
        self.assertEqual(len(tri), 3)

    def test_window_never_spans_a_caging_period_boundary(self):
        # Two caging periods; the last two rows of period 1 must NOT borrow period 2's days.
        recs = _records(date(2024, 2, 1), 1, 4, egg_start=100)
        recs += _records(date(2024, 5, 1), 2, 4, egg_start=200)
        df = ml.build_feature_frame(recs)
        tri = ml.add_tri_day_target(df)
        # Each 4-row segment yields exactly 2 valid windows (rows 0 and 1) -> 4 total.
        self.assertEqual(len(tri), 4)
        # Period 1 rows sum only from {100..103}; if any window had borrowed period 2's
        # 200-range days its total would jump into the 400s+. Staying in the 300s proves
        # no cross-boundary borrowing.
        period1 = tri[tri["caging_period"] == 1][ml.TRI_DAY_TARGET]
        self.assertTrue((period1 < 400).all())
        self.assertTrue((period1 >= 300).all())

    def test_internal_date_gap_breaks_the_window(self):
        recs = _records(date(2024, 2, 1), 1, 5, egg_start=100)
        del recs[2]  # remove 2024-02-03, leaving a one-day hole
        df = ml.build_feature_frame(recs)
        tri = ml.add_tri_day_target(df).sort_values("date")
        # Days whose forward window would cross the hole are excluded; the row on
        # 2024-02-01 (needs 02, 03) is invalid because 03 is missing.
        included = set(tri["date"].dt.date)
        self.assertNotIn(date(2024, 2, 1), included)
        self.assertNotIn(date(2024, 2, 2), included)


class LagFeatureTests(SimpleTestCase):
    def test_lag1_is_previous_day_and_first_rows_dropped(self):
        df = ml.build_feature_frame(_records(date(2024, 2, 1), 1, 6, egg_start=100))
        lagged = ml.add_lag_features(df).sort_values("date").reset_index(drop=True)
        # roll3 needs 3 prior days, so the first 3 rows of the segment are dropped.
        self.assertEqual(len(lagged), 3)
        self.assertEqual(lagged.loc[0, "date"].date(), date(2024, 2, 4))
        # lag1 on 2024-02-04 is the egg_count from 2024-02-03 (100 + 2 = 102).
        self.assertEqual(lagged.loc[0, "lag1"], 102)
        # roll3 is the mean of 2024-02-01..03 = mean(100,101,102) = 101.
        self.assertEqual(lagged.loc[0, "roll3"], 101)

    def test_lags_never_borrow_across_a_caging_period(self):
        recs = _records(date(2024, 2, 1), 1, 6, egg_start=100)
        recs += _records(date(2024, 6, 1), 2, 6, egg_start=500)
        lagged = ml.add_lag_features(ml.build_feature_frame(recs))
        # Period 2's first retained row must lag on period-2 days (500s), never period 1.
        period2 = lagged[lagged["caging_period"] == 2].sort_values("date")
        self.assertGreaterEqual(period2["lag1"].min(), 500)

    def test_lags_never_borrow_across_two_flocks_sharing_the_same_caging_period_number(self):
        """Multi-tenant land-mine: a second farm's own caging_period numbering can
        legitimately collide with another farm's (e.g. both start at 1). Without the
        flock_id-prefixed segment_key, groupby("caging_period") alone would treat two
        unrelated flocks' rows as one contiguous segment and leak lag1/roll3 between
        two different farms' data.
        """
        recs = _records(date(2024, 2, 1), 1, 4, egg_start=100, flock_id=1)
        # Same raw caging_period (1) as above, but a different flock, overlapping dates.
        recs += _records(date(2024, 2, 1), 1, 4, egg_start=900, flock_id=2)
        df = ml.build_feature_frame(recs)
        # The two flocks must land in different segments despite sharing caging_period.
        self.assertEqual(df["segment_key"].nunique(), 2)
        lagged = ml.add_lag_features(df)
        flock2 = lagged[lagged["flock_id"] == 2].sort_values("date")
        # flock 2's lag1 values must only ever come from flock 2's own 900s range,
        # never flock 1's 100s range.
        self.assertGreaterEqual(flock2["lag1"].min(), 900)


class ChronologicalSplitTests(SimpleTestCase):
    def test_test_set_holds_the_most_recent_days_per_segment(self):
        df = ml.build_feature_frame(_records(date(2024, 2, 1), 1, 10))
        train, test = ml.chronological_split(df, test_fraction=0.2)
        self.assertEqual(len(train), 8)
        self.assertEqual(len(test), 2)
        # Every test date is strictly after every train date within the segment.
        self.assertGreater(test["date"].min(), train["date"].max())

    def test_each_segment_is_represented_in_both_splits(self):
        recs = _records(date(2024, 2, 1), 1, 10) + _records(date(2024, 6, 1), 2, 10)
        df = ml.build_feature_frame(recs)
        train, test = ml.chronological_split(df, test_fraction=0.2)
        self.assertEqual(set(train["caging_period"]), {1, 2})
        self.assertEqual(set(test["caging_period"]), {1, 2})


class SegmentedTimeSeriesSplitsTests(SimpleTestCase):
    def test_validation_rows_are_always_after_training_rows_within_a_segment(self):
        recs = _records(date(2024, 2, 1), 1, 6) + _records(date(2024, 6, 1), 2, 6)
        df = ml.build_feature_frame(recs)
        folds = ml.segmented_time_series_splits(df, n_splits=2)
        self.assertEqual(len(folds), 2)
        for train_idx, val_idx in folds:
            train_rows = df.loc[train_idx]
            val_rows = df.loc[val_idx]
            for period, val_segment in val_rows.groupby("caging_period"):
                train_segment = train_rows[train_rows["caging_period"] == period]
                self.assertGreater(val_segment["date"].min(), train_segment["date"].max())

    def test_a_segment_too_small_for_validation_is_always_in_train_never_in_val(self):
        # Period 1 has plenty of rows; period 2 has only 3 -> below n_splits(=4)+1.
        recs = _records(date(2024, 2, 1), 1, 12) + _records(date(2024, 6, 1), 2, 3)
        df = ml.build_feature_frame(recs)
        folds = ml.segmented_time_series_splits(df, n_splits=4)
        small_segment_idx = set(df[df["caging_period"] == 2].index)
        for train_idx, val_idx in folds:
            self.assertTrue(small_segment_idx.issubset(set(train_idx)))
            self.assertFalse(small_segment_idx & set(val_idx))

    def test_folds_are_directly_usable_as_a_cv_argument(self):
        df = ml.build_feature_frame(_records(date(2024, 2, 1), 1, 12))
        folds = ml.segmented_time_series_splits(df, n_splits=3)
        # Every index appears exactly once as int-like positions covering the frame.
        for train_idx, val_idx in folds:
            self.assertTrue(set(train_idx) | set(val_idx) <= set(df.index))
            self.assertFalse(set(train_idx) & set(val_idx))


class TuneEstimatorTests(SimpleTestCase):
    def test_returns_a_fitted_pipeline_and_params_from_the_search_space(self):
        recs = _records(date(2024, 2, 1), 1, 20) + _records(date(2024, 6, 1), 2, 20)
        df = ml.add_lag_features(ml.build_feature_frame(recs))
        model, best_params = ml.tune_estimator(
            df, ml.MODEL_FEATURES, ml.DAILY_TARGET, n_splits=2, n_iter=3,
        )
        # The pipeline came back fitted and usable.
        preds = model.predict(df[ml.MODEL_FEATURES])
        self.assertEqual(len(preds), len(df))
        # Reported hyperparameters are drawn from the declared search space.
        for name, value in best_params.items():
            self.assertIn(value, ml.PARAM_DISTRIBUTIONS[name])


def _fit_stub_pipeline(n_estimators=5):
    """A real (tiny) fitted sklearn Pipeline, shaped exactly like a trained model
    artifact's daily_pipeline/tri_day_pipeline — synthetic data, not the real dataset,
    since these tests only need something .predict()-able, not an accurate model."""
    rng = np.random.RandomState(0)
    n = 30
    X = pd.DataFrame({feature: rng.uniform(1, 100, n) for feature in ml.MODEL_FEATURES})
    y = rng.uniform(100, 300, n)
    pipeline = ml.build_estimator(n_estimators=n_estimators)
    pipeline.fit(X, y)
    return pipeline


def _write_stub_artifact(path, daily_importances=None, tri_importances=None):
    """A minimal model artifact matching what train_forecast_model.py persists, for
    services.generate_forecast to load without needing a real training run."""
    if daily_importances is None:
        daily_importances = {feature: 1 / len(ml.MODEL_FEATURES) for feature in ml.MODEL_FEATURES}
    if tri_importances is None:
        tri_importances = dict(daily_importances)
    artifact = {
        "model_version": "rf-test",
        "trained_at": "2024-01-01T00:00:00",
        "feature_names": ml.MODEL_FEATURES,
        "n_samples": 30,
        "daily_pipeline": _fit_stub_pipeline(),
        "tri_day_pipeline": _fit_stub_pipeline(),
        "feature_importances": {"daily": daily_importances, "tri_day": tri_importances},
        "metrics": {},
    }
    joblib.dump(artifact, path)


class GenerateForecastTests(TestCase):
    """DB-backed orchestration tests for services.generate_forecast, following the same
    "real DB rows, no mocking" style as recommendations/tests.py's GenerateRecommendationsTests.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        self.logs = [
            DailyLog.objects.create(
                flock=self.flock, date=date(2024, 1, 1) + timedelta(days=i), caging_period=1,
                flock_size=240, flock_age_weeks=25, egg_count=egg, feed_intake_kg="40.0",
                temperature_c="33.0", humidity_pct="70.0", recorded_by=self.user,
            )
            for i, egg in enumerate([150, 155, 160, 165])
        ]

        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.model_path = Path(self.tmpdir.name) / "forecast_model.joblib"
        _write_stub_artifact(self.model_path)

    def test_generates_forecast_and_recommendations_from_flock_history(self):
        day4 = self.logs[3]
        forecast = services.generate_forecast(day4, model_path=self.model_path)

        self.assertEqual(Forecast.objects.filter(flock=self.flock, forecast_date=day4.date).count(), 1)
        self.assertEqual(forecast.forecast_date, day4.date)
        self.assertEqual(forecast.model_version, "rf-test")
        self.assertIsInstance(forecast.predicted_daily_yield, Decimal)
        self.assertIsInstance(forecast.predicted_tri_day_yield, Decimal)
        # Priors are the 3 days before day4, all in the same caging period.
        self.assertEqual(set(forecast.source_logs.all()), {day4, *self.logs[:3]})
        # temperature_c=33.0 is at/above the severe heat-stress threshold -> fires.
        self.assertTrue(forecast.recommendations.exists())

    def test_cold_start_first_log_of_flock_has_no_priors_and_still_succeeds(self):
        day1 = self.logs[0]
        forecast = services.generate_forecast(day1, model_path=self.model_path)
        self.assertEqual(set(forecast.source_logs.all()), {day1})

    def test_missing_model_file_raises_model_not_trained_error(self):
        missing_path = Path(self.tmpdir.name) / "does-not-exist.joblib"
        with self.assertRaises(services.ModelNotTrainedError):
            services.generate_forecast(self.logs[0], model_path=missing_path)

    def test_calling_twice_for_the_same_date_updates_in_place(self):
        day4 = self.logs[3]
        services.generate_forecast(day4, model_path=self.model_path)
        services.generate_forecast(day4, model_path=self.model_path)
        self.assertEqual(Forecast.objects.filter(flock=self.flock, forecast_date=day4.date).count(), 1)

    def test_uses_daily_not_tri_day_feature_importances(self):
        daily_importances = {"temperature_c": 0.9, "humidity_pct": 0.1}
        tri_importances = {"temperature_c": 0.1, "humidity_pct": 0.9}
        custom_path = Path(self.tmpdir.name) / "custom.joblib"
        _write_stub_artifact(custom_path, daily_importances, tri_importances)

        forecast = services.generate_forecast(self.logs[3], model_path=custom_path)
        self.assertEqual(forecast.feature_importances, daily_importances)

    def test_generates_next_day_forecasts_using_weather_when_todays_log(self):
        today_log = DailyLog.objects.create(
            flock=self.flock, date=date.today(), caging_period=1,
            flock_size=240, flock_age_weeks=25, egg_count=170, feed_intake_kg="40.0",
            temperature_c="33.0", humidity_pct="70.0", recorded_by=self.user,
        )
        weather = {
            date.today() + timedelta(days=1): {"temperature_c": 30.0, "humidity_pct": 80.0},
            date.today() + timedelta(days=2): {"temperature_c": 31.0, "humidity_pct": 78.0},
            date.today() + timedelta(days=3): {"temperature_c": 32.0, "humidity_pct": 76.0},
        }
        with patch("forecasting.services.fetch_forecast_weather", return_value=weather) as mock_fetch:
            forecast = services.generate_forecast(today_log, model_path=self.model_path)

        mock_fetch.assert_called_once()
        self.assertIsInstance(forecast.predicted_next_day1_yield, Decimal)
        self.assertIsInstance(forecast.predicted_next_day2_yield, Decimal)
        self.assertIsInstance(forecast.predicted_next_day3_yield, Decimal)

    def test_next_day_forecasts_fall_back_to_carried_forward_weather_on_fetch_failure(self):
        today_log = DailyLog.objects.create(
            flock=self.flock, date=date.today(), caging_period=1,
            flock_size=240, flock_age_weeks=25, egg_count=170, feed_intake_kg="40.0",
            temperature_c="33.0", humidity_pct="70.0", recorded_by=self.user,
        )
        with patch("forecasting.services.fetch_forecast_weather", return_value={}) as mock_fetch:
            forecast = services.generate_forecast(today_log, model_path=self.model_path)

        mock_fetch.assert_called_once()
        self.assertIsInstance(forecast.predicted_next_day1_yield, Decimal)
        self.assertIsInstance(forecast.predicted_next_day2_yield, Decimal)
        self.assertIsInstance(forecast.predicted_next_day3_yield, Decimal)

    def test_backdated_log_skips_weather_forecast_fetch(self):
        day4 = self.logs[3]  # dated 2024-01-04, never "today" -- Open-Meteo's forecast
        # can't meaningfully inform a backdated log's future days.
        with patch("forecasting.services.fetch_forecast_weather") as mock_fetch:
            services.generate_forecast(day4, model_path=self.model_path)
        mock_fetch.assert_not_called()


class TrainForecastModelLockTests(TestCase):
    """DB-backed tests for train_forecast_model's DailyLog.is_locked bookkeeping,
    following the same "real DB rows, no mocking" style as GenerateForecastTests.
    Uses a tiny n_estimators and a real tempdir --output-dir so these never touch the
    real models/ folder and stay fast; see forecasting/pipeline.py for why 20
    consecutive same-segment rows is enough for both the daily and tri-day models to
    have rows left after add_lag_features/add_tri_day_target's dropna."""

    def setUp(self):
        # A foundation farmer must exist for _load_records to resolve at all (see its
        # User.get_foundation_farmer() call) whenever the trained owner isn't the
        # foundation farmer themselves -- given no DailyLogs of their own here, they
        # contribute zero extra rows to the pooled training set below.
        self.foundation_user = User.objects.create_user(
            username="foundationfarmer", password="pw12345", is_foundation_farmer=True,
        )

        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        for i in range(20):
            DailyLog.objects.create(
                flock=self.flock, date=date(2024, 1, 1) + timedelta(days=i), caging_period=1,
                flock_size=240, flock_age_weeks=25, egg_count=140 + i, feed_intake_kg="40.0",
                temperature_c="28.0", humidity_pct="70.0", recorded_by=self.user,
            )
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _train(self, **extra_options):
        options = {"owner_id": self.user.id, "n_estimators": 10, "output_dir": self.tmpdir.name}
        options.update(extra_options)
        call_command("train_forecast_model", **options)

    def test_dry_run_does_not_lock_any_records(self):
        self._train(dry_run=True)
        self.assertFalse(DailyLog.objects.filter(flock=self.flock, is_locked=True).exists())

    def test_successful_persist_locks_all_of_owners_own_records(self):
        self._train()
        self.assertEqual(DailyLog.objects.filter(flock=self.flock, is_locked=True).count(), 20)

    def test_persist_does_not_lock_foundation_farmers_records(self):
        foundation_flock = Flock.objects.create(
            owner=self.foundation_user, generation_number=1, started_on=date(2023, 1, 1),
        )
        foundation_log = DailyLog.objects.create(
            flock=foundation_flock, date=date(2023, 1, 1), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=130, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="70.0", recorded_by=self.foundation_user,
        )

        self._train()  # self.user is not the foundation farmer, so _load_records pools both.

        self.assertEqual(DailyLog.objects.filter(flock=self.flock, is_locked=True).count(), 20)
        foundation_log.refresh_from_db()
        self.assertFalse(foundation_log.is_locked)

    def test_strict_failure_does_not_lock_records(self):
        # Overwrite egg_count with noise uncorrelated to every feature/lag, so the
        # held-out test rows can't be predicted well enough to clear the R2 threshold
        # -- --strict must then refuse to persist, and nothing should get locked.
        rng = random.Random(42)
        for log in DailyLog.objects.filter(flock=self.flock):
            log.egg_count = rng.randint(50, 500)
            log.save(update_fields=["egg_count"])

        with self.assertRaises(CommandError):
            self._train(strict=True)
        self.assertFalse(DailyLog.objects.filter(flock=self.flock, is_locked=True).exists())


class PredictNextDaysTests(SimpleTestCase):
    """Direct unit test of the recursive lag1/roll3 feature construction in
    services._predict_next_days, isolated from real RF prediction values via a stub
    pipeline that just records the feature rows it was called with."""

    def test_recursive_lag1_roll3_construction(self):
        class StubLog:
            date = date(2024, 1, 10)
            egg_count = 150
            flock_size = 240
            flock_age_weeks = 25
            feed_intake_kg = Decimal("40.0")
            temperature_c = Decimal("30.0")
            humidity_pct = Decimal("70.0")

        class StubPrior:
            def __init__(self, egg_count):
                self.egg_count = egg_count

        # priors ordered most-recent-first, matching _build_feature_row's
        # .order_by("-date")[:3].
        priors = [StubPrior(145), StubPrior(140), StubPrior(135)]

        captured_rows = []

        class StubPipeline:
            def predict(self, X):
                captured_rows.append(X.iloc[0].to_dict())
                return [100.0 + len(captured_rows)]

        day1, day2, day3 = services._predict_next_days(StubPipeline(), StubLog(), priors, {})

        # day+1: lag1 = today's actual; roll3 = mean(today, prior[0], prior[1]).
        self.assertEqual(captured_rows[0]["lag1"], 150.0)
        self.assertAlmostEqual(captured_rows[0]["roll3"], (150 + 145 + 140) / 3)
        self.assertEqual(day1, 101.0)

        # day+2: lag1 = day1's own prediction; roll3 = mean(day1_pred, today, prior[0]).
        self.assertEqual(captured_rows[1]["lag1"], day1)
        self.assertAlmostEqual(captured_rows[1]["roll3"], (day1 + 150 + 145) / 3)
        self.assertEqual(day2, 102.0)

        # day+3: lag1 = day2's own prediction; roll3 = mean(day2_pred, day1_pred, today).
        self.assertEqual(captured_rows[2]["lag1"], day2)
        self.assertAlmostEqual(captured_rows[2]["roll3"], (day2 + day1 + 150) / 3)
        self.assertEqual(day3, 103.0)

        # No weather_by_date entries -> temperature/humidity carried forward from today.
        for row in captured_rows:
            self.assertEqual(row["temperature_c"], 30.0)
            self.assertEqual(row["humidity_pct"], 70.0)


class TriggerRetrainTests(SimpleTestCase):
    """services.trigger_retrain is a fire-and-forget launcher: these tests only check
    that it starts the right command and never raises, not that a real training run
    happens (that would be slow and duplicate train_forecast_model's own tests)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.log_path = Path(self.tmpdir.name) / "retrain.log"
        patcher = patch.object(services, "RETRAIN_LOG_PATH", self.log_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("forecasting.services.subprocess.Popen")
    def test_launches_the_tune_strict_management_command_without_waiting(self, mock_popen):
        services.trigger_retrain("caging_period_closed", owner_id=7)

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args.args[0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertIn("train_forecast_model", cmd)
        self.assertIn("--owner-id", cmd)
        self.assertIn("7", cmd)
        self.assertIn("--tune", cmd)
        self.assertIn("--strict", cmd)
        # Fire-and-forget: never blocks on the child process.
        mock_popen.return_value.wait.assert_not_called()
        mock_popen.return_value.communicate.assert_not_called()

    @patch("forecasting.services.subprocess.Popen")
    def test_writes_the_reason_and_owner_to_the_retrain_log(self, mock_popen):
        services.trigger_retrain("flock_retired", owner_id=7)
        log_contents = self.log_path.read_text(encoding="utf-8")
        self.assertIn("flock_retired", log_contents)
        self.assertIn("owner_id=7", log_contents)

    @patch("forecasting.services.subprocess.Popen", side_effect=OSError("no such executable"))
    def test_a_launch_failure_is_swallowed_not_raised(self, mock_popen):
        services.trigger_retrain("caging_period_closed", owner_id=7)  # must not raise


class ForecastRecommendationsViewTests(TestCase):
    """Covers the Key Influencing Factors panel and the Egg Yield Trend chart's context
    on the /forecast-recommendations/ page (forecasting/views.py)."""

    def setUp(self):
        self.user = User.objects.create_user(username="farmer1", password="pw12345")
        self.client = Client()
        self.client.login(username="farmer1", password="pw12345")
        self.flock = Flock.objects.create(owner=self.user, generation_number=1, started_on=date(2024, 1, 1))
        self.log = DailyLog.objects.create(
            flock=self.flock, date=date.today(), flock_size=240, caging_period=1,
            flock_age_weeks=25, egg_count=150, feed_intake_kg="40.0",
            temperature_c="28.0", humidity_pct="75.0", recorded_by=self.user,
        )
        self.forecast = Forecast.objects.create(
            flock=self.flock, forecast_date=date.today(),
            predicted_daily_yield=Decimal("152.00"), predicted_tri_day_yield=Decimal("455.00"),
            predicted_next_day1_yield=Decimal("157.00"), predicted_next_day2_yield=Decimal("155.00"),
            predicted_next_day3_yield=Decimal("172.00"),
            feature_importances={
                "flock_size": 0.55, "flock_age_weeks": 0.06, "feed_intake_kg": 0.20,
                "temperature_c": 0.03, "humidity_pct": 0.01, "lag1": 0.10, "roll3": 0.05,
            },
            model_version="rf-test",
        )
        self.forecast.source_logs.set([self.log])

    def test_key_influencing_factors_shows_only_the_five_raw_features(self):
        response = self.client.get("/forecast-recommendations/")
        shown = dict(response.context["feature_importances"])
        self.assertEqual(
            set(shown), {"flock_size", "flock_age_weeks", "feed_intake_kg", "temperature_c", "humidity_pct"}
        )
        self.assertNotIn("lag1", shown)
        self.assertNotIn("roll3", shown)

    def test_key_influencing_factors_converts_fraction_to_percent_unscaled(self):
        response = self.client.get("/forecast-recommendations/")
        shown = dict(response.context["feature_importances"])
        # True RF fraction * 100, not rescaled to sum to 100 across just the 5 shown.
        self.assertAlmostEqual(shown["flock_size"], 55.0)
        self.assertAlmostEqual(shown["feed_intake_kg"], 20.0)
        self.assertContains(response, "55%")

    def test_trend_context_matches_dashboards_shape(self):
        response = self.client.get("/forecast-recommendations/")
        self.assertIn("trend_predicted_json", response.context)
        self.assertIn("trend_range_choices", response.context)
        self.assertEqual(response.context["trend_range"], "7")
        self.assertIn("152.0", response.context["trend_predicted_json"])

    def test_trend_range_query_param_is_respected(self):
        response = self.client.get("/forecast-recommendations/?trend_range=30")
        self.assertEqual(response.context["trend_range"], "30")
