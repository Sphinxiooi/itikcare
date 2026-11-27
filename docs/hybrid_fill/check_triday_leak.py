"""Measure the tri-day overlap: drop training rows whose 3-day total includes a test day,
retrain, and compare on the same test rows. Saves triday_leak_check.json.

Run after hybrid_fill_experiment.py:
    venv/Scripts/python docs/hybrid_fill/check_triday_leak.py
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
source = (HERE / "hybrid_fill_experiment.py").read_text(encoding="utf-8")
exec(source.split("results = {")[0])  # reuse the data and functions without rerunning everything

target = ml.TRI_DAY_TARGET
base = model_frame(real, target)
train, test = ml.chronological_split(base, TEST_FRACTION)
denom = float(base[target].mean())
windows = test_windows(test)
filled, _ = hybrid_fill(windows, True)
frame = model_frame(filled, target)
training_sets = {"real_level": train, "hybrid_level": frame[~in_windows(frame, windows, False)]}

saved = {}
for name, training in training_sets.items():
    overlap = np.zeros(len(training), dtype=bool)
    for flock, _, first, _ in windows:
        overlap |= ((training.flock_id == flock) & (training.date < first)
                    & (training.date >= first - pd.Timedelta(days=2))).to_numpy()
    run = train_and_score(training[~overlap], test, target, False, denom)
    saved[name] = {"rows_removed": int(overlap.sum()), "metrics": run["metrics"]}
    m = run["metrics"]
    print(f"{name:13s} removed {overlap.sum():2d} rows | MAE {m['mae']:.2f} ({m['mae_pct']*100:.2f}%) "
          f"RMSE {m['rmse_pct']*100:.2f}% MAPE {m['mape']*100:.2f}% R2 {m['r2']:.3f} "
          f"{sum(m['passes'].values())}/4")
(HERE / "triday_leak_check.json").write_text(json.dumps(saved, indent=1), encoding="utf-8")
