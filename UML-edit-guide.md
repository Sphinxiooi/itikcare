# Step-by-Step: Editing the Class Diagram to Match the Code

Goal: end up with a diagram where **every box, attribute, and operation can be pointed
to in the code**, without over-complicating it. Work through the steps in order. Each
step is a small, self-contained edit to your existing `UMLdigaram.png` source file.

Companion reference: `UML-class-diagram-spec.md` (full field list + Mermaid skeleton).

---

## Step 0 — Prep

1. Keep the original. Save a copy as `UMLdiagram_proposal_v1` (or export the current PNG)
   so you can show "proposal vs. as-built" if the panel asks what changed.
2. Open the editable source (draw.io / Lucidchart / Figma), not the PNG.
3. Add a small **legend box** in a corner:
   `« » = stereotype/constraint · PK = primary key · FK = foreign key · M2M = many-to-many`
   `- private attribute · + public operation · /name = derived (not stored)`

---

## Step 1 — Fix the User class

| In your diagram now | Action | Result |
|---|---|---|
| `User_ID : Integer (PK)` | rename | `- id : Integer «PK»` |
| `Username : String` | keep, mark private | `- username : String` |
| `Password : String` | keep, add note | `- password : String «hashed»` |
| `Farm_Name : String` | **DELETE** | *(no such field in code)* |
| `Location : String` | replace with 3 fields | `- address : String` `- latitude : Decimal «null»` `- longitude : Decimal «null»` |
| `ManageFlocks()` | **DELETE** | *(handled in views/services)* |
| `LogDailyLog()` | **DELETE** | *(handled in views/services)* |

**Add these attributes** (they exist in `accounts/models.py` and matter for the story):
- `- role : String «farmer | admin»`
- `- is_foundation_farmer : Boolean` — add a note: *"seeds new farmers' bootstrap model"*
- `- google_sub : String «null, unique»` — Google sign-in
- `- avatar : Image «null»`

**Add a class-name note:** `«extends Django AbstractUser»` — so you don't have to draw
`is_active`, `date_joined`, etc.

**Operations (real ones only):** `+ save()`, `+ get_foundation_farmer()`

---

## Step 2 — Fix the Flock class

