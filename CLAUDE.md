# ItikCare — Project Instructions for Claude

Full spec: see `itikcare-spec.md` in this repo. Read it before doing any planning work if you haven't already this session.

## What this project is

A Django 5 web app that forecasts daily/tri-day egg yield for a small-scale itik (duck) farm using Random Forest Regression, plus a rule-based prescriptive module that turns forecasts into farmer-facing recommendations. This is a capstone thesis project — code needs to be clean, explainable, and defensible, not just working.

Figma prototype (for UI/dashboard reference): https://www.figma.com/design/TZqCWEen9aoTE3mQZXDWa6/itikcare?node-id=0-1&t=FBmxO4nrTV7NrB4n-0

## Tech stack — do not deviate without asking

- Backend: Python 3.13, Django 5
- Frontend: HTML, CSS, JavaScript, Tailwind CSS (no other CSS framework)
- Database: MySQL
- ML: scikit-learn

## Core data model

Five entities: **User, Flock, DailyLog, Forecast, Recommendation**. See `itikcare-spec.md` section 3 for relationships and fields. DailyLog edits must always be tracked/audited (old value, new value, timestamp) — never allow silent overwrites of historical farm data.

## Hard requirements — check against these every time you touch the model or rule engine

**Random Forest forecasting model:**
- 80:20 train/test split
- Must support periodic rolling retraining (a repeatable script/management command, not a one-off notebook)
- Must expose feature importance scores (the prescriptive module depends on these)
- Acceptance thresholds: MAE ≤ 8% of avg daily yield · RMSE ≤ 10% of avg daily yield · MAPE ≤ 15% · R² ≥ 0.75

**Prescriptive analytics module:**
- Forward chaining (IF-THEN) combined with RF feature importance
- Recommendations must be traceable back to which variable/rule triggered them — this transparency is required for the thesis defense, don't build a black box
- Acceptance thresholds: Concordance Rate ≥ 80% · Prescriptive Effectiveness Rate ≥ 75% · False Recommendation Rate ≤ 10%

## Explicit scope boundaries — do not build these

- No IoT/sensor integration — temperature and humidity are always manual farmer entry
- No disease diagnostics, no meat-production tracking (egg yield only)
- No long-term/seasonal forecasting — short-term (daily/tri-day) only
- No financial/payroll/market transaction features
- Single-farm context — don't over-engineer for multi-tenancy

## Historical data has known, intentional gaps

`ItikCare_Cleaned_Dataset.csv` is real farm data, not a clean simulated dataset — it has multi-week/multi-month gaps (semi-intensive husbandry: ducks are caged for logging only part of the year) and at least one flock-generation reset (old flock retired, new younger flock brought in, seen as a sudden drop in flock age). These are real and expected, not data errors. The `Caging_Period` and `Flock_Generation` columns already mark these boundaries for you — use them to avoid building lag/rolling features that span across a gap, but don't feed them into the model as raw predictive features (they'd let the model memorize specific time periods instead of learning general patterns). See `itikcare-spec.md` section 10 before writing any data preprocessing, feature engineering, or train/test split logic.

## Workflow expectations

- Start non-trivial tasks (new modules, schema changes, anything touching the ML pipeline or rule engine) in **plan mode**. Small one-file fixes don't need it.
- If a plan conflicts with something in `itikcare-spec.md`, flag it rather than silently deviating.
- Prefer readable, well-commented code over clever one-liners, especially in the forecasting and rule-engine code — this needs to be walked through in a thesis defense.

## Correction log

*(Add a note here only after correcting Claude twice on the same thing — keep this file lean, delete stale entries during cleanup.)*
