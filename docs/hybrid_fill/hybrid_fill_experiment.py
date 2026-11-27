"""Hybrid gap-fill experiment (dry run: writes only into this folder, never the database).

Run from the project root:
    venv/Scripts/python docs/hybrid_fill/hybrid_fill_experiment.py

Fills gaps 1, 3 and 4 of ItikCareDataSet.csv. Gap 2 stays empty (the flock was replaced).
    egg yield, flock size, flock age, feed intake : linear interpolation
    temperature, humidity : IterativeImputer(ExtraTreesRegressor), trend + day of year

Every model is scored on the same real test rows that forecasting/pipeline.py produces.
Outputs: results.json, filled_dataset_daily.csv, filled_dataset_triday.csv
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.experimental import enable_iterative_imputer  # noqa: F401
from sklearn.impute import IterativeImputer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from forecasting import pipeline as ml  # noqa: E402

SEG = ml.SEGMENT_COLUMN
WEATHER = ["temperature_c", "humidity_pct"]
LINEAR = ["flock_size", "flock_age_weeks", "feed_intake_kg", ml.DAILY_TARGET]
FILL_AFTER_PERIODS = [1, 3, 4]
TUNE_ITER, CV_FOLDS, TEST_FRACTION = 120, 4, 0.15
IMPUTER_TREES, IMPUTER_MAX_ITER = 100, 10

CSV_NAMES = {
    "Date": "date", "Caging_Period": "caging_period", "Flock_Generation": "flock_id",
    "Number of Flocks": "flock_size", "Average Age of Flock (weeks)": "flock_age_weeks",
    "Feed Intake (kgs per day)": "feed_intake_kg", "Egg Yield (per day)": "egg_count",
    "Temperature": "temperature_c", "Humidity": "humidity_pct",
}
raw = pd.read_csv(ROOT / "ItikCareDataSet.csv").rename(columns=CSV_NAMES)
real = ml.build_feature_frame(raw.to_dict("records"))


def test_windows(test):
    """(flock, period, first test date, last real date) for every caging period."""
    windows = []
    for _, seg in test.groupby(SEG):
        flock, period = seg.flock_id.iloc[0], seg.caging_period.iloc[0]
        last = real[(real.flock_id == flock) & (real.caging_period == period)].date.max()
        windows.append((flock, period, seg.date.min(), last))
    return windows


def in_windows(df, windows, by_period):
    mask = np.zeros(len(df), dtype=bool)
    for flock, period, first, last in windows:
        m = (df.flock_id == flock) & df.date.between(first, last)
        if by_period:
            m &= df.caging_period == period
        mask |= m.to_numpy()
    return mask


def hybrid_fill(windows, no_leak):
    """Return (full filled frame, imputer iterations).

    no_leak=True: no filled value is computed from any test-window day.
    no_leak=False: the naive build, kept only to show what leakage does.
    """
    safe = real[~in_windows(real, windows, True)] if no_leak else real
    blocks = []
    for period in FILL_AFTER_PERIODS:
        before = safe[safe.caging_period == period].sort_values("date").iloc[-1]
        last_day = real[real.caging_period == period].date.max()
        after = real[real.caging_period == period + 1].sort_values("date").iloc[0]
        days = pd.date_range(last_day, after.date)[1:-1]
        block = pd.DataFrame({"date": days, "flock_id": before.flock_id,
                              "caging_period": period, "synthetic": True})
        step = np.arange(1, len(days) + 1) / (len(days) + 1)
        for col in LINEAR:
            block[col] = before[col] + (after[col] - before[col]) * step
        blocks.append(block)
    gaps = pd.concat(blocks, ignore_index=True)

    def imputer_inputs(d):
        return pd.DataFrame({
            "trend": (d.date - real.date.min()).dt.days.to_numpy(),
            "day_of_year": d.date.dt.dayofyear.to_numpy(),
            **{c: (d[c].to_numpy() if c in d else np.nan) for c in WEATHER},
        })

    imputer = IterativeImputer(
        estimator=ExtraTreesRegressor(n_estimators=IMPUTER_TREES, random_state=ml.RANDOM_STATE, n_jobs=-1),
        max_iter=IMPUTER_MAX_ITER, random_state=ml.RANDOM_STATE)
    imputer.fit(pd.concat([imputer_inputs(safe), imputer_inputs(gaps)], ignore_index=True))
    gaps[WEATHER] = imputer.transform(imputer_inputs(gaps))[:, 2:].round(1)

    df = pd.concat([real.assign(synthetic=False), gaps], ignore_index=True)
    df["synthetic"] = df["synthetic"].astype(bool)
    for col in ["flock_size", "flock_age_weeks", ml.DAILY_TARGET]:
        df[col] = df[col].round()
    # Filling joins periods 1+2 and 3+4+5; gap 2 remains the only boundary.
    df[SEG] = np.where(df.flock_id == 1, "1:merged", "2:merged")
    return df.sort_values([SEG, "date"]).reset_index(drop=True), int(imputer.n_iter_)


def model_frame(df, target):
    if target == ml.TRI_DAY_TARGET:
        df = ml.add_tri_day_target(df)
    return ml.add_lag_features(df)


def yesterday(df, target):
    return df["lag1"] * (3 if target == ml.TRI_DAY_TARGET else 1)


def train_and_score(train, test, target, delta, denom):
    train = train.sort_values([SEG, "date"]).reset_index(drop=True)
    fit_col = target
    if delta:
        fit_col = "delta"
        train = train.assign(delta=train[target] - yesterday(train, target))
    model, params = ml.tune_estimator(train, ml.MODEL_FEATURES, fit_col,
                                      n_splits=CV_FOLDS, n_iter=TUNE_ITER)
    pred = model.predict(test[ml.MODEL_FEATURES])
    if delta:
        pred = pred + yesterday(test, target).to_numpy()
    return {
        "n_train": len(train),
        "metrics": ml.evaluate(test[target], pred, denom),
        "best_params": params,
        "importances": ml.feature_importances(model),
        "predictions": [round(float(v), 2) for v in pred],
    }


results = {
    "versions": {"scikit-learn": sklearn.__version__, "pandas": pd.__version__, "numpy": np.__version__},
    "settings": {"test_fraction": TEST_FRACTION, "tune_iter": TUNE_ITER, "cv_folds": CV_FOLDS,
                 "random_state": ml.RANDOM_STATE, "imputer_trees": IMPUTER_TREES,
                 "imputer_max_iter": IMPUTER_MAX_ITER,
                 "search_space": ml.PARAM_DISTRIBUTIONS, "thresholds": ml.THRESHOLDS},
    "real_rows": len(real),
    "targets": {},
}

for key, target in (("daily", ml.DAILY_TARGET), ("triday", ml.TRI_DAY_TARGET)):
    base = model_frame(real, target)
    train, test = ml.chronological_split(base, TEST_FRACTION)
    denom = float(base[target].mean())
    windows = test_windows(test)
    out = {
        "test": [{"date": str(d.date()), "period": int(p), "actual": float(a)}
                 for d, p, a in zip(test.date, test.caging_period, test[target])],
        "runs": {"persistence": {
            "n_train": 0,
            "metrics": ml.evaluate(test[target], yesterday(test, target), denom),
            "predictions": [float(v) for v in yesterday(test, target)]}},
    }
    training_sets = {"real": train}
    for build, no_leak in (("hybrid", True), ("leaky", False)):
        filled, iterations = hybrid_fill(windows, no_leak)
        frame = model_frame(filled, target)
        training_sets[build] = frame[~in_windows(frame, windows, False)]
        if no_leak:
            out["imputer_iterations"] = iterations
            out["filled_days"] = int(filled.synthetic.sum())
            filled.drop(columns=SEG).rename(columns={v: k for k, v in CSV_NAMES.items()} | {
                "synthetic": "Is_Synthetic"}).to_csv(HERE / f"filled_dataset_{key}.csv", index=False)
    for name, training in training_sets.items():
        for delta in (False, True):
            label = f"{name}_{'delta' if delta else 'level'}"
            out["runs"][label] = train_and_score(training, test, target, delta, denom)
            m = out["runs"][label]["metrics"]
            print(f"{key:7s} {label:14s} MAE {m['mae']:6.2f} RMSE {m['rmse']:6.2f} "
                  f"MAPE {m['mape']*100:5.2f}% R2 {m['r2']:.3f} "
                  f"{sum(m['passes'].values())}/4", flush=True)
    results["targets"][key] = out

(HERE / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
print("saved", HERE / "results.json")
