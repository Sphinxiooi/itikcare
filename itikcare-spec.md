# ItikCare — Working Spec

**Full name:** ItikCare: A Web-Based Egg Yield Forecasting System for Itik Farming using Random Forest Regression
**Client / test farm:** Mr. Mario A. Realizan, Zone 5, Sitio Tabawan, Patag, Libmanan, Camarines Sur
**Figma prototype:** https://www.figma.com/design/TZqCWEen9aoTE3mQZXDWa6/itikcare?node-id=0-1&t=FBmxO4nrTV7NrB4n-0
**Purpose of this doc:** condensed build spec pulled from the full capstone thesis, meant to be handed to Claude Code directly (put this file in the project root, or reference key sections in `CLAUDE.md`).

---

## 1. What the system does

A web-based decision support system that replaces manual paper record-keeping for a small-scale itik (duck) egg farm. It has three modules:

1. **Farm Data Management** — structured logging of daily farm data with validation
2. **Egg Yield Forecasting** — Random Forest Regression model predicting daily and tri-day (3-day) egg yield
3. **Prescriptive Analytics** — rule-based recommendation engine (forward chaining + feature importance from the RF model) that turns forecasts into preventive actions

Core loop: farmer logs in → enters daily data → system validates & stores it → RF model combines new + historical data → generates forecast → prescriptive module turns forecast into recommendations → farmer sees forecast + recommendations on dashboard.

The farm runs a **3-day delivery cycle** to local balut and itlog na maalat (salted egg) buyers — this is why forecasts are daily *and* tri-day, not just daily.

---

## 2. Tech stack (already decided — don't deviate without asking)

| Layer | Choice |
|---|---|
| Backend | Python 3.13, Django 5 |
| Frontend | HTML, CSS, JavaScript, Tailwind CSS |
| Database | MySQL |
| ML | scikit-learn (Random Forest Regression) |
| Target browser | Chrome, responsive (desktop + mobile) |

---

## 3. Data model (from the thesis's UML class diagram)

Five core classes/entities:

- **User** — authentication, role (farmer/admin)
- **Flock** — flock size (number of ducks), flock age (weeks)
- **DailyLog** — daily manual entry: egg count, feed intake (kg/day), temperature (°C), humidity (%), date
- **Forecast** — model output: predicted daily yield, predicted tri-day yield, timestamp, links back to the DailyLog data it was generated from. Also carries a recursive best-effort predicted_next_day1/2/3_yield breakdown (dashboard's "Next 3-Day Forecast" panel) — 3 distinct day-by-day numbers derived by re-applying the same daily model iteratively, not a separately trained model or a split of the tri-day sum (see forecasting/services.py's `_predict_next_days`).
- **Recommendation** — output of the prescriptive module: linked to a Forecast, contains the actionable advice text and which rule/variable triggered it

Relationships: a Flock has many DailyLogs → DailyLogs feed the Forecast model → each Forecast can produce one or more Recommendations.

**Important:** the system allows **editing of historical DailyLog entries** (to handle real-world data entry mistakes/delayed reporting), but every edit must be tracked/audited — don't silently overwrite, log the change (old value, new value, timestamp) so data integrity for model retraining isn't compromised.

---

## 4. Input variables (forecasting model features)

All of these are **manually entered by the farmer** — no IoT/sensor integration in scope:

- Flock size (number of ducks)
- Flock age (average, in weeks) — peak laying period for itik is ~28-29 weeks
- Feed intake (kg/day)
- Temperature (°C) — read from farmer's own external device, entered manually
- Humidity (%) — same, manual entry

