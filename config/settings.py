"""
Django settings for the ELD trip-planner backend.

Reads configuration from environment variables (optionally a local .env file)
so the same code runs in dev and on a hosted environment. See .env.example.
"""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env (if present) before reading any os.environ values.
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --- Core ---------------------------------------------------------------------

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-change-me-in-production",
)

DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

# Railway injects the public domain at runtime; trust it automatically.
RAILWAY_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
if RAILWAY_DOMAIN:
    ALLOWED_HOSTS.append(RAILWAY_DOMAIN)

# CSRF needs the full https origin (scheme + host) for the admin / browsable API.
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
if RAILWAY_DOMAIN:
    CSRF_TRUSTED_ORIGINS.append(f"https://{RAILWAY_DOMAIN}")


# --- Applications -------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "corsheaders",
    # Local
    "apps.trips",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static files in production; must sit just below security.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# --- Database -----------------------------------------------------------------
# Postgres in production via DATABASE_URL (Railway sets this automatically when a
# Postgres service is attached); SQLite locally when DATABASE_URL is unset.

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        ssl_require=not DEBUG and bool(os.environ.get("DATABASE_URL")),
    )
}


# --- Password validation ------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --- I18N / TZ ----------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# --- Static -------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# WhiteNoise: compressed, hashed static files served by the app process.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Production hardening -----------------------------------------------------
# Only enforced when DEBUG is off (i.e. on Railway), so local dev stays simple.

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True


# --- Django REST Framework ----------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
}


# --- CORS ---------------------------------------------------------------------
# Allow the React frontend (dev + hosted) to call the API.

CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:5173,http://127.0.0.1:5173",
)
CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", False)


# --- Third-party API configuration --------------------------------------------
# Free map / routing / geocoding services. See docs/eld-log-format.md.

ORS_API_KEY = os.environ.get("ORS_API_KEY", "")
ORS_BASE_URL = os.environ.get("ORS_BASE_URL", "https://api.openrouteservice.org")
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
NOMINATIM_BASE_URL = os.environ.get(
    "NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org"
)
# Nominatim's usage policy requires a descriptive User-Agent / contact.
GEOCODER_USER_AGENT = os.environ.get(
    "GEOCODER_USER_AGENT", "eld-trip-planner"
)
# Restrict geocoding (search + autocomplete) to these ISO 3166-1 alpha-2 country
# codes so users can only pick routable places — a US HOS trip can't be planned
# from another continent. Comma-separated; blank = worldwide (no filter).
GEOCODE_COUNTRIES = os.environ.get("GEOCODE_COUNTRIES", "US")

# A trip's duty timeline is built in the local time zone of its current location
# (resolved from coordinates) — real ELD logs are kept in one fixed zone, not UTC.
# This IANA zone is the fallback when the location's zone can't be resolved.
FALLBACK_TIMEZONE = os.environ.get("FALLBACK_TIMEZONE", "America/Chicago")


# --- HOS / trip assumptions (from the assessment brief) -----------------------
# Centralized so the simulator and serializers share one source of truth.

HOS_SETTINGS = {
    "CYCLE_HOURS": 70,          # 70-hour / 8-day cycle
    "CYCLE_DAYS": 8,
    "MAX_DRIVING_HOURS": 11,    # 11-hour driving limit
    "MAX_WINDOW_HOURS": 14,     # 14-hour driving window
    "DRIVING_BEFORE_BREAK": 8,  # 30-min break required after 8h cumulative driving
    "BREAK_DURATION_HOURS": 0.5,
    "REQUIRED_REST_HOURS": 10,  # 10 consecutive hours off resets the daily clocks
    "RESTART_HOURS": 34,        # 34-hour restart for the weekly cycle
    "PICKUP_HOURS": 1.0,        # 1h on-duty at pickup
    "DROPOFF_HOURS": 1.0,       # 1h on-duty at dropoff
    "FUEL_INTERVAL_MILES": 1000,  # fuel at least every 1,000 miles
    "FUEL_STOP_HOURS": 0.5,     # assumed on-duty time per fuel stop
    "AVG_SPEED_MPH": 55,        # fallback speed if routing API gives no duration
}
