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

- `accounts` — custom User model (role: farmer/admin); self-service signup
  (optional email/address — address is geocoded for weather prefill, email
  enables self-service password reset) plus admin-created accounts via
  `/admin/`; optional "Sign in with Google" (`accounts/google_oauth.py`,
  see `.env.example`/`DEPLOYMENT.md`) that creates or logs into an account
  with no local password to ever reset
- `farm` — Flock (with caging-period/generation lifecycle), DailyLog,
  DailyLogEdit (audit trail for historical data edits), CSV bulk import; also
  owns the Log Daily Data and Farm Records (list + audited edit) pages.
  `farm/weather.py` prefills temperature/humidity suggestions on the daily log
  form from a live weather API (Open-Meteo), geocoded to each farmer's own
  address when set, falling back to the `FARM_LATITUDE`/`FARM_LONGITUDE`
  settings otherwise — the farmer can always override the prefilled values by
  hand, since the spec scopes temperature/humidity as ultimately manual entry
- `forecasting` — Forecast model, the Random Forest training pipeline
  (`forecasting/pipeline.py`), and the `train_forecast_model` management
  command (rolling retraining, per-owner models, `--tune`/`--strict` flags);
  also owns the Forecast & Recommendations page
- `recommendations` — Recommendation model + the forward-chaining rule engine
  (`recommendations/engine.py`, `recommendations/rules.py`), combined with RF
  feature importance, with every recommendation traceable to the rule/variable
  that triggered it
- `dashboard` — main landing page aggregating forecasts, recommendations, and
  recent farm records

The UI (sidebar layout, login screen, and all pages above) matches the Figma
prototype in `prototype/`.

## Deployment

For running this somewhere other than your own machine, see `DEPLOYMENT.md`.