Dependent variable (what's being predicted): **daily egg yield** (and derived tri-day yield).

Known variable relationships to keep in mind when building/validating the model:
- Flock size → positive correlation with yield
- Feed intake → moderate positive correlation
- Temperature & humidity → negative correlation (heat stress lowers yield)
- Flock age → non-linear (rises to a peak around 28-29 weeks, then plateaus/declines)

---

## 5. Random Forest Regression — model requirements

- Preprocessing: handle missing values/outliers, normalize/scale numerical features
- Train/test split: 80:20
- Library: scikit-learn
- Designed for **periodic rolling retraining** as the farmer adds new daily data (not a one-time trained model)
- Must expose **feature importance scores** — these feed directly into the prescriptive module's rule logic

**Acceptance thresholds (from thesis Chapter 3 — use these to validate any model you build):**

| Metric | Target |
|---|---|
| MAE | ≤ 8% of average daily egg yield |
| RMSE | ≤ 10% of average daily egg yield |
| MAPE | ≤ 15% |
| R² | ≥ 0.75 |

---

## 6. Prescriptive Analytics module

- Rule-based system using **forward chaining** (IF-THEN rules) combined with **feature importance** from the RF model
- Inputs: the current forecast + current manual farm inputs (flock age, feed intake, temperature, humidity)
- Logic: prioritize recommendations by whichever variable has the highest feature importance for a potential yield drop (e.g. flag temperature/heat stress first if it's the dominant negative factor)
- Output: plain-language, actionable preventive advice (e.g. adjust feeding, flag environmental control needed) — written for a farmer, not a data scientist

**Acceptance thresholds:**

| Metric | Target |
|---|---|
| Concordance Rate (agreement with expert/actual best outcome) | ≥ 80% |
| Prescriptive Effectiveness Rate (recommendations rated useful/led to positive outcome) | ≥ 75% |
| False Recommendation Rate | ≤ 10% |

---

## 7. Explicit scope boundaries (don't build these — out of scope)

- No IoT/sensor hardware integration — temperature/humidity are always manual entry
- No disease diagnostics or meat-production features (egg yield only)
- No long-term/seasonal forecasting — short-term (daily/tri-day) only
- No financial management features (cost analysis, payroll, market transactions)
- Single-farm context — not designed for multi-farm/enterprise scale (yet)

---

## 8. User flow (from the thesis's Level 1 DFD)

1. User logs in → system checks credentials against user account DB
2. Farmer lands on dashboard
3. Farmer enters daily manual inputs via a data logging form
4. System validates the entry → saves to DailyLog
5. Forecasting process pulls daily + historical DailyLogs → runs Random Forest model → produces Forecast
6. Prescriptive module takes the Forecast + current inputs → generates Recommendation(s)
7. Dashboard displays: egg yield forecast (daily + tri-day) + recommendations + farm records

---

## 9. Testing requirements

- Functional testing across all modules (data logging, forecasting, prescriptive analytics)
- Model validation against the metrics in section 5
- Prescriptive module validation against the metrics in section 6
- **Layered data validation**, three levels:
  1. Client (farmer) validates daily entries
  2. DA-Libmanan (Dept. of Agriculture) provides domain expertise validation
  3. Researchers perform technical validation
- Final phase: User Acceptance Testing (UAT) with the farm owner + at least 2 other local itik farmers, using a Likert-scale questionnaire (usability, efficiency, accuracy perception, satisfaction) + follow-up interviews

---

## 10. Known data patterns — read before touching the training data

The farm operates a **semi-intensive setup**: ducks are let out to free-range roughly every 3 months, then caged again for a period of close monitoring. Daily logs only exist for the caged periods — this is why the historical dataset (`ItikCare_Cleaned_Dataset.csv`) has several multi-week/multi-month gaps in the date sequence, marked by the `Caging_Period` column. These gaps are **expected and correct, not missing/corrupted data.**

Implication for the model: treat each caged period as its own contiguous segment. Do not build lag/rolling features (e.g. "yesterday's yield," 7-day rolling average) that span across a gap — the days on either side aren't operationally connected.

**Flock generation resets:** occasionally flock age drops sharply in the data (e.g. from 107 weeks back down to 23 weeks). This reflects a real event — the previous flock was retired and a new, younger flock brought in — not a data entry error. The model/pipeline should be aware of which "generation" a row belongs to (a simple derived batch/generation ID based on age resets works), since a fresh flock and a mature flock behave differently even before accounting for age numerically.

**Male ratio:** `Number of Flocks` is a total bird count and includes males — standard practice caps males at no more than ~10% of the flock (needed for fertilization, but males don't lay eggs). This is not recorded as a separate column in the dataset. It partly explains why `Yield_Per_Bird` never approaches 1.0 even in healthy periods — the denominator includes some non-laying birds. Don't assume a fixed 90/10 split when interpreting the data; the actual male ratio varies day to day and isn't logged. If a more precise laying-female count becomes available later, revisit `Yield_Per_Bird` to use it as the denominator instead of total flock size.

**Flock size jumps:** flock size (Number of Flocks) sometimes jumps upward mid-cycle. This is usually legitimate — the farmer periodically adds ducks to the flock (often similar-aged birds, or additional males for breeding). Don't assume every increase is an error, but unusually large or fast jumps are still worth a quick sanity check with the farm owner before training, since a data-entry mistake and a real bulk restocking event can look similar in the raw numbers.

## 11. Notes for whoever (Claude) is building this

- This is a capstone project — code should be clean and explainable in a defense, not just functional. Prefer readable, well-commented implementations over clever ones, especially for the RF model and rule engine.
- Keep the prescriptive module's rules transparent and easy to trace back to a specific feature importance value — the thesis's evaluation methodology depends on being able to show *why* a recommendation was made.
- Because retraining is expected to happen periodically as real farm data comes in, design the model training/storage as a repeatable pipeline (e.g. a management command or script), not a one-off notebook.
