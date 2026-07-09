"""Unit tests for the pure forecasting pipeline logic (no DB required).

These target the two places where caging-period boundaries matter — the tri-day target
construction and the chronological split — since those are the spec-section-10 rules the
model's defensibility rests on.
"""

from datetime import date, timedelta

from django.test import SimpleTestCase

from forecasting import pipeline as ml


def _records(start, caging_period, n, egg_start=100):
    """A run of `n` consecutive daily rows in one caging period."""
    return [
        {
            "date": start + timedelta(days=i),
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
