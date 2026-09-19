# Deploying ItikCare on Railway

Two Railway services from this one repo (web + daily-reminder cron), one PostgreSQL
database, one volume. Steps marked **(Railway UI)** are dashboard clicks; Railway's exact
labels can shift, so if one doesn't match, trust the intent.

## What was verified locally, and what wasn't

Rehearsed against a scratch local PostgreSQL with the same variables as below
(`DJANGO_DEBUG=False`, `DJANGO_BEHIND_PROXY=True`, `DJANGO_SHARED_CACHE=True`, a separate
`DJANGO_DATA_DIR`): `migrate`, `createcachetable`, `collectstatic`, `loaddata` of the
export (632 objects, sequences correct), `train_forecast_model --strict` for both farmers,
and every main page returning 200 for Mario and Rodel with the built CSS served.

Mario's retrained model matched the existing local one metric for metric (daily MAE 13.58 /
RMSE 17.73 / R² 0.956; tri-day MAE 46.03 / RMSE 58.63 / R² 0.946), so a Railway retrain
reproduces today's forecasts.

**Not** testable on Windows: gunicorn itself (Linux-only), Railway's proxy, volume and
cron behaviour. Those are covered by the checklist at the bottom.

## 1. Before you push

- Rebuild the CSS if any template/CSS changed since it was last built:
  `.\tailwindcss.exe -i .\static\src\input.css -o .\static\dist\output.css --minify`
  (`static/dist/output.css` is committed on purpose, Railway has no Tailwind binary.)
- Run the full suite: `python manage.py test --keepdb`.
- Commit **all** new files: migrations (`farm/migrations/0012_*`,
  `recommendations/migrations/0003_*`), `static/images/*`, `railway.toml`,
  `.python-version`, `deploy/`, `accounts/ratelimit.py`, `accounts/templatetags/`.
- Do **not** commit `.env` or `deploy/farm_data.json` (both gitignored).

## 2. Create the project **(Railway UI)**

1. New Project → Deploy from GitHub repo → this repo. That creates the web service and
   reads `railway.toml` (start command: migrate → createcachetable → collectstatic →
   gunicorn).
2. Add → Database → PostgreSQL.
3. Web service → Settings → Volumes → add a volume mounted at `/data`.
4. Web service → Settings → Networking → Generate Domain. Region: Singapore, for both
   the service and the database if the option is offered.

## 3. Web service variables

Use Railway reference variables (`${{ ... }}`) so credentials stay in sync. Replace
`Postgres` with the database service's actual name.

| Variable | Value |
|---|---|
| `DJANGO_SECRET_KEY` | a new random string (`python -c "import secrets;print(secrets.token_urlsafe(50))"`), not the local one |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_ALLOWED_HOSTS` | `${{RAILWAY_PUBLIC_DOMAIN}}` (add your custom domain later, comma-separated) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://${{RAILWAY_PUBLIC_DOMAIN}}` |
| `DJANGO_BEHIND_PROXY` | `True` |
| `DJANGO_SHARED_CACHE` | `True` |
| `DJANGO_DATA_DIR` | `/data` |
| `WEB_CONCURRENCY` | `2` |
| `PYTHON_VERSION` | `3.13` |
| `RAILWAY_RUN_UID` | `0` (lets the app write to the root-owned volume) |
| `DB_NAME` | `${{Postgres.PGDATABASE}}` |
| `DB_USER` | `${{Postgres.PGUSER}}` |
| `DB_PASSWORD` | `${{Postgres.PGPASSWORD}}` |
| `DB_HOST` | `${{Postgres.PGHOST}}` |
| `DB_PORT` | `${{Postgres.PGPORT}}` |
| `FARM_LATITUDE` / `FARM_LONGITUDE` | copy from your local `.env` |

Optional, same as `.env.example`: `GOOGLE_OAUTH_*` (and add
`https://<domain>/accounts/google/callback/` to the Google console's redirect URIs),
`DJANGO_ADMINS`, and the Brevo settings in the Email section below.

Once the site loads over HTTPS, add `DJANGO_SECURE_SSL_REDIRECT=True`, then
`DJANGO_SECURE_HSTS_SECONDS=3600` (raise later). Do these last.

## 4. Load Mario's and Rodel's data

Refresh the export whenever the local data changed (it includes the admin account,
because the admin recorded most of the imported logs, and it blanks avatar files):

```powershell
python manage.py export_farm_data --owner-ids 2 3
```

Wait for the first deploy to finish (it runs `migrate`). Then, from your PC, point the
local `manage.py` at Railway's **public** database address (Postgres service → Variables:
`PGDATABASE`, `PGUSER`, `PGPASSWORD`, `RAILWAY_TCP_PROXY_DOMAIN`, `RAILWAY_TCP_PROXY_PORT`).
Shell variables win over `.env`, so nothing local is touched:

```powershell
$env:DB_NAME="<PGDATABASE>"; $env:DB_USER="<PGUSER>"; $env:DB_PASSWORD="<PGPASSWORD>"
$env:DB_HOST="<RAILWAY_TCP_PROXY_DOMAIN>"; $env:DB_PORT="<RAILWAY_TCP_PROXY_PORT>"
$env:DB_SSLMODE="prefer"
python manage.py showmigrations farm | Select-Object -Last 3   # confirms you hit Railway, all [X]
python manage.py loaddata deploy/farm_data.json               # "Installed 632 object(s)"
```

