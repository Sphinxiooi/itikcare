"""Hybrid filling of free-range gaps, for model training only (pure, DB-agnostic).

Ported from the experiment in docs/hybrid_fill/. Nothing here reads or writes the
database: the synthetic days exist only in memory while a model is being trained, and are
never saved as DailyLogs or shown to the farmer.

**What is filled.** The days between two consecutive caging periods of the *same flock*:

* egg yield, flock size, flock age, feed intake: a straight line from the last usable day
  before the gap to the first real day after it;
* temperature, humidity: an iterative imputer (Extra Trees) using the trend (days since the
  farm's first record), the day of the year and the other weather value.

**What is never filled.**

* the gap left by a flock replacement (the two sides belong to different flocks);
* a free-range stay that has not ended (there is no day after the gap to draw the line to);
* gaps longer than ``MAX_FILL_GAP_DAYS``;
* unlogged days inside one caging period.

**Leakage.** The caller splits the *real* rows into train and test first, so the test set
is identical with or without filling. No filled value is computed from a test-window day,
and training rows whose lag features or tri-day total touch a test day are removed.

Two limits to state openly: each straight line ends on the first real day after the gap,
which is later in time than the test days before it; and filled values are estimates that
vary less than real ones.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.exceptions import ConvergenceWarning
from sklearn.experimental import enable_iterative_imputer  # noqa: F401  (enables the import below)
from sklearn.impute import IterativeImputer

from . import pipeline as ml

# Longest gap that gets filled. The longest gap tested in the experiment was 103 days; a
# much longer line would mostly be invented data.
MAX_FILL_GAP_DAYS = 120

# roll3 looks back three rows (lag1 one), so this many rows after a test window would
# otherwise be built from test-day egg counts.
HISTORY_ROWS = 3

# True on filled rows, False on real ones.
SYNTHETIC_COLUMN = "synthetic"

# Optional input column naming each row's farm, so every farm gets its own weather imputer.
OWNER_COLUMN = "flock__owner_id"

LINEAR_COLUMNS = ["flock_size", "flock_age_weeks", "feed_intake_kg", ml.DAILY_TARGET]
WHOLE_NUMBER_COLUMNS = ["flock_size", "flock_age_weeks", ml.DAILY_TARGET]
WEATHER_COLUMNS = ["temperature_c", "humidity_pct"]

IMPUTER_TREES = 100
IMPUTER_MAX_ITER = 10


def test_windows(real: pd.DataFrame, test: pd.DataFrame) -> list[tuple]:
    """(flock_id, caging_period, first test date, last real date) per tested caging period.

    These are the date ranges that filled values and training rows must stay clear of.
    """
    windows = []
    for _, segment in test.groupby(ml.SEGMENT_COLUMN):
        flock = segment["flock_id"].iloc[0]
        period = segment[ml.CAGING_PERIOD_COLUMN].iloc[0]
        in_period = (real["flock_id"] == flock) & (real[ml.CAGING_PERIOD_COLUMN] == period)
        windows.append((flock, period, segment["date"].min(), real.loc[in_period, "date"].max()))
    return windows


def in_windows(df: pd.DataFrame, windows: list[tuple], by_period: bool) -> np.ndarray:
    """Boolean mask of the rows of ``df`` that fall inside a test window.

    ``by_period=False`` matches on flock and date only, for a filled frame whose rows have
    been joined across caging periods.
    """
    mask = np.zeros(len(df), dtype=bool)
    for flock, period, first, last in windows:
        match = (df["flock_id"] == flock) & df["date"].between(first, last)
        if by_period:
            match &= df[ml.CAGING_PERIOD_COLUMN] == period
        mask |= match.to_numpy()
    return mask


def _straight_line_block(before: pd.Series, after: pd.Series, days: pd.DatetimeIndex) -> pd.DataFrame:
    """The filled rows for one gap, without weather (see _impute_weather)."""
    block = pd.DataFrame({
        "date": days,
        "flock_id": before["flock_id"],
        ml.CAGING_PERIOD_COLUMN: before[ml.CAGING_PERIOD_COLUMN],
        SYNTHETIC_COLUMN: True,
    })
    if OWNER_COLUMN in before.index:
        block[OWNER_COLUMN] = before[OWNER_COLUMN]
    step = np.arange(1, len(days) + 1) / (len(days) + 1)
    for column in LINEAR_COLUMNS:
        block[column] = before[column] + (after[column] - before[column]) * step
    return block


def _impute_weather(safe: pd.DataFrame, gaps: pd.DataFrame, first_date) -> np.ndarray:
    """Estimated [temperature, humidity] for every row of ``gaps``.

    Fitted on ``safe`` (this farm's real days outside the test windows) plus the gap days.
    """
    def inputs(frame):
        return pd.DataFrame({
            "trend": (frame["date"] - first_date).dt.days.to_numpy(),
            "day_of_year": frame["date"].dt.dayofyear.to_numpy(),
            **{c: (frame[c].to_numpy() if c in frame else np.nan) for c in WEATHER_COLUMNS},
        })

    imputer = IterativeImputer(
        estimator=ExtraTreesRegressor(
            n_estimators=IMPUTER_TREES, random_state=ml.RANDOM_STATE, n_jobs=-1
        ),
        max_iter=IMPUTER_MAX_ITER,
        random_state=ml.RANDOM_STATE,
    )
    with warnings.catch_warnings():
        # The imputer normally uses all of IMPUTER_MAX_ITER without meeting its own
        # early-stopping rule. Expected, and not actionable during a routine retrain.
        warnings.simplefilter("ignore", ConvergenceWarning)
        imputer.fit(pd.concat([inputs(safe), inputs(gaps)], ignore_index=True))
    return imputer.transform(inputs(gaps))[:, 2:].round(1)


def fill_gaps(real: pd.DataFrame, windows: list[tuple]) -> pd.DataFrame:
    """Return ``real`` plus the filled gap days, re-segmented into unbroken chains.

    ``real`` is build_feature_frame's output. In the result, caging periods of one flock
    that are now continuous share a single segment_key, so lag features run through the
    filled days. A gap that is not filled starts a new segment, as does every new flock.
    ``real`` itself is not modified.
    """
    real = real.assign(**{SYNTHETIC_COLUMN: False})
    safe = real[~in_windows(real, windows, by_period=True)]
    farm_of = real[OWNER_COLUMN] if OWNER_COLUMN in real else pd.Series(0, index=real.index)

    segment_keys = {}  # (flock_id, caging_period) -> segment key after filling
    filled_blocks = []
    for farm in farm_of.unique():
        farm_rows = real[farm_of == farm]
        blocks = []
        for flock, flock_rows in farm_rows.groupby("flock_id"):
            periods = [
                rows.sort_values("date")
                for _, rows in flock_rows.groupby(ml.CAGING_PERIOD_COLUMN)
            ]
            periods.sort(key=lambda rows: rows["date"].iloc[0])
            chain = 0
            for index, rows in enumerate(periods):
                period = rows[ml.CAGING_PERIOD_COLUMN].iloc[0]
                if index > 0:
                    previous = periods[index - 1]
                    last_day = previous["date"].iloc[-1]
                    after = rows.iloc[0]
                    gap_days = (after["date"] - last_day).days - 1
                    if 0 <= gap_days <= MAX_FILL_GAP_DAYS:
                        previous_period = previous[ml.CAGING_PERIOD_COLUMN].iloc[0]
                        # The line starts from the last day that is *not* a test day,
                        # placed at the end of the previous period.
                        before = safe[
                            (safe["flock_id"] == flock)
                            & (safe[ml.CAGING_PERIOD_COLUMN] == previous_period)
                        ].sort_values("date").iloc[-1]
                        days = pd.date_range(last_day, after["date"])[1:-1]
                        if len(days):
                            blocks.append(_straight_line_block(before, after, days))
                    else:
                        chain += 1
                segment_keys[(flock, period)] = f"{flock}:chain{chain}"

        if blocks:
            gaps = pd.concat(blocks, ignore_index=True)
            farm_safe = safe[safe.index.isin(farm_rows.index)]
            gaps[WEATHER_COLUMNS] = _impute_weather(farm_safe, gaps, farm_rows["date"].min())
            gaps[WHOLE_NUMBER_COLUMNS] = gaps[WHOLE_NUMBER_COLUMNS].round()
            filled_blocks.append(gaps)

    filled = pd.concat([real, *filled_blocks], ignore_index=True)
    filled[SYNTHETIC_COLUMN] = filled[SYNTHETIC_COLUMN].astype(bool)
    # A filled row carries the caging period it follows, so it joins that period's chain.
    filled[ml.SEGMENT_COLUMN] = [
        segment_keys[key]
        for key in zip(filled["flock_id"], filled[ml.CAGING_PERIOD_COLUMN])
    ]
    return filled.sort_values([ml.SEGMENT_COLUMN, "date"]).reset_index(drop=True)


def clean_training_rows(frame: pd.DataFrame, windows: list[tuple], target: str) -> pd.DataFrame:
    """Drop every row of a filled model frame that is, or touches, a test day.

    1. the test days themselves;
    2. history leak: lag1 / roll3 built from a test day (the rows right after a window);
    3. target leak (tri-day only): a 3-day total that includes a test day (the 2 days
       before a window).
    """
    is_test = pd.Series(in_windows(frame, windows, by_period=False), index=frame.index)

    history_leak = pd.Series(False, index=frame.index)
    for rows_back in range(1, HISTORY_ROWS + 1):
        shifted = is_test.groupby(frame[ml.SEGMENT_COLUMN]).shift(rows_back, fill_value=False)
        history_leak |= shifted.astype(bool)

    target_leak = pd.Series(False, index=frame.index)
    if target == ml.TRI_DAY_TARGET:
        for flock, _, first, _ in windows:
            target_leak |= (
                (frame["flock_id"] == flock)
                & (frame["date"] < first)
                & (frame["date"] >= first - pd.Timedelta(days=2))
            )

    return frame[~(is_test | history_leak | target_leak)].reset_index(drop=True)


def hybrid_training_frame(
    real: pd.DataFrame, test: pd.DataFrame, target: str
) -> tuple[pd.DataFrame, int]:
    """Training rows for ``target`` built from real plus filled days.

    ``test`` is the test half of chronological_split on the real data; it is only used to
    know which days to stay clear of, and stays the set the model is scored on.

    Returns ``(training_frame, number_of_filled_days)``. The frame is sorted by
    (segment_key, date) with a fresh index, as tune_estimator's inner CV requires.
    """
    windows = test_windows(real, test)
    filled = fill_gaps(real, windows)
    frame = filled
    if target == ml.TRI_DAY_TARGET:
        frame = ml.add_tri_day_target(frame)
    frame = ml.add_lag_features(frame)
    return clean_training_rows(frame, windows, target), int(filled[SYNTHETIC_COLUMN].sum())
