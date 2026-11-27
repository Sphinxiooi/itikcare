"""Turn results.json + the filled datasets into PNG figures and report.html.

Run after hybrid_fill_experiment.py:
    venv/Scripts/python docs/hybrid_fill/build_report.py
"""
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

HERE = Path(__file__).resolve().parent
FIG = HERE / "figures"
FIG.mkdir(exist_ok=True)
R = json.loads((HERE / "results.json").read_text(encoding="utf-8"))
DATA = pd.read_csv(HERE / "filled_dataset_daily.csv", parse_dates=["Date"]).sort_values("Date")
REAL, FILL, INK, MUTED = "#2a78d6", "#eb6834", "#222222", "#9a9994"
HORIZONS = {"daily": "Daily yield", "triday": "Tri-day yield"}
FEATURES = {"lag1": "Yesterday's yield (lag1)", "roll3": "3-day average (roll3)",
            "flock_size": "Flock size", "flock_age_weeks": "Flock age",
            "feed_intake_kg": "Feed intake", "temperature_c": "Temperature", "humidity_pct": "Humidity"}
SERIES = [("Egg Yield (per day)", "Egg yield (eggs per day)", "egg"),
          ("Temperature", "Temperature (°C)", "temp"), ("Humidity", "Humidity (%)", "hum")]
METRICS = [("mae_pct", "MAE (% of mean)", 8, "max"), ("rmse_pct", "RMSE (% of mean)", 10, "max"),
           ("mape", "MAPE (%)", 15, "max"), ("r2", "R²", 0.75, "min")]
MODELS = [("real_level", "Real data only", REAL), ("hybrid_level", "Hybrid fill", FILL),
          ("persistence", "Same as yesterday (no model)", MUTED)]


def value(horizon, run, metric):
    v = R["targets"][horizon]["runs"][run]["metrics"][metric]
    return v if metric == "r2" else v * 100


def save(fig, name):
    fig.tight_layout()
    fig.savefig(FIG / name, dpi=200)
    plt.close(fig)


def style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(color="#e4e3df", linewidth=0.6)
    ax.set_axisbelow(True)


# ---------- PNG figures ----------
# Split the calendar into continuous runs so no line is drawn across an empty gap.
DATA["run"] = ((DATA.Date.diff().dt.days != 1) | (DATA.Is_Synthetic != DATA.Is_Synthetic.shift())).cumsum()
fig, axes = plt.subplots(3, 1, figsize=(10, 8.5), sharex=True)
for ax, (col, label, _) in zip(axes, SERIES):
    for _, run in DATA.groupby("run"):
        filled = bool(run.Is_Synthetic.iloc[0])
        ax.plot(run.Date, run[col], color=FILL if filled else INK, linewidth=1.2,
                label="Filled (synthetic)" if filled else "Observed")
    ax.set_title(label, loc="left", fontsize=10)
    style(ax)
lines, labels = axes[0].get_legend_handles_labels()
unique = dict(zip(labels, lines))
axes[0].legend(unique.values(), unique.keys(), frameon=False, fontsize=8, ncol=2)
axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
save(fig, "fig1_observed_and_filled.png")

for n, (horizon, title) in enumerate(HORIZONS.items()):
    fig, axes = plt.subplots(2, 2, figsize=(10, 5.5))
    for ax, (key, label, limit, kind) in zip(axes.flat, METRICS):
        vals = [value(horizon, m, key) for m, _, _ in MODELS]
        bars = ax.barh([name for _, name, _ in MODELS], vals, color=[c for *_, c in MODELS], height=0.6)
        ax.bar_label(bars, fmt="%.3f" if key == "r2" else "%.2f", padding=3, fontsize=8)
        ax.axvline(limit, color=INK, linestyle="--", linewidth=1)
        ax.set_xlim(0, max(vals + [limit]) * 1.2)
        ax.set_title(f"{label}  ({'at least' if kind == 'min' else 'at most'} {limit})", loc="left", fontsize=9)
        ax.invert_yaxis()
        ax.tick_params(labelsize=8)
        style(ax)
    fig.suptitle(f"{title}: test metrics (dashed line = acceptance threshold)", fontsize=11)
    save(fig, f"fig{2 + n}_metrics_{horizon}.png")

