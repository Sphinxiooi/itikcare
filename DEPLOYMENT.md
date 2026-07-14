# Deploying ItikCare to a VM

This covers a self-managed Linux VM/VPS you fully control (a DigitalOcean
droplet, a university server, etc.). It assumes you can SSH in, install
packages with `apt`, and point a domain's DNS at the box.

If you ever move to a managed PaaS (Render/Railway/Heroku-style, ephemeral
containers) instead, read the [PaaS caveats](#if-you-move-to-a-paas-instead)
section before following these steps as-is — two pieces of this app's design
assume a single, long-lived filesystem.

## 1. Server prep

```bash
sudo apt update
sudo apt install python3.13 python3.13-venv mysql-server nginx certbot python3-certbot-nginx git
```

Create a dedicated user to run the app (matches `deploy/itikcare.service`,
which assumes `/home/itikcare/app`):

```bash
sudo adduser --disabled-password itikcare
sudo -iu itikcare
git clone <your-repo-url> app
cd app
python3.13 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 2. Database

```bash
sudo mysql -e "CREATE DATABASE itikcare CHARACTER SET utf8mb4;"
sudo mysql -e "CREATE USER 'itikcare_user'@'localhost' IDENTIFIED BY 'REPLACE_ME';"
sudo mysql -e "GRANT ALL PRIVILEGES ON itikcare.* TO 'itikcare_user'@'localhost';"
```

## 3. `.env`

Copy `.env.example` to `.env` and fill it in. Two things must **not** be
copied from your dev `.env` if you have one — generate fresh values instead:

```bash
# New SECRET_KEY (never reuse the dev one):
venv/bin/python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# New DB_PASSWORD matching whatever you set in step 2 above.
```

Set at minimum: `DJANGO_DEBUG=False`, `DJANGO_ALLOWED_HOSTS=itikcare.example.com`,
`DB_*` to match step 2. Leave the `DJANGO_BEHIND_PROXY` /
`DJANGO_SECURE_SSL_REDIRECT` / `DJANGO_SECURE_HSTS_SECONDS` block at its
defaults (all off) for now — see step 6.

## 4. Migrate, seed, collect static

```bash
venv/bin/python manage.py migrate
venv/bin/python manage.py createsuperuser
venv/bin/python manage.py collectstatic --noinput
```

`collectstatic` populates `staticfiles/` (gitignored), which
`WhiteNoiseMiddleware` (see `itikcare/settings.py`) serves directly out of the
gunicorn process — nginx doesn't need its own static file block.

## 5. gunicorn via systemd

```bash
sudo cp deploy/itikcare.service /etc/systemd/system/
# edit User/Group/WorkingDirectory/paths in the copied file if your setup
# differs from /home/itikcare/app
sudo systemctl daemon-reload
sudo systemctl enable --now itikcare
sudo systemctl status itikcare
```

At this point the app is running on `127.0.0.1:8000` but not yet reachable
from outside the box.

## 6. nginx + HTTPS, then flip the security env vars on

```bash
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/itikcare
# edit server_name to your real domain
sudo ln -s /etc/nginx/sites-available/itikcare /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Visit `http://itikcare.example.com` and confirm the app loads over plain
HTTP first. Then:

```bash
sudo certbot --nginx -d itikcare.example.com
```

This rewrites the nginx config to add TLS and an http→https redirect. Once
you've confirmed `https://itikcare.example.com` works, edit `.env` and flip,
**in this order** (each one only after confirming the previous one didn't
break access):

1. `DJANGO_BEHIND_PROXY=True` — lets Django trust nginx's
   `X-Forwarded-Proto` header, so it correctly sees requests as HTTPS.
2. `DJANGO_SECURE_SSL_REDIRECT=True` — redirects any stray plain-HTTP
   request to HTTPS at the Django level too.
3. `DJANGO_SECURE_HSTS_SECONDS=31536000` (one year) — only once you're
   confident HTTPS is staying up; HSTS tells browsers to *refuse* plain HTTP
   to this domain for that long, which is hard to walk back quickly.

Restart after each change: `sudo systemctl restart itikcare`.

## 7. Email (optional)

Password-reset emails print to `journalctl -u itikcare` (the console email
backend) until `DJANGO_EMAIL_HOST` is set in `.env` to real SMTP credentials
— see the commented block in `.env.example`. Farmers who never gave an email
at signup can't self-service reset either way; reset their password for them
via `/admin/`.

## 8. "Sign in with Google" (optional)

Farmers who sign in with Google never have a local password to forget in the
first place, so this is worth setting up even before email/SMTP above.

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project (or reuse one), then **APIs & Services → Credentials → Create
   Credentials → OAuth client ID**, application type **Web application**.
2. Under **Authorized redirect URIs**, add both:
   - `http://localhost:8000/accounts/google/callback/` (local dev)
   - `https://itikcare.example.com/accounts/google/callback/` (your real
     domain, once step 6 above has HTTPS working)
3. Copy the generated **Client ID** and **Client secret** into `.env`:
   ```
   GOOGLE_OAUTH_CLIENT_ID=...
   GOOGLE_OAUTH_CLIENT_SECRET=...
   ```
4. Restart the app (`sudo systemctl restart itikcare`). The "Sign in with
   Google" button appears on the login/signup pages automatically once both
   values are set — see `accounts/context_processors.py`.

Leaving both blank is fine; local username/password signup and login are
unaffected either way.

## Rate limiting caveat

Signup, login, and the Google sign-in callback are all rate-limited (see
`accounts/views.py`) using Django's default cache (in-memory, per-process)
as the counter store. With the single
gunicorn worker set in `deploy/gunicorn_conf.py`, this is accurate. If you
ever raise `workers` above 1, each worker keeps its own separate counter, so
the effective limit becomes `rate × workers` — configure a shared `CACHES`
backend (e.g. Redis) at that point to keep it accurate.

## If you move to a PaaS instead

Two pieces of this app assume one long-lived filesystem shared by every
request, which the steps above satisfy on a single VM but a typical
ephemeral/multi-instance PaaS does not:

- **Retraining** (`forecasting/services.py::trigger_retrain`) launches a
  detached `subprocess.Popen` running `manage.py train_forecast_model` in
  the background. On a platform that recycles or scales worker instances,
  that subprocess can vanish mid-run, or spawning child processes may not be
  permitted at all.
- **Model storage**: each farmer's trained model is a `.joblib` file under
  `models/` on local disk. A second instance (or a redeployed/replaced one)
  won't see models trained by the first.

Moving to that kind of platform would mean replacing the subprocess retrain
with a real task queue (Celery + Redis, or `django-rq`) and switching model
storage to something shared (object storage, or a mounted volume all
instances share) — a larger change than this deployment pass covers.