Run `loaddata` **once** (a second run just overwrites the same rows, harmless, but there's
no reason to). Close that PowerShell window afterwards so the overrides don't linger.

Mario keeps `is_foundation_farmer`, which every new signup's starter model trains on, so
loading Mario's data is what makes signup work. Both farmers keep their existing
passwords. Avatars must be re-uploaded in Account settings.

## 5. Retrain on Railway

Models live on the volume, not in git, so train once on the service (Railway CLI, linked
to the project):

```powershell
railway ssh
python manage.py train_forecast_model --owner-id 2 --strict
python manage.py train_forecast_model --owner-id 3 --strict
```

Use `--strict` **without** `--tune`. It saves a model only if every acceptance threshold
passes, and reproduces the currently deployed model exactly (~20 s each).

> **Automatic retrains fall back to untuned.** With `--tune`, Mario's *tri-day* model
> currently misses two thresholds (MAE 8.60% > 8%, RMSE 10.31% > 10%). The app's
> automatic retrain (`forecasting/services.py::trigger_retrain`) therefore runs
> `--tune --fallback-untuned --strict`: if the tuned models miss any threshold, they are
> discarded and the fixed-hyperparameter models are trained and scored on the same
> held-out rows instead. A tuned model that passes everything is still preferred. If
> neither passes, `--strict` leaves the previous model in place. Verified on Mario's real
> data (dry run): tuned fails, fallback passes with the same metrics as above. The metrics
> JSON records `"tuned"` and `"fell_back_from_tuned"` for each run.

No `railway ssh`? Temporarily set the web service's start command to
`sh -c 'python manage.py train_forecast_model --owner-id 2 --strict && python manage.py train_forecast_model --owner-id 3 --strict'`
(one deploy), then remove the override so `railway.toml` applies again.

## 6. Daily reminder cron service

1. Add another service from the same repo.
2. Settings → Config file path → `/deploy/railway-cron.toml` (runs
   `send_daily_log_reminders` at 09:00 UTC = 17:00 Manila).
3. Give it the same variables as the web service (copy them, or use shared variables).
   It needs no volume and no domain.

## Email (Brevo over HTTPS)

Password-reset codes and the daily reminder go through Django's email backend. Railway
blocks outbound SMTP on non-Pro plans, so Gmail SMTP can't be used there. Instead the app
sends through Brevo's HTTPS API (`accounts/email_backend.py`, free tier ~300 emails/day).
The message is identical to what the console backend prints on localhost; only the
delivery path differs.

1. Create a free Brevo account at brevo.com and finish its onboarding questions.
2. Brevo -> Settings -> Senders & IPs -> Senders -> add a sender (name + your Gmail
   address). Brevo emails a **6-digit code** to that inbox; enter it in Brevo to verify.
   **The From address must be a verified sender, or Brevo rejects every message.**
3. Brevo -> Settings -> SMTP & API -> **API Keys** tab (not "SMTP Keys", a different
   credential) -> Generate a new API key, name it "itikcare-railway" and copy it at once
   (it's shown only once). Never put it in git or `.env.example`.
4. Set these on **both** the web service and the cron service (the reminder is sent by
   the cron one):

| Variable | Value |
|---|---|
| `BREVO_API_KEY` | the API key |
| `DJANGO_DEFAULT_FROM_EMAIL` | the verified sender, e.g. `ItikCare <you@gmail.com>` |
| `DJANGO_SERVER_EMAIL` | the same address (used for error alerts to `DJANGO_ADMINS`) |

Leave every `DJANGO_EMAIL_HOST*` variable unset. When `BREVO_API_KEY` is set it takes
precedence over SMTP; with neither set, emails are just printed to the log.

Test it right after deploying: request a password reset for a user that has an email
address and check the code arrives. If nothing arrives, the deploy log shows
`Brevo API returned <status>: ...` with Brevo's own reason (most often an unverified
sender or a wrong key).

**Gmail-sender limitation (per Brevo's help center):** free-mail domains such as
`@gmail.com` cannot be authenticated in Brevo. Mail sent from one may be rejected or
filtered to spam, and until a domain is authenticated Brevo may replace the sender's
domain with `@brevosend.com`. So expect the reset code to arrive from a Brevo-looking
address, possibly in the **spam folder**. For a demo this works; for dependable delivery
use an address on a domain you own and authenticate it in Brevo (Settings -> Senders &
IPs -> Domains).

## Checklist after the first deploy

- [ ] Deploy logs show migrate, `collectstatic`, then gunicorn `Listening at: http://0.0.0.0:<port>`.
- [ ] `https://<domain>/` loads styled (CSS present).
- [ ] Mario and Rodel can log in; farm records, forecast and flock pages load.
- [ ] Logging a daily entry as Mario produces a forecast.
- [ ] Upload an avatar, redeploy, and it is still there (the volume works).
- [ ] `ls /data/models` (via `railway ssh`) shows both `forecast_model_*.joblib`.
- [ ] Signup as a throwaway user works (bootstrap training, may take up to ~1 minute), then delete it.
- [ ] Memory graph stays under the plan limit after a signup or retrain; if it OOMs, set `WEB_CONCURRENCY=1`.
- [ ] Cron service ran once at 09:00 UTC (or use "Run now" once).
