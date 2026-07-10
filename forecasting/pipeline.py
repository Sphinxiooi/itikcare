"""Random Forest egg-yield forecasting pipeline (pure, DB-agnostic).

This module holds the modelling logic as plain functions over a pandas DataFrame so it
can be unit-tested and reasoned about without the Django ORM. The management command
``train_forecast_model`` is the thin orchestration layer that pulls DailyLog rows,
calls into here, and persists the result.

Design notes for the thesis defense (itikcare-spec.md sections 4, 5, 10):

* **Model features = five same-day inputs + two within-segment history features.**
  The raw inputs are flock size, flock age, feed intake, temperature, humidity. On top
  of those we add ``lag1`` (yesterday's yield) and ``roll3`` (mean of the previous three
  days). These history features are built with a per-caging-period shift, so they are
  ``NaN`` for the first days of every caging period and can *never* borrow a value from
  before a free-range gap — this is exactly the boundary rule spec section 10 requires
  (it forbids only lag/rolling features that *span* a gap; within-segment lags are the
  intended design). ``add_lag_features`` enforces that.
* The *tri-day target* likewise sums three consecutive days and must never span a gap;
  ``add_tri_day_target`` enforces that.
* **Caging_Period / Flock_Generation are used for segmentation only** — never fed to the
  model as raw features, or it would memorise specific time periods instead of learning
  general patterns.
* Everything is seeded with ``random_state=42`` for reproducibility.
* **Optional hyperparameter tuning** (``tune_estimator``) is scored with an inner,
  segment-aware CV (``segmented_time_series_splits``) built only from the training
  partition returned by ``chronological_split`` — the held-out 20% test rows are never
  used to pick hyperparameters, only to run the final, honest acceptance check.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import RandomizedSearchCV, TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# The five manual-entry raw inputs (itikcare-spec.md section 4). These are what the
# farmer types in and what we load from the DB.
FEATURES = [
    "flock_size",
    "flock_age_weeks",
    "feed_intake_kg",
    "temperature_c",
    "humidity_pct",
]

# Within-segment history features derived from past daily yield (see add_lag_features).
LAG_FEATURES = ["lag1", "roll3"]

# What the model is actually fitted on. Order is fixed so saved feature-importance
# vectors line up with prediction-time inputs.
MODEL_FEATURES = FEATURES + LAG_FEATURES

DAILY_TARGET = "egg_count"
TRI_DAY_TARGET = "tri_day_yield"

# Column that marks a contiguous caged stretch. Used only to segment the data; never a
# model feature. In the historical data these values are globally unique per stretch
# (gen 1 -> periods 1-2, gen 2 -> periods 3-5), but we also sort by date within each
# segment so the logic is robust regardless.
SEGMENT_COLUMN = "caging_period"

RANDOM_STATE = 42

# Acceptance thresholds from itikcare-spec.md section 5. MAE/RMSE are expressed as a
# fraction of the target's own mean; MAPE is a fraction; R2 is absolute.
THRESHOLDS = {
    "mae_pct": 0.08,   # MAE <= 8% of average yield
    "rmse_pct": 0.10,  # RMSE <= 10% of average yield
    "mape": 0.15,      # MAPE <= 15%
    "r2": 0.75,        # R2 >= 0.75
}

# Search space for optional hyperparameter tuning (see tune_estimator). Only
# n_estimators/random_state are set by default in build_estimator; these ranges cover
# the tree-shape/regularisation knobs left at sklearn defaults otherwise. Kept small and
# fixed (rather than tuned on the fly) so a thesis defense can point at one reasoned,
# reproducible space instead of an ad hoc one.
PARAM_DISTRIBUTIONS = {
    "rf__n_estimators": [200, 300, 400, 500, 600, 800],
    "rf__max_depth": [None, 4, 5, 6, 8, 10, 12, 16],
    "rf__min_samples_leaf": [1, 2, 3, 4, 6, 8],
    "rf__min_samples_split": [2, 5, 10, 15, 20, 25, 30],
    "rf__max_features": ["sqrt", "log2", 0.4, 0.5, 0.7, 1.0],
}


def build_feature_frame(records: list[dict]) -> pd.DataFrame:
    """Build the working DataFrame from a list of DailyLog-shaped dicts.

    Each dict must contain ``date``, ``caging_period``, the five FEATURES, and
    ``egg_count``. Decimal fields (feed/temperature/humidity) are coerced to float so
    scikit-learn sees numeric dtypes. Kept separate from the ORM so tests can pass plain
    dicts.
    """
    df = pd.DataFrame.from_records(records)
    df["date"] = pd.to_datetime(df["date"])
    numeric = FEATURES + [DAILY_TARGET]
    df[numeric] = df[numeric].apply(pd.to_numeric, errors="coerce")
    return df.sort_values([SEGMENT_COLUMN, "date"]).reset_index(drop=True)


def add_tri_day_target(df: pd.DataFrame) -> pd.DataFrame:
    """Return rows that have a valid tri-day (3-day) yield target.

    The tri-day target for a given day is the sum of ``egg_count`` for that day and the
    next two days. It is only defined when those three days are *consecutive calendar
    days within the same caging period* — this is what stops a 3-day window from
    silently spanning a free-range gap (or any internal missing day), per spec section
    10. Rows whose forward window is incomplete are dropped from the tri-day dataset
    only; they still count toward the daily model.
    """
    frames = []
    for _, segment in df.groupby(SEGMENT_COLUMN, sort=False):
        segment = segment.sort_values("date").reset_index(drop=True)
        egg = segment[DAILY_TARGET]
        # Next two calendar days must be exactly +1 and +2 days from this row's date.
        date = segment["date"]
        next1_ok = date.shift(-1) == date + pd.Timedelta(days=1)
        next2_ok = date.shift(-2) == date + pd.Timedelta(days=2)
        window_complete = next1_ok & next2_ok

        tri_day = egg + egg.shift(-1) + egg.shift(-2)
        segment = segment.assign(**{TRI_DAY_TARGET: tri_day.where(window_complete)})
        frames.append(segment[window_complete])

    if not frames:
        return df.iloc[0:0].assign(**{TRI_DAY_TARGET: pd.Series(dtype=float)})
    return pd.concat(frames).reset_index(drop=True)


def add_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add within-segment history features and drop rows that don't have them yet.

    ``lag1`` is the previous day's egg_count; ``roll3`` is the mean egg_count of the
    previous three days. Both are computed with a ``groupby(caging_period).shift(1)`` so
    a value is only ever taken from an earlier day *in the same caging period* — it can
    never reach back across a free-range gap into a different segment (spec section 10).

    Because of that, the first day of each segment has no ``lag1`` and the first three
    have no ``roll3``; those rows are dropped (they are the unavoidable cold-start of a
    freshly re-caged flock). At prediction time these come from the farmer's own recent
    logs, which always exist mid-period.
    """
    df = df.sort_values([SEGMENT_COLUMN, "date"]).copy()
    grouped = df.groupby(SEGMENT_COLUMN, sort=False)[DAILY_TARGET]
    df["lag1"] = grouped.shift(1)
    # shift(1) first so "today" is never part of its own rolling window.
    df["roll3"] = grouped.transform(lambda s: s.shift(1).rolling(3).mean())
    return df.dropna(subset=LAG_FEATURES).reset_index(drop=True)


