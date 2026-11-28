"""Django settings for the itikcare project."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ---------- paths ----------
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

# Runtime-written files (uploads, trained models); point DJANGO_DATA_DIR at a persistent volume on Railway
DATA_DIR = Path(os.environ.get('DJANGO_DATA_DIR') or BASE_DIR)
MODEL_DIR = DATA_DIR / 'models'


# ---------- core ----------
SECRET_KEY = os.environ['DJANGO_SECRET_KEY']

DEBUG = os.environ.get('DJANGO_DEBUG', 'False') == 'True'

# The manage.py subcommand being run ('' under gunicorn)
_MANAGE_COMMAND = sys.argv[1] if len(sys.argv) > 1 else ''

# Weather/model caches; disabled under the test runner so tests see fresh results
PERFORMANCE_CACHES_ENABLED = _MANAGE_COMMAND != 'test'

ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '').split(',')

# Must include the scheme (https://...) once served from a real domain
CSRF_TRUSTED_ORIGINS = [
    origin for origin in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',') if origin
]

# Public base URL for links in emails sent outside a request (reminder cron job)
SITE_URL = os.environ.get(
    'DJANGO_SITE_URL', CSRF_TRUSTED_ORIGINS[0] if CSRF_TRUSTED_ORIGINS else ''
).rstrip('/')


# ---------- HTTPS / proxy (all off by default; enable one at a time after HTTPS works) ----------
DJANGO_BEHIND_PROXY = os.environ.get('DJANGO_BEHIND_PROXY', 'False') == 'True'
if DJANGO_BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    # USE_X_FORWARDED_HOST is intentionally NOT set (it broke ALLOWED_HOSTS on Railway)
    # Real client IP for django-ratelimit (REMOTE_ADDR is the proxy's address)
    RATELIMIT_IP_META_KEY = 'accounts.ratelimit.client_ip'

SECURE_SSL_REDIRECT = os.environ.get('DJANGO_SECURE_SSL_REDIRECT', 'False') == 'True'
SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_SECURE_HSTS_SECONDS', '0'))

# Secure cookies are never sent over plain http, so tying them to DEBUG is safe
# How long a login lasts (Django's default is two weeks). Configurable so the
# "session expiring" warning can be exercised without waiting two weeks.
SESSION_COOKIE_AGE = int(os.environ.get('DJANGO_SESSION_AGE_SECONDS', 60 * 60 * 24 * 14))
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG


# ---------- forecasting ----------
# True = train on real logs plus filled free-range gaps (forecasting/gapfill.py).
# Off until the adviser approves it; synthetic days are never saved to the database.
FORECAST_GAP_FILL = os.environ.get('FORECAST_GAP_FILL', 'False') == 'True'


# ---------- optional integrations ----------
# Farm coordinates for the weather prefill (farm/weather.py); unset = prefill disabled
FARM_LATITUDE = os.environ.get('FARM_LATITUDE')
FARM_LONGITUDE = os.environ.get('FARM_LONGITUDE')

# "Sign in with Google"; unset = button hidden
GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET')


# ---------- apps ----------
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    # staticfiles + skip Tailwind source folder in collectstatic (itikcare/apps.py)
    'itikcare.apps.ItikCareStaticFilesConfig',
    'accounts',
    'farm',
    'forecasting',
    'recommendations',
    'dashboard',
    'notifications',
]

AUTH_USER_MODEL = 'accounts.User'

# Login by username, email or full name
AUTHENTICATION_BACKENDS = [
    'accounts.auth_backends.UsernameEmailOrFullNameBackend',
    'django.contrib.auth.backends.ModelBackend',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise must come right after SecurityMiddleware
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    # Language switch; must be after Session and before Common
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'itikcare.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.template.context_processors.i18n',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'accounts.context_processors.google_oauth_enabled',
                'notifications.context_processors.notifications',
                'itikcare.context_processors.static_files',
            ],
        },
    },
]

WSGI_APPLICATION = 'itikcare.wsgi.application'


# ---------- database ----------
# PostgreSQL by default; DB_ENGINE=sqlite is an escape hatch for hosts without Postgres
if os.environ.get('DB_ENGINE', 'postgresql') == 'sqlite':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ['DB_NAME'],
            'USER': os.environ['DB_USER'],
            'PASSWORD': os.environ['DB_PASSWORD'],
            'HOST': os.environ['DB_HOST'],
            'PORT': os.environ['DB_PORT'],
            # Reuse connections across requests and replace dropped ones
            'CONN_MAX_AGE': 60,
            'CONN_HEALTH_CHECKS': True,
        }
    }
    # Only for external managed Postgres over the internet (e.g. DB_SSLMODE=require)
    if os.environ.get('DB_SSLMODE'):
        DATABASES['default']['OPTIONS'] = {'sslmode': os.environ['DB_SSLMODE']}

# Shared DB-table cache so rate-limit counters are shared across gunicorn workers
# (create the table once with `manage.py createcachetable`)
if os.environ.get('DJANGO_SHARED_CACHE', 'False') == 'True':
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
            'LOCATION': 'django_cache',
        }
    }


# ---------- password validation ----------
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# ---------- internationalization ----------
LANGUAGE_CODE = 'en'

# English/Filipino; rebuild .mo files with `python manage.py compile_translations`
LANGUAGES = [
    ('en', 'English'),
    ('fil', 'Filipino'),
]
LOCALE_PATHS = [BASE_DIR / 'locale']


# Philippine time (UTC+8); UTC made same-day entries look like future dates
TIME_ZONE = 'Asia/Manila'

USE_I18N = True

USE_TZ = True


# ---------- static & media files ----------
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
# Filled by `manage.py collectstatic`
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Farmer-uploaded avatars (served via itikcare/urls.py)
MEDIA_URL = 'media/'
MEDIA_ROOT = DATA_DIR / 'media'

# Hashed (long-cache) static storage only where staticfiles.json is guaranteed to exist:
# during collectstatic, or once the manifest exists. Never under the test runner.
USE_HASHED_STATIC = _MANAGE_COMMAND != 'test' and (
    _MANAGE_COMMAND == 'collectstatic' or (STATIC_ROOT / 'staticfiles.json').exists()
)

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': (
            'whitenoise.storage.CompressedManifestStaticFilesStorage'
            if USE_HASHED_STATIC
            else 'whitenoise.storage.CompressedStaticFilesStorage'
        ),
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ---------- authentication redirects ----------
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'dashboard'


# ---------- email ----------
# Used for password reset and ADMINS error alerts; prints to console when no provider is set
EMAIL_HOST = os.environ.get('DJANGO_EMAIL_HOST')
# Brevo (HTTPS API) takes precedence; works where outbound SMTP is blocked
BREVO_API_KEY = os.environ.get('BREVO_API_KEY')
if BREVO_API_KEY:
    EMAIL_BACKEND = 'accounts.email_backend.BrevoEmailBackend'
elif EMAIL_HOST:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_PORT = int(os.environ.get('DJANGO_EMAIL_PORT', '587'))
    EMAIL_HOST_USER = os.environ.get('DJANGO_EMAIL_HOST_USER', '')
    EMAIL_HOST_PASSWORD = os.environ.get('DJANGO_EMAIL_HOST_PASSWORD', '')
    EMAIL_USE_TLS = os.environ.get('DJANGO_EMAIL_USE_TLS', 'True') == 'True'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = os.environ.get('DJANGO_DEFAULT_FROM_EMAIL', 'webmaster@localhost')

# Error-alert recipients; .env format "Name:email,Name:email"
ADMINS = [
    tuple(entry.split(':', 1))
    for entry in os.environ.get('DJANGO_ADMINS', '').split(',')
    if ':' in entry
]
SERVER_EMAIL = os.environ.get('DJANGO_SERVER_EMAIL', DEFAULT_FROM_EMAIL)


# ---------- logging ----------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'app.log',
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'verbose',
        },
        # Emails ADMINS on unhandled exceptions
        'mail_admins': {
            'level': 'ERROR',
            'class': 'django.utils.log.AdminEmailHandler',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'INFO',
    },
    'loggers': {
        'django.request': {
            'handlers': ['mail_admins'],
            'level': 'ERROR',
            'propagate': True,
        },
    },
}
(BASE_DIR / 'logs').mkdir(exist_ok=True)
