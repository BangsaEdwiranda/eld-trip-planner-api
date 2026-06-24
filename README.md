# ELD Trip Planner — Backend

Django + DRF backend for a full-stack app that takes trip details and outputs
route information plus auto-filled **ELD daily log sheets**, compliant with FMCSA
Hours-of-Service rules for property-carrying drivers (70hr/8day cycle).

The React frontend is a separate project (built later) that consumes this API.

> Domain references: [HOS rules](documentation/hos-rules.md) ·
> [ELD log format](documentation/eld-log-format.md).

## Stack

- Python 3.14, Django 6.0, Django REST Framework
- SQLite (dev) — swap for Postgres in production
- Free map APIs: OpenRouteService (geocoding + `driving-hgv` truck routing);
  Nominatim / OSRM as no-key fallbacks
- `timezonefinder` (offline) to resolve each trip's local time zone from coordinates

## Project layout

```
eld-trip-planner-api/
├─ config/                 # Django project (settings, urls, wsgi/asgi)
├─ apps/                   # all Django apps live here
│  └─ trips/               # main app
│     ├─ models.py         # Trip, LogSheet, LogSegment
│     ├─ serializers.py    # DRF input/output serializers
│     ├─ views.py          # TripViewSet: plan a trip end-to-end
│     ├─ urls.py           # /api/trips/ router
│     ├─ admin.py
│     ├─ tests/            # per-module suite (HOS, ELD, geo, routing, timezone, views)
│     └─ services/
│        ├─ routing.py     # geocoding + routing + reverse-geocode (ORS / Nominatim / OSRM)
│        ├─ timezone.py    # coords -> local time zone + trip start ("now")
│        ├─ geo.py         # pure polyline math (locate stops along the route)
│        ├─ hos.py         # Hours-of-Service simulator -> duty-status timeline
│        └─ eld.py         # timeline -> per-day ELD log sheets (24h off-duty-padded)
├─ documentation/          # domain references: HOS rules + ELD log format
├─ requirements.txt
└─ .env.example
```

## Architecture & conventions

- **Services are split by side effect.** `services/hos.py`, `eld.py`, and `geo.py`
  are **pure** (no DB or network) so the HOS/ELD/geometry logic is unit-testable;
  network lives only in `routing.py`, and persistence only in `views.py`. Every
  request funnels through `views._plan_trip` — read it top to bottom for the whole
  pipeline (geocode → route → timezone → HOS → locate stops → daily sheets → save).
- **HOS limits live in one place** — `HOS_SETTINGS` in `config/settings.py`
  (11h / 14h / 30-min break / 70h-8day / 1,000 mi fuel / etc.). Don't hardcode those
  numbers elsewhere; read them from there.
- **App label is pinned.** `apps/trips` sets `AppConfig.name = "apps.trips"` but
  `label = "trips"`, so DB tables stay `trips_*` — keep that label if you move or
  rename the app.
- **Timezone-aware timeline.** The HOS timeline is tz-aware; `eld.py` preserves
  `tzinfo` when splitting segments at midnight (a naive/aware comparison would crash).
- **Errors are clean 4xx, never 500.** Upstream geocoding/routing failures surface as
  a `400` with a user-friendly `detail`; the raw provider error is logged, not returned.
- **Style:** double-quoted strings, type hints, module/function docstrings, and
  `from __future__ import annotations` in the service modules.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env            # then edit values
python manage.py migrate
python manage.py runserver
```

Optional: `python manage.py createsuperuser` to use the admin at `/admin/`.

## Database & migrations

The database engine is chosen at runtime from `DATABASE_URL` (via `dj-database-url`):
**unset → SQLite** (`db.sqlite3`, local dev); **set → Postgres** (production). The
same committed migrations apply to both.

```bash
python manage.py makemigrations   # after changing models — commit the result
python manage.py migrate          # apply migrations to the configured database
```

Migrations live in `apps/trips/migrations/` and are checked into the repo, so the
schema history travels with the code. In production they run **automatically on
every deploy** — the Railway `startCommand` does `migrate --noinput` before
`collectstatic` and `gunicorn` (see `railway.toml`), so there's no manual prod
migration step. Re-running `migrate` is idempotent; already-applied migrations are
no-ops.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health/` | health check |
| GET | `/api/geocode/?q=` | place autocomplete (US-restricted suggestions) |
| POST | `/api/trips/` | plan a trip (see below) |
| GET | `/api/trips/` | list planned trips |
| GET | `/api/trips/{id}/` | retrieve a trip + its ELD log sheets |

### Plan a trip

```http
POST /api/trips/
Content-Type: application/json

{
  "current_location": "Chicago, IL",
  "pickup_location": "Des Moines, IA",
  "dropoff_location": "Denver, CO",
  "current_cycle_used_hours": 10
}
```