def chronological_split(df: pd.DataFrame, test_fraction: float = 0.2):
    """Split 80:20 chronologically *within each caging period*.

    For every caging period the rows are ordered by date and the last
    ``test_fraction`` become the test set. This guarantees we never train on days that
    are more recent than the days we test on (within a segment), which is the defensible
    choice for a time-ordered farm dataset. Splitting per segment (rather than globally
    by date) keeps every caging period represented in both train and test.
    """
    train_parts, test_parts = [], []
    for _, segment in df.groupby(SEGMENT_COLUMN, sort=False):
        segment = segment.sort_values("date")
        n_test = int(round(len(segment) * test_fraction))
        # Keep at least one row on each side when a segment is large enough to split.
        n_test = min(max(n_test, 1), len(segment) - 1) if len(segment) >= 2 else 0
        n_train = len(segment) - n_test
        train_parts.append(segment.iloc[:n_train])
        test_parts.append(segment.iloc[n_train:])

    train = pd.concat(train_parts).reset_index(drop=True)
    test = pd.concat(test_parts).reset_index(drop=True)
    return train, test


def segmented_time_series_splits(df: pd.DataFrame, n_splits: int = 4, segment_col: str = SEGMENT_COLUMN):
    """Expanding-window CV folds, computed within each caging period, for hyperparameter
    tuning only.

    This is the *inner* CV boundary used solely to score candidate hyperparameters on the
    training partition returned by ``chronological_split`` — it must never see that
    function's held-out test rows. Like ``chronological_split``, it never validates on a
    row that precedes a training row within the same segment, and never mixes rows from
    different segments into one fold, so it cannot leak information across a free-range
    gap (spec section 10).

    ``df`` must already be sorted by (segment_col, date) with a fresh 0..n-1 index (the
    ``train`` half of ``chronological_split``'s output already satisfies this). Segments
    too small to support ``n_splits`` validation folds contribute all of their rows to
    every fold's training side and are never used for validation, rather than raising.

    Returns a list of (train_idx, val_idx) numpy-array pairs, directly usable as the
    ``cv`` argument to RandomizedSearchCV/GridSearchCV.
    """
    folds = [[] for _ in range(n_splits)]  # each entry: (train_idx_parts, val_idx_parts)
    for i in range(n_splits):
        folds[i] = ([], [])

    for _, segment in df.groupby(segment_col, sort=False):
        segment = segment.sort_values("date")
        positions = segment.index.to_numpy()

        if len(positions) < n_splits + 1:
            # Too small to hold out any validation fold: always train, never validate.
            for train_parts, _val_parts in folds:
                train_parts.append(positions)
            continue

        splitter = TimeSeriesSplit(n_splits=n_splits)
        for fold_i, (train_local, val_local) in enumerate(splitter.split(positions)):
            folds[fold_i][0].append(positions[train_local])
            folds[fold_i][1].append(positions[val_local])

    return [
        (np.concatenate(train_parts), np.concatenate(val_parts))
        for train_parts, val_parts in folds
        if val_parts  # drop a fold entirely if no segment was large enough to fill it
    ]


