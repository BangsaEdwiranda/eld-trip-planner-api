"""Geocoding and routing via free map APIs.

Geocoding: OpenRouteService (Pelias) when an ORS key is set — reliable and keyed;
           Nominatim (OpenStreetMap, no key) as the fallback.
Routing:   OpenRouteService (free key) preferred; OSRM public demo as fallback.

These are thin wrappers returning plain dicts. Network calls are isolated here so
the HOS simulator (hos.py) can be unit-tested with stub route data. Any failure
(HTTP error, timeout, empty result) surfaces as ``RoutingError`` so the API layer
can return a clean 4xx instead of a 500.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_KM_TO_MILES = 0.621371
_TIMEOUT = 30
# Geocoding/routing results are stable for the same inputs, so cache them: a
# repeated trip (or a popular city) skips the upstream call entirely.
_GEOCACHE_TTL = 60 * 60 * 24 * 30  # 30 days


def _cached(key: str, producer):
    """Return ``cache[key]`` or compute it via ``producer()`` and store it.

    Only truthy results are cached, so failures (which raise) and empty
    reverse-geocode results stay retryable rather than being pinned.
    """
    value = cache.get(key)
    if value is not None:
        return value
    value = producer()
    if value:
        cache.set(key, value, _GEOCACHE_TTL)
    return value


class RoutingError(Exception):
    """Raised when geocoding or routing fails.

    The message is **user-facing** — it is returned verbatim as the API's
    ``detail`` on a 4xx, so it must be a clean, actionable sentence. The raw
    provider/HTTP details are logged (not shown to the user).
    """


# --- geocoding ----------------------------------------------------------------


def _ors_country_param() -> dict:
    """ORS/Pelias ``boundary.country`` filter from settings (empty = worldwide)."""
    countries = settings.GEOCODE_COUNTRIES.strip()
    return {"boundary.country": countries} if countries else {}


def _nominatim_country_param() -> dict:
    """Nominatim ``countrycodes`` filter from settings (empty = worldwide)."""
    countries = settings.GEOCODE_COUNTRIES.strip()
    return {"countrycodes": countries.lower()} if countries else {}


def geocode(place: str) -> list[float]:
    """Resolve a place string to ``[lon, lat]`` (cached).

    Prefers ORS (Pelias) when a key is configured; falls back to Nominatim.
    Raises ``RoutingError`` if neither backend can resolve the place.
    """
    key = f"geocode:{settings.GEOCODE_COUNTRIES}:{place.strip().lower()}"
    return _cached(key, lambda: _geocode_uncached(place))


def _geocode_uncached(place: str) -> list[float]:
    errors: list[str] = []

    if settings.ORS_API_KEY:
        try:
            return _geocode_ors(place)
        except RoutingError as exc:
            errors.append(str(exc))

    try:
        return _geocode_nominatim(place)
    except RoutingError as exc:
        errors.append(str(exc))

    logger.warning("Geocoding failed for %r: %s", place, "; ".join(errors))
    raise RoutingError(
        f"We couldn't find a location matching “{place}”. "
        "Check the spelling, or try a more specific place like “City, State”."
    )


def geocode_many(places: list[str]) -> list[list[float]]:
    """Geocode several places **concurrently**, returning coords in input order.

    The three trip locations are independent, so resolving them in parallel cuts
    the geocoding wait from ~3× to ~1× a single lookup. If any place can't be
    resolved, its ``RoutingError`` propagates (naming that place) — same behaviour
    as calling ``geocode`` directly, just faster.
    """
    if not places:
        return []
    with ThreadPoolExecutor(max_workers=len(places)) as pool:
        return list(pool.map(geocode, places))


def _geocode_ors(place: str) -> list[float]:
    try:
        resp = requests.get(
            f"{settings.ORS_BASE_URL}/geocode/search",
            params={
                "api_key": settings.ORS_API_KEY,
                "text": place,
                "size": 1,
                **_ors_country_param(),
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except requests.RequestException as exc:
        raise RoutingError(f"ORS geocoder error: {exc}") from exc
    if not features:
        raise RoutingError(f"ORS found no match for {place!r}")
    return list(features[0]["geometry"]["coordinates"][:2])


def _geocode_nominatim(place: str) -> list[float]:
    try:
        resp = requests.get(
            f"{settings.NOMINATIM_BASE_URL}/search",
            params={
                "q": place,
                "format": "json",
                "limit": 1,
                **_nominatim_country_param(),
            },
            headers={"User-Agent": settings.GEOCODER_USER_AGENT},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as exc:
        raise RoutingError(f"Nominatim error: {exc}") from exc
    if not results:
        raise RoutingError(f"Nominatim found no match for {place!r}")
    return [float(results[0]["lon"]), float(results[0]["lat"])]


# --- place autocomplete -------------------------------------------------------


def suggest_places(query: str, limit: int = 5) -> list[dict]:
    """Return up to ``limit`` place suggestions for typeahead/autocomplete.

    Each suggestion is ``{"label": str, "coords": [lon, lat]}``. Prefers ORS
    (Pelias autocomplete) when a key is configured; falls back to Nominatim. A
    backend that simply finds nothing returns ``[]``; ``RoutingError`` is raised
    only when every backend errors (network/HTTP), so the API can return a 4xx.
    """
    errors: list[str] = []

    if settings.ORS_API_KEY:
        try:
            return _suggest_ors(query, limit)
        except RoutingError as exc:
            errors.append(str(exc))

    try:
        return _suggest_nominatim(query, limit)
    except RoutingError as exc:
        errors.append(str(exc))

    logger.warning("Place search failed for %r: %s", query, "; ".join(errors))
    raise RoutingError("Place search is temporarily unavailable.")


def _suggest_ors(query: str, limit: int) -> list[dict]:
    try:
        resp = requests.get(
            f"{settings.ORS_BASE_URL}/geocode/autocomplete",
            params={
                "api_key": settings.ORS_API_KEY,
                "text": query,
                "size": limit,
                **_ors_country_param(),
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except requests.RequestException as exc:
        raise RoutingError(f"ORS autocomplete error: {exc}") from exc
    return [
        {
            "label": feat["properties"].get("label", ""),
            "coords": list(feat["geometry"]["coordinates"][:2]),
        }
        for feat in features
        if feat.get("geometry", {}).get("coordinates")
    ]


def _suggest_nominatim(query: str, limit: int) -> list[dict]:
    try:
        resp = requests.get(
            f"{settings.NOMINATIM_BASE_URL}/search",
            params={
                "q": query,
                "format": "json",
                "limit": limit,
                "addressdetails": 0,
                **_nominatim_country_param(),
            },
            headers={"User-Agent": settings.GEOCODER_USER_AGENT},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as exc:
        raise RoutingError(f"Nominatim error: {exc}") from exc
    return [
        {"label": r.get("display_name", ""), "coords": [float(r["lon"]), float(r["lat"])]}
        for r in results
    ]


# --- reverse geocoding (coords -> "City, ST") ---------------------------------


def reverse_geocode(coords: list[float] | None) -> str:
    """Resolve a ``[lon, lat]`` point to a short ``"City, ST"`` label.

    Best-effort: prefers ORS (Pelias) when keyed, falls back to Nominatim, and
    returns ``""`` on any failure (so a missing label degrades gracefully to the
    generic stop name rather than erroring the whole plan).
    """
    if not coords:
        return ""
    key = f"reverse:{round(coords[0], 4)}:{round(coords[1], 4)}"
    return _cached(key, lambda: _reverse_uncached(coords[0], coords[1]))


def _reverse_uncached(lon: float, lat: float) -> str:
    if settings.ORS_API_KEY:
        try:
            label = _reverse_ors(lon, lat)
            if label:
                return label
        except requests.RequestException as exc:
            logger.warning("ORS reverse geocode failed at %s,%s: %s", lon, lat, exc)
    try:
        return _reverse_nominatim(lon, lat)
    except requests.RequestException as exc:
        logger.warning("Nominatim reverse geocode failed at %s,%s: %s", lon, lat, exc)
        return ""


def reverse_geocode_many(coords_list: list[list[float] | None]) -> list[str]:
    """Reverse-geocode several points concurrently, preserving input order.

    Deduplicates near-identical points (rounded to ~100 m) so repeated stops cost
    one lookup, and runs the rest in parallel to keep latency down on long trips.
    """
    if not coords_list:
        return []

    def key(c):
        return (round(c[0], 3), round(c[1], 3)) if c else None

    unique = {key(c): c for c in coords_list if c}
    # Resolve all unique points in one wave (a long multi-day trip has ~15 stops);
    # a throttled/failed lookup just yields "" and keeps the generic stop label.
    workers = max(1, min(len(unique), 16))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        resolved = dict(zip(unique.keys(), pool.map(reverse_geocode, unique.values())))
    return [resolved.get(key(c), "") for c in coords_list]


def _reverse_ors(lon: float, lat: float) -> str:
    resp = requests.get(
        f"{settings.ORS_BASE_URL}/geocode/reverse",
        params={
            "api_key": settings.ORS_API_KEY,
            "point.lon": lon,
            "point.lat": lat,
            "size": 1,
            "layers": "locality,localadmin,county,region",
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        return ""
    p = features[0].get("properties", {})
    city = p.get("locality") or p.get("localadmin") or p.get("county") or p.get("name")
    state = p.get("region_a") or p.get("region")
    return ", ".join(part for part in (city, state) if part) or p.get("label", "")


def _reverse_nominatim(lon: float, lat: float) -> str:
    resp = requests.get(
        f"{settings.NOMINATIM_BASE_URL}/reverse",
        params={"lon": lon, "lat": lat, "format": "json", "zoom": 10, "addressdetails": 1},
        headers={"User-Agent": settings.GEOCODER_USER_AGENT},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    addr = resp.json().get("address", {})
    city = (
        addr.get("city") or addr.get("town") or addr.get("village")
        or addr.get("hamlet") or addr.get("county")
    )
    state = addr.get("state")
    return ", ".join(part for part in (city, state) if part)


# --- routing ------------------------------------------------------------------


def route(coords: list[list[float]]) -> dict:
    """Route through an ordered list of ``[lon, lat]`` waypoints (cached).

    Returns ``{distance_miles, duration_hours, geometry, legs}`` where geometry is
    a list of ``[lon, lat]`` points and ``legs`` holds the per-waypoint-pair
    ``{distance_miles, duration_hours}`` (e.g. current->pickup, pickup->dropoff).
    Tries OpenRouteService, falls back to OSRM. Raises ``RoutingError`` if both
    backends fail.
    """
    key = "route:" + ";".join(f"{round(c[0], 5)},{round(c[1], 5)}" for c in coords)
    return _cached(key, lambda: _route_uncached(coords))


def _route_uncached(coords: list[list[float]]) -> dict:
    errors: list[str] = []
    if settings.ORS_API_KEY:
        try:
            return _route_ors(coords)
        except Exception as exc:  # noqa: BLE001 - fall back to OSRM on any failure
            errors.append(f"ORS: {exc}")
    try:
        return _route_osrm(coords)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"OSRM: {exc}")

    logger.warning("Routing failed for %s: %s", coords, "; ".join(errors))
    raise RoutingError(
        "We couldn't find a drivable route between those locations. "
        "Make sure each one is reachable by road — they can't be separated by "
        "water or on different continents."
    )


def _route_ors(coords: list[list[float]]) -> dict:
    url = f"{settings.ORS_BASE_URL}/v2/directions/driving-hgv/geojson"
    resp = requests.post(
        url,
        json={"coordinates": coords},
        headers={
            "Authorization": settings.ORS_API_KEY,
            "Content-Type": "application/json",
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    feature = resp.json()["features"][0]
    summary = feature["properties"]["summary"]
    legs = [
        {
            "distance_miles": seg["distance"] / 1000 * _KM_TO_MILES,
            "duration_hours": seg["duration"] / 3600,
        }
        for seg in feature["properties"].get("segments", [])
    ]
    return {
        "distance_miles": summary["distance"] / 1000 * _KM_TO_MILES,
        "duration_hours": summary["duration"] / 3600,
        "geometry": feature["geometry"]["coordinates"],
        "legs": legs,
    }


def _route_osrm(coords: list[list[float]]) -> dict:
    path = ";".join(f"{lon},{lat}" for lon, lat in coords)
    url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{path}"
    resp = requests.get(
        url,
        params={"overview": "full", "geometries": "geojson"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RoutingError(f"OSRM routing failed: {data.get('code')}")
    r = data["routes"][0]
    legs = [
        {
            "distance_miles": leg["distance"] / 1000 * _KM_TO_MILES,
            "duration_hours": leg["duration"] / 3600,
        }
        for leg in r.get("legs", [])
    ]
    return {
        "distance_miles": r["distance"] / 1000 * _KM_TO_MILES,
        "duration_hours": r["duration"] / 3600,
        "geometry": r["geometry"]["coordinates"],
        "legs": legs,
    }