Response (`201`): the trip with resolved coordinates, route summary
(`total_distance_miles`, `total_duration_hours`, `route_geometry`), the trip
`timezone` (IANA), `stops` (each with `[lon, lat]` `coords` and a reverse-geocoded
`"City, ST"` `location`), and `log_sheets[]` — one per day, each off-duty-padded to
a full 24h with per-status `totals` and grid-ready `segments` (`status`,
`start_minute`, `end_minute`, `location`, `note`).

## Tests

```bash
python manage.py test
```

Network-free and fast. The HOS/ELD output is validated against an **independent
re-implementation** of the FMCSA limits (`tests/_helpers.py::HOSComplianceMixin`) —
so the simulator is checked against the rules, not its own output. Service and
API-layer tests mock the upstream geocoding/routing calls, and the full
`POST /api/trips/` flow is covered end-to-end (orchestration → persistence →
serialization).

## Deployment (Railway + Postgres)

The app runs on **gunicorn**, serves static files with **WhiteNoise**, and uses
**Postgres** via `DATABASE_URL` (SQLite is only the local fallback). Build and run
config (start command, healthcheck, restart policy) lives in
[`railway.toml`](railway.toml); Python version is pinned in `.python-version`.

1. Create a Railway project and **add a PostgreSQL service** to it.
2. Deploy this repo as a service (connect the GitHub repo, or `railway up`).
3. Set service variables (Railway → Variables):
   - `DATABASE_URL` = `${{ Postgres.DATABASE_URL }}` (reference the Postgres service)
   - `DJANGO_SECRET_KEY` = a long random string
   - `DJANGO_DEBUG` = `False`
   - `DJANGO_ALLOWED_HOSTS` = your Railway domain (e.g. `eld-trip-planner-api.up.railway.app`)
   - `CSRF_TRUSTED_ORIGINS` = your frontend origin (e.g. `https://your-app.vercel.app`)
   - `CORS_ALLOWED_ORIGINS` = same frontend origin
   - `ORS_API_KEY` = your OpenRouteService key (optional; blank uses OSRM demo)
   - `GEOCODE_COUNTRIES` = `US` (optional; comma-separated, blank = worldwide)
   - `FALLBACK_TIMEZONE` = `America/Chicago` (optional; used only if a location's
     zone can't be resolved)
   - `SECURE_HSTS_SECONDS` = `31536000` (optional, once HTTPS is confirmed)
   - `RAILWAY_PUBLIC_DOMAIN` is injected automatically — no need to set it.

On deploy `railway.toml` runs `migrate` → `collectstatic` → `gunicorn`. When
`DJANGO_DEBUG=False`, production security (SSL redirect, secure cookies,
HSTS, proxy SSL header) switches on automatically.

## Notes

- `services/hos.py` models the 11h, 14h, 30-min-break, 70hr/8day and 34h-restart
  rules; sleeper-berth splits and short-haul exceptions are out of scope per brief.
- **Trip time zone & start.** Each trip runs in the **local time zone of its current
  location** (resolved from coordinates by `services/timezone.py`, held constant for
  the whole trip). This mirrors how real ELDs keep a driver's record of duty status
  in one fixed *home-terminal* time zone even across time zones (FMCSA §395.8). The
  brief gives no start time and FMCSA defines no fixed shift start, so the duty day
  begins **"now"** in that zone — the moment the driver goes on duty. `FALLBACK_TIMEZONE`
  is used only when a location's zone can't be resolved.
  - **"Depart now" projection.** This is a trip *planner*, not a live ELD recorder:
    it assumes the driver **departs at the planning moment and starts by driving**
    (current → pickup). It does not model a chosen departure time, the driver's real
    current duty status, or a pre-trip on-duty inspection. Off-duty time before the
    start is shown as padding; the simplification is that planning-time = departure-
    time. An optional departure-time / pre-trip input would close this (out of scope).
- **ELD sheets total a full 24h.** `services/eld.py` pads each daily sheet with
  off-duty time at the edges (midnight → first duty, last duty → midnight), so every
  sheet is a complete 24-hour RODS page rather than a partial day.
- **Latency & caching.** A fresh plan makes concurrent geocode/route/reverse-geocode
  calls (~3–7s). Results are **cached** (`services/routing.py`, 30-day TTL on
  `geocode`/`route`/`reverse_geocode`), so repeated trips and popular cities return
  near-instantly. The default cache is per-process in-memory (`LocMemCache`); for a
  cache shared across gunicorn workers in production, point Django's `CACHES` at Redis.
- **Geocoding is restricted to the US by default** (`GEOCODE_COUNTRIES=US`). This is
  deliberate: the HOS simulator implements **US FMCSA §395 rules only**, so allowing
  Canadian/Mexican locations would apply US limits to trips governed by different
  rules — a confidently-wrong result. Restricting the location search to the US keeps
  inputs aligned with the rules being simulated, and as a bonus blocks unroutable
  picks (e.g. another continent). Widen to `US,CA,MX` only alongside per-jurisdiction
  HOS support. Note the country filter can't catch every unroutable pair — non-mainland
  US places (Hawaii, Puerto Rico) still fail routing, surfaced as a clean 4xx.