def build_estimator(n_estimators: int = 300, random_state: int = RANDOM_STATE) -> Pipeline:
    """A single sklearn Pipeline: impute -> scale -> Random Forest.

    * ``SimpleImputer(median)`` fills any missing feature values (median is robust to
      outliers on this small, real dataset).
    * ``StandardScaler`` is included to satisfy spec section 5's "normalize/scale
      numerical features". Random Forests are scale-invariant, so this does not change
      predictions or the impurity-based feature importances — it is kept for a
      consistent, spec-compliant preprocessing story rather than out of necessity.
    * Wrapping preprocessing + model in one Pipeline means fit/predict cannot leak the
      test set through the imputer/scaler statistics.

    Outliers are intentionally *not* removed: values are already range-validated on entry
    (DailyLog validators + the import command's full_clean), the dataset is small and
    real, and trees are robust to outliers. Dropping real farm rows would hurt more than
    help and is harder to defend.
    """
    return Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            (
                "rf",
                RandomForestRegressor(
                    n_estimators=n_estimators,
                    random_state=random_state,
                    n_jobs=-1,
                ),
            ),
        ]
    )


def tune_estimator(
    train_df: pd.DataFrame,
    feats: list[str],
    target: str,
    n_splits: int = 4,
    n_iter: int = 40,
    random_state: int = RANDOM_STATE,
) -> tuple[Pipeline, dict]:
    """Randomized hyperparameter search over PARAM_DISTRIBUTIONS, scored by MAE.

    CV is ``segmented_time_series_splits(train_df, n_splits)`` — an inner split of the
    *training* partition only. The real 20% test set from ``chronological_split`` is
    never passed to this function, so hyperparameters are chosen without ever looking at
    the held-out rows the final acceptance-threshold check runs against.

    A randomized search (rather than an exhaustive grid) is used because
    PARAM_DISTRIBUTIONS's full grid is several thousand fit combinations once every fold
    is counted — impractical to exhaust for a routine retrain. ``scoring`` is plain MAE
    (not the spec's %-of-mean MAE) for simplicity: within one training run the target
    mean is constant, so ranking candidate hyperparameters by raw MAE and by MAE-%-of-mean
    gives the same ordering.

    Returns ``(best_estimator, best_params)``. ``best_estimator`` is already refit on the
    full ``train_df`` by RandomizedSearchCV's default ``refit=True``.
    """
    cv = segmented_time_series_splits(train_df, n_splits=n_splits)
    search = RandomizedSearchCV(
        build_estimator(random_state=random_state),
        PARAM_DISTRIBUTIONS,
        n_iter=n_iter,
        cv=cv,
        scoring="neg_mean_absolute_error",
        random_state=random_state,
        n_jobs=-1,
    )
    search.fit(train_df[feats], train_df[target])
    return search.best_estimator_, search.best_params_


