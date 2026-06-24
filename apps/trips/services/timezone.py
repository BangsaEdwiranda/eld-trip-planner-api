"""Resolve a trip's local time zone and start instant from its coordinates.

Real ELD records of duty status are kept in a single fixed time zone — the
driver's home terminal — even when the truck crosses zones (FMCSA §395.8). We
have no home-terminal input, so we approximate it with the time zone of the
trip's **current location**, resolved from coordinates and held constant for the
whole trip.

The duty day starts when the driver goes on duty; FMCSA defines no fixed shift
start, so we use **"now" in that zone**. (eld.py then pads each daily sheet with
off-duty time so it still totals a full 24h, like a real RODS page.)

Pure + offline: the only dependency is ``timezonefinder`` (bundled data, no
network), so this stays unit-testable.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings

# TimezoneFinder loads a data file on init; build it once and reuse.
_finder = None


def _tz_finder():
    global _finder
    if _finder is None:
        from timezonefinder import TimezoneFinder

        _finder = TimezoneFinder()
    return _finder


def timezone_for(coords: list[float]) -> str:
    """Return the IANA time-zone name for ``[lon, lat]``.

    Falls back to ``settings.FALLBACK_TIMEZONE`` when the point can't be resolved
    (e.g. mid-ocean) or the lookup errors.
    """
    try:
        name = _tz_finder().timezone_at(lng=coords[0], lat=coords[1])
    except Exception:  # noqa: BLE001 - any lookup failure -> fallback zone
        name = None
    return name or settings.FALLBACK_TIMEZONE


def start_time_for(coords: list[float], *, now: datetime | None = None) -> datetime:
    """Return the trip's start instant: "now" in the location's zone.

    Truncated to the whole minute. ``now`` is injectable for tests; in
    production it defaults to the real current time.
    """
    tz = ZoneInfo(timezone_for(coords))
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    return current.replace(second=0, microsecond=0)
