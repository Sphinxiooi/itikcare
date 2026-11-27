"""Train and save the leak-free hybrid-fill delta models (daily and tri-day).

Run from the project root, after hybrid_fill_experiment.py:
    venv/Scripts/python docs/hybrid_fill/train_clean_delta_model.py

On top of the experiment's no-leak fill, this removes training rows that still touch a
test day in either of two ways:
    1. history leak : lag1 / roll3 is built from a test day (the first filled days after
                      a test window)
    2. target leak  : the tri-day total includes a test day (the last 2 rows before a
                      test window)

It then proves the result: every test-day value is changed, everything is rebuilt, and the
training data must come out identical.

The saved model predicts the CHANGE from yesterday, so a forecast is
    daily   : daily_model.predict(X) + lag1
    tri-day : tri_day_model.predict(X) + 3 * lag1
The Django app does not load this file; its keys differ from the app's model artifacts on
purpose, so it cannot be mistaken for a level model.

Outputs: hybrid_delta_model.joblib, clean_delta_results.json
"""
import json
from pathlib import Path

import joblib

HERE = Path(__file__).resolve().parent
source = (HERE / "hybrid_fill_experiment.py").read_text(encoding="utf-8")
exec(source.split("results = {")[0])  # reuse the data and functions without rerunning everything

HISTORY_ROWS = 3  # roll3 looks back three rows; lag1 looks back one
original_real = real


def clean_training_rows(target, windows):
    """Return (clean training rows, counts of removed rows) from the no-leak hybrid fill."""
    filled, _ = hybrid_fill(windows, True)
    frame = model_frame(filled, target)
    is_test = pd.Series(in_windows(frame, windows, False), index=frame.index)

    history_leak = pd.Series(False, index=frame.index)
    for back in range(1, HISTORY_ROWS + 1):
        history_leak |= is_test.groupby(frame[SEG]).shift(back, fill_value=False).astype(bool)

    target_leak = pd.Series(False, index=frame.index)
    if target == ml.TRI_DAY_TARGET:
        for flock, _, first, _ in windows:
            target_leak |= ((frame.flock_id == flock) & (frame.date < first)
                            & (frame.date >= first - pd.Timedelta(days=2)))

    removed = {
        "history_leak": int((history_leak & ~is_test).sum()),
        "target_leak": int((target_leak & ~is_test).sum()),
    }
    return frame[~(is_test | history_leak | target_leak)].reset_index(drop=True), removed


def assert_no_test_values_in_training(target, windows, training):
    """Change every value on every test day; the training data must not move."""
    global real
    changed = original_real.copy()
    mask = in_windows(changed, windows, True)
    columns = ml.FEATURES + [ml.DAILY_TARGET]
    changed[columns] = changed[columns].astype(float)
    changed.loc[mask, columns] += 1000.0
    real = changed
    try:
        rebuilt, _ = clean_training_rows(target, windows)
    finally:
        real = original_real
    compared = ["date", *ml.MODEL_FEATURES, target]
    pd.testing.assert_frame_equal(training[compared], rebuilt[compared], check_dtype=False)


artifact = {
    "model_version": "hybrid-delta-clean-v1",
    "target_type": "delta",
    "prediction_rule": {"daily_model": "predict(X) + lag1", "tri_day_model": "predict(X) + 3 * lag1"},
    "feature_names": ml.MODEL_FEATURES,
    "versions": {"scikit-learn": sklearn.__version__, "pandas": pd.__version__, "numpy": np.__version__},
}
summary = {k: artifact[k] for k in ("model_version", "target_type", "prediction_rule", "versions")}
summary["targets"] = {}

for key, target, model_key in (("daily", ml.DAILY_TARGET, "daily_model"),
                               ("triday", ml.TRI_DAY_TARGET, "tri_day_model")):
    base = model_frame(original_real, target)
    train, test = ml.chronological_split(base, TEST_FRACTION)
    denom = float(base[target].mean())
    windows = test_windows(test)

    training, removed = clean_training_rows(target, windows)
    assert_no_test_values_in_training(target, windows, training)

    ordered = training.sort_values([SEG, "date"]).reset_index(drop=True)
    ordered = ordered.assign(delta=ordered[target] - yesterday(ordered, target))
    model, params = ml.tune_estimator(ordered, ml.MODEL_FEATURES, "delta",
                                      n_splits=CV_FOLDS, n_iter=TUNE_ITER)
    predicted = model.predict(test[ml.MODEL_FEATURES]) + yesterday(test, target).to_numpy()
    metrics = ml.evaluate(test[target], predicted, denom)
    persistence = ml.evaluate(test[target], yesterday(test, target), denom)

    artifact[model_key] = model
    summary["targets"][key] = {
        "n_train": len(ordered), "n_test": len(test), "rows_removed": removed,
        "leak_test_passed": True, "metrics": metrics, "persistence_metrics": persistence,
        "best_params": params, "importances": ml.feature_importances(model),
    }
    for label, m in (("clean delta", metrics), ("persistence", persistence)):
        print(f"{key:7s} {label:12s} MAE {m['mae']:6.2f} RMSE {m['rmse']:6.2f} "
              f"MAPE {m['mape']*100:5.2f}% R2 {m['r2']:.3f} {sum(m['passes'].values())}/4")
    print(f"{key:7s} train rows {len(ordered)}, removed {removed}, leak test passed")

artifact["metrics"] = {k: v["metrics"] for k, v in summary["targets"].items()}
joblib.dump(artifact, HERE / "hybrid_delta_model.joblib", compress=3)
(HERE / "clean_delta_results.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
print("saved", HERE / "hybrid_delta_model.joblib")