def feature_importances(fitted_pipeline: Pipeline) -> dict[str, float]:
    """Extract RF feature importances as a {feature: importance} dict, sorted descending.

    Matches the shape documented on ``forecasting.models.Forecast.feature_importances``
    and feeds the prescriptive module's rule prioritisation.
    """
    rf = fitted_pipeline.named_steps["rf"]
    pairs = zip(MODEL_FEATURES, (float(v) for v in rf.feature_importances_))
    return dict(sorted(pairs, key=lambda kv: kv[1], reverse=True))


def evaluate(y_true, y_pred, denom_mean: float) -> dict:
    """Compute the spec section-5 metrics and PASS/FAIL flags for one model.

    ``denom_mean`` is the mean of that model's own target (daily yield mean for the daily
    model, tri-day yield mean for the tri-day model), used to turn MAE/RMSE into the
    "% of average yield" figures the thresholds are written against.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)

    mae = mean_absolute_error(y_true, y_pred)
    rmse = float(np.sqrt(mean_squared_error(y_true, y_pred)))
    mape = float(np.mean(np.abs((y_true - y_pred) / y_true)))  # targets are always > 0
    r2 = r2_score(y_true, y_pred)

    mae_pct = mae / denom_mean
    rmse_pct = rmse / denom_mean

    return {
        "n": int(len(y_true)),
        "target_mean": float(denom_mean),
        "mae": float(mae),
        "mae_pct": float(mae_pct),
        "rmse": rmse,
        "rmse_pct": float(rmse_pct),
        "mape": mape,
        "r2": float(r2),
        "passes": {
            "mae": mae_pct <= THRESHOLDS["mae_pct"],
            "rmse": rmse_pct <= THRESHOLDS["rmse_pct"],
            "mape": mape <= THRESHOLDS["mape"],
            "r2": r2 >= THRESHOLDS["r2"],
        },
    }


def baseline_metrics(X_train, y_train, X_test, y_test, denom_mean: float) -> dict:
    """Metrics for a trivial mean-predictor baseline, so RF improvement is measurable."""
    dummy = DummyRegressor(strategy="mean")
    dummy.fit(X_train, y_train)
    return evaluate(y_test, dummy.predict(X_test), denom_mean)