| In your diagram now | Action | Result |
|---|---|---|
| `Flock_ID : Integer (PK)` | rename | `- id : Integer «PK»` |
| `User_ID : Integer (FK)` | rename | `- owner : FK → User` |
| `Flock_Average_Age : Integer` | **DELETE** or mark derived | `/ average_age` (slash = derived, computed in `farm/services.py`) |
| `Flock_Initial_Size : Integer` | **DELETE** | *(size is snapshotted per DailyLog)* |
| `Current_Size : Integer` | **DELETE** | *(size is snapshotted per DailyLog)* |
| `CalculateAverageAge()` | **DELETE** | *(→ `current_flock_age_weeks()` in service layer)* |
| `UpdateSize()` | **DELETE** | *(size isn't stored, so nothing to update)* |

**Add these attributes:**
- `- generation_number : Integer` — note: *"new row = flock generation reset"*
- `- started_on : Date`
- `- is_active : Boolean`
- `- is_caged : Boolean` — note: *"logging/forecasts pause when free-range"*
- `- pending_flock_size : Integer «null»`
- `- pending_flock_age_weeks : Integer «null»`
- `- pending_feed_intake_kg : Decimal «null»`

**Add constraint note:** `«unique (owner, generation_number)»`

**Operations:** none (or `+ __str__()` if your rubric wants at least one).

---

## Step 3 — Fix the DailyLog class

| In your diagram now | Action | Result |
|---|---|---|
| `Log_ID : Integer (PK)` | rename | `- id : Integer «PK»` |
| `Flock_ID : Integer (FK)` | rename | `- flock : FK → Flock` |
| `User_ID : Integer (FK)` | rename | `- recorded_by : FK → User` |
| `Log_Date : Date` | rename | `- date : Date` |
| `Egg_Collected_Count : Integer` | rename | `- egg_count : Integer «0..1000»` |
| `Feed_Intake : Decimal` | rename | `- feed_intake_kg : Decimal «0..150»` |
| `Temperature : Decimal` | rename | `- temperature_c : Decimal «0..45»` |
| `Humidity : Decimal` | rename | `- humidity_pct : Decimal «0..100»` |
| `RecordEggCount()` | **DELETE** | *(form + view)* |
| `RecordFeedIntake()` | **DELETE** | *(form + view)* |
| `CaptureManualData()` | **DELETE** | *(→ `log_daily_data` view + `DailyLogForm`)* |
| `ValidateData()` | replace with real method | `+ clean()` — model-level validation |

**Add these attributes:**
- `- flock_size : Integer «1..100000»` — note: *"snapshot for that day"*
- `- flock_age_weeks : Integer «1..150»` — note: *"snapshot for that day"*
- `- caging_period : Integer` — note: *"segments training data; not fed to the RF model"*
- `- is_locked : Boolean` — note: *"true once a model trained on it → immutable"*
- `- created_at : DateTime`
- `- updated_at : DateTime`

**Add constraint note:** `«unique (flock, date)»`

**Operations:** `+ clean()`, `+ __str__()`

---

## Step 4 — Fix the Forecast class

| In your diagram now | Action | Result |
|---|---|---|
| `Forecast_ID : Integer (PK)` | rename | `- id : Integer «PK»` |
| `Log_ID : Integer (FK)` | **replace** — see Step 7 | remove this single FK; becomes an M2M `source_logs` |
| `Forecast_Date : Date` | rename | `- forecast_date : Date` |
| `Predicted_Egg_Yield : Decimal` | rename + expand | `- predicted_daily_yield : Decimal` |
| `Prediction_Horizon : String` | **DELETE** | *(not stored — implicit in which field you read)* |
| `generatePrediction()` | **DELETE** | *(→ `generate_forecast()` in `forecasting/services.py`)* |
| `useHistoricalData()` | **DELETE** | *(→ `forecasting/pipeline.py`)* |

**Add these attributes:**
- `- flock : FK → Flock` — **this link is missing from your diagram entirely**
- `- predicted_tri_day_yield : Decimal`
- `- predicted_next_day1_yield : Decimal «null»`
- `- predicted_next_day2_yield : Decimal «null»`
- `- predicted_next_day3_yield : Decimal «null»`
- `- feature_importances : JSON` — note: *"RF importance scores; feeds the rule engine"*
  (thesis-critical — do not skip this one)
- `- model_version : String`
- `- generated_at : DateTime`

**Add constraint note:** `«unique (flock, forecast_date)»`

**Operations:** `+ __str__()`

---

## Step 5 — Fix the Recommendation class

| In your diagram now | Action | Result |
|---|---|---|
| `Recommendation_ID : Integer (PK)` | rename | `- id : Integer «PK»` |
| `Forecast_ID : Integer (FK)` | rename | `- forecast : FK → Forecast` |
| `Message : Text` | keep | `- message : Text` |
| `Recommendation_Type : String` | replace | `- priority : String «low..high»` |
| `Influencing_Variable : String` | rename | `- triggered_by : String` — note: *"the variable/rule that fired"* |
| `Action_Suggestion : Text` | **DELETE / merge** | *(merged into `message` in code)* |
| `generateRecommendation()` | **DELETE** | *(→ `evaluate_rules()` in `recommendations/rules.py`)* |
| `applyPrescriptiveLogic()` | **DELETE** | *(→ `recommendations/rules.py`)* |

**Add:** `- created_at : DateTime`

**Operations:** `+ __str__()`

---

## Step 6 — Add the DailyLogEdit class (new box)

CLAUDE.md requires an audit trail for DailyLog edits — it's a real table
(`farm/models.py`), and the panel will likely ask "how do you prevent silent
overwrites of farm data?" This box is your answer.

```
DailyLogEdit
- id : Integer «PK»
- daily_log : FK → DailyLog
- changed_by : FK → User
- field_name : String
- old_value : String
- new_value : String
- changed_at : DateTime
+ __str__()
```

Note on the box: *"one row per edited field — an edit touching 3 fields creates 3 rows"*

---

## Step 7 — Redo the relationships

Delete all existing association lines and redraw:

| From | | To | Label | Notes |
|---|---|---|---|---|
| User `1` | —— | `0..*` Flock | owns | |
| User `1` | —— | `0..*` DailyLog | records | |
| User `1` | —— | `0..*` DailyLogEdit | makes | |
| Flock `1` | —— | `0..*` DailyLog | has | |
| Flock `1` | —— | `0..*` Forecast | has | **new link** |
| DailyLog `1` | —— | `0..*` DailyLogEdit | audited by | |
| DailyLog `0..*` | ——◆ | `0..*` Forecast | source_logs | **many-to-many** — draw as a plain association with `*` on both ends, label `source_logs` |
| Forecast `1` | —— | `0..*` Recommendation | generates | |

Change every `1..*` from the old diagram to `0..*` (a new farmer has zero flocks, a new
flock has zero logs, etc. — `0..*` is the honest multiplicity).

Optional: `User 1 —— 0..* PasswordResetCode` if you add that box (Step 8).

---

## Step 8 — (Optional) PasswordResetCode

Only add if you want the auth flow represented. It's supporting infrastructure, not core
domain, so it's fine to leave off a conceptual diagram and mention it in the text instead.

```
PasswordResetCode
- id : Integer «PK»
- user : FK → User
- code_hash : String
- created_at : DateTime
- expires_at : DateTime
- attempts : SmallInteger
- consumed_at : DateTime «null»
```

---

## Step 9 — Decide how to show operations (pick ONE)

You've now deleted 12 invented methods. Choose how to handle behavior:

**Option A — recommended, simplest.** Leave the entity classes with only their real
methods (`save`, `clean`, `__str__`, `get_foundation_farmer`). Add ONE note box:

```
«Application logic — service modules»
generate_forecast()        forecasting/services.py
trigger_retrain()          forecasting/services.py
build_feature_frame() ...  forecasting/pipeline.py
evaluate_rules()           recommendations/rules.py
get_active_flock() ...     farm/services.py
```

When asked "where's the forecasting method?" → point at the note, then the file.

**Option B — if your rubric demands operations in classes.** Add exactly 3 service
classes with real method names, linked to the models with dashed `«uses»` arrows:
`ForecastService`, `RuleEngine`, `FarmService` (method lists in
`UML-class-diagram-spec.md` section 4). Don't add more than 3.

Do **not** put invented method names back on the entity classes.

---

## Step 10 — Visibility pass (encapsulation)

- Prefix every attribute with `-` (private).
- Prefix every operation with `+` (public).
- If any helper is internal-only, use `-` on it. (On the entity classes there are none —
  they're all public.)

This is what makes the diagram *show* encapsulation instead of just implying it.

---

## Step 11 — Consistency check (do this before exporting)

For every element on the canvas, confirm you can name the file it lives in:

| Element | File |
|---|---|
| each class | `*/models.py` (User→accounts, Flock/DailyLog/DailyLogEdit→farm, Forecast→forecasting, Recommendation→recommendations) |
| each attribute | a field in that model |
| `clean()` | `farm/models.py:142` |
| `save()` / `get_foundation_farmer()` | `accounts/models.py:91` / `:108` |
| every `«unique (...)»` note | a `UniqueConstraint` in that model's `Meta` |
| the M2M line | `Forecast.source_logs` in `forecasting/models.py:15` |
| service note box | `forecasting/services.py`, `recommendations/rules.py`, `farm/services.py` |

If you can't point to it → delete it from the diagram.

---

## Step 12 — Write the change note + tell your adviser

1. In the thesis text (or a caption), add:
   *"The class diagram reflects the as-built system. It differs from the Chapter 3
   proposal in the data-model shape (derived vs. stored flock size/age, added audit
   trail, many-to-many forecast sourcing). These changes do not affect the forecasting
   or prescriptive acceptance thresholds."*
2. Email your adviser the before/after — per project rules, flag deviations, don't ship
   them silently. The threshold objectives are untouched, so this is a routine update.

---

## Quick checklist

- [ ] Original diagram archived as `_proposal_v1`
- [ ] Legend box added
- [ ] User: `Farm_Name` deleted, `Location` → address/lat/long, real methods only
- [ ] Flock: stored size/age deleted (or `/derived`), lifecycle fields added
- [ ] DailyLog: fields renamed, snapshots + `is_locked` + timestamps added, `clean()`
- [ ] Forecast: `flock` FK added, 5 yield fields, `feature_importances`, `model_version`
- [ ] Recommendation: `triggered_by` / `priority`, `Action_Suggestion` merged
- [ ] DailyLogEdit box added
- [ ] Relationships redrawn, `1..*` → `0..*`, M2M for source_logs
- [ ] Operations: Option A or B chosen, no invented method names remain
- [ ] `-` / `+` visibility on everything
- [ ] Every element traced to a file (Step 11)
- [ ] Adviser notified
