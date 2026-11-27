"""Find long straight-line runs of egg counts in the source data and rescore without them.

A run is at least 6 consecutive daily changes in the same direction that differ by at most
1 egg, inside one caging period. Run after hybrid_fill_experiment.py:
    venv/Scripts/python docs/hybrid_fill/check_straight_runs.py
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
EGG = "Egg Yield (per day)"
MIN_STEPS = 6

df = pd.read_csv(HERE.parents[1] / "ItikCareDataSet.csv", parse_dates=["Date"])
flagged = set()
for period, seg in df.groupby("Caging_Period"):
    step = seg[EGG].diff().to_numpy()
    i = 1
    while i < len(seg):
        j = i
        while j + 1 < len(seg):
            window = step[i:j + 2]
            if np.ptp(window) <= 1 and ((window > 0).all() or (window < 0).all()):
                j += 1
            else:
                break
        if j - i + 1 >= MIN_STEPS and (step[i:j + 1] != 0).all():
            run = seg.iloc[i - 1:j + 1]
            flagged.update(run.Date.dt.strftime("%Y-%m-%d"))
            print(f"period {period}: {run.Date.iloc[0].date()} to {run.Date.iloc[-1].date()} "
                  f"({len(run)} days), eggs {run[EGG].iloc[0]} to {run[EGG].iloc[-1]}")
            i = j + 1
        else:
            i += 1
print(f"days in straight runs: {len(flagged)} of {len(df)}")

results = json.loads((HERE / "results.json").read_text(encoding="utf-8"))
for horizon, target in results["targets"].items():
    straight = np.array([row["date"] in flagged for row in target["test"]])
    actual = np.array([row["actual"] for row in target["test"]])
    print(f"{horizon}: {straight.sum()} of {len(straight)} test days are in straight runs")
    for name in ("real_level", "hybrid_level", "persistence"):
        error = np.abs(actual - np.array(target["runs"][name]["predictions"]))
        print(f"   {name:13s} MAE on straight days {error[straight].mean():6.2f} | "
              f"other days {error[~straight].mean():6.2f}")