for n, (horizon, title) in enumerate(HORIZONS.items()):
    t = R["targets"][horizon]
    x = range(len(t["test"]))
    fig, ax = plt.subplots(figsize=(10, 3.8))
    ax.plot(x, [r["actual"] for r in t["test"]], color=INK, linewidth=1.6, label="Actual")
    for run, name, color in MODELS[:2]:
        ax.plot(x, t["runs"][run]["predictions"], color=color, linewidth=1.3, label=name)
    for i in x[1:]:
        if t["test"][i]["period"] != t["test"][i - 1]["period"]:
            ax.axvline(i - 0.5, color=MUTED, linewidth=0.8)
    ax.set_title(f"{title}: actual vs predicted on test days (vertical lines separate caging periods)",
                 loc="left", fontsize=10)
    ax.set_xlabel("Test day number")
    ax.set_ylabel("Eggs")
    ax.legend(frameon=False, fontsize=8, ncol=3)
    style(ax)
    save(fig, f"fig{4 + n}_predictions_{horizon}.png")

fig, axes = plt.subplots(1, 2, figsize=(10, 4), sharey=True)
for ax, (horizon, title) in zip(axes, HORIZONS.items()):
    for offset, (run, name, color) in zip((-0.2, 0.2), MODELS[:2]):
        imp = R["targets"][horizon]["runs"][run]["importances"]
        ax.barh([i + offset for i in range(len(FEATURES))], [imp[f] for f in FEATURES],
                height=0.38, color=color, label=name)
    ax.set_yticks(range(len(FEATURES)), FEATURES.values(), fontsize=8)
    ax.set_title(f"{title}: feature importance", loc="left", fontsize=10)
    style(ax)
axes[0].invert_yaxis()
axes[1].legend(frameon=False, fontsize=8)
save(fig, "fig6_feature_importance.png")

fig, axes = plt.subplots(1, 2, figsize=(10, 3))
for ax, (horizon, title) in zip(axes, HORIZONS.items()):
    names = ["Real data only", "Hybrid with leak\n(invalid)", "Hybrid, no leak"]
    vals = [R["targets"][horizon]["runs"][k]["metrics"]["mae"]
            for k in ("real_level", "leaky_level", "hybrid_level")]
    bars = ax.barh(names, vals, color=[REAL, MUTED, FILL], height=0.6)
    ax.bar_label(bars, fmt="%.2f", padding=3, fontsize=8)
    ax.set_xlim(0, max(vals) * 1.18)
    ax.set_title(f"{title}: MAE in eggs (lower is better)", loc="left", fontsize=10)
    ax.invert_yaxis()
    ax.tick_params(labelsize=8)
    style(ax)
save(fig, "fig7_leakage.png")

# ---------- HTML page ----------
payload = {
    "results": R,
    "data": {"date": [str(d.date()) for d in DATA.Date], "filled": [int(v) for v in DATA.Is_Synthetic],
             **{short: [round(float(v), 1) for v in DATA[col]] for col, _, short in SERIES}},
}
template = (HERE / "report_template.html").read_text(encoding="utf-8")
page = template.replace("/*DATA*/", json.dumps(payload, separators=(",", ":")))
for token, col in (("EGG", "Egg Yield (per day)"), ("TEMP", "Temperature"), ("HUM", "Humidity")):
    observed, filled = DATA[~DATA.Is_Synthetic][col].std(), DATA[DATA.Is_Synthetic][col].std()
    page = page.replace(f"[{token}_REAL]", f"{observed:.1f}").replace(f"[{token}_FILL]", f"{filled:.1f}")
(HERE / "report.html").write_text(page, encoding="utf-8")
print("wrote", len(list(FIG.glob('*.png'))), "figures and report.html")
