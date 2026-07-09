# ItikCare

Web-based egg yield forecasting and prescriptive analytics for a small-scale itik
(duck) farm. See `itikcare-spec.md` for the full build spec and `CLAUDE.md` for
project instructions.

## Stack

Python 3.13, Django 5.2, MySQL, Tailwind CSS (standalone CLI, no Node/npm), scikit-learn.

## First-time setup

1. Create and activate a virtual environment, then install dependencies:
   ```
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in real values (secret key, DB credentials).
3. Make sure MySQL is running and the `itikcare` database + a DB user exist (see
   `.env.example` for the expected variable names).
4. Download the Tailwind standalone CLI binary (Windows x64) into the project root as
   `tailwindcss.exe` — it is not committed to git:
   ```
   Invoke-WebRequest -Uri "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-windows-x64.exe" -OutFile "tailwindcss.exe"
   ```
5. Run migrations and create a superuser:
   ```
   python manage.py migrate
   python manage.py createsuperuser
   ```

## Running the app

Two terminals:

```
# Terminal 1 — Django dev server
python manage.py runserver

# Terminal 2 — Tailwind CSS watcher (rebuilds static/dist/output.css on change)
.\tailwindcss.exe -i .\static\src\input.css -o .\static\dist\output.css --watch
```

For a one-off production-style build (minified, no watch):

```
.\tailwindcss.exe -i .\static\src\input.css -o .\static\dist\output.css --minify
```

## Project structure

- `accounts` — custom User model (role: farmer/admin)
- `farm` — Flock, DailyLog, DailyLogEdit (audit trail for historical data edits);
  also owns the Log Daily Data and Farm Records (list + audited edit) pages
- `forecasting` — Forecast model + `train_forecast_model` management command
  (Random Forest training pipeline — currently a scaffold, not yet implemented);
  also owns the Forecast & Recommendations page
- `recommendations` — Recommendation model + rule engine (forward chaining —
  currently a scaffold, not yet implemented)
- `dashboard` — main landing page aggregating forecasts, recommendations, and
  recent farm records

The UI (sidebar layout, login screen, and all four pages above) matches the Figma
prototype in `prototype/`. Three intentional deviations from the mockup: the weather
box on the dashboard reads from today's own DailyLog entry rather than a live
third-party weather API (spec explicitly scopes temperature/humidity as manual
entry, no external integrations); there's no self-registration/"Create Account"
flow (accounts are admin-created via `/admin/`, per the spec's single-farm scope);
and the mockup's "Refresh" button on the Next 3-day Forecast card is omitted,
since regenerating a forecast is a periodic retraining/pipeline action (see
`train_forecast_model`), not something that makes sense to trigger inline from a
page request.

The Random Forest model and the rule engine are both stubs (see
`forecasting/management/commands/train_forecast_model.py` and
`recommendations/engine.py`) — the dashboard, forecast, and recommendations pages
render correctly against empty data and will populate once those are implemented.
