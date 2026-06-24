"""Pure geometry helpers for the route polyline.

Used to locate a stop (break, rest, fuel) along the route by how far the driver
has travelled — the HOS simulator knows distance, not coordinates, so we project
a stop's cumulative mileage onto the route geometry here. No DB or network; just
math, so it's easy to unit-test.
"""

from __future__ import annotations

import math

_EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(a: list[float], b: list[float]) -> float:
    """Great-circle distance in miles between two ``[lon, lat]`` points."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def point_at_fraction(geometry: list[list[float]], fraction: float) -> list[float] | None:
    """Return the ``[lon, lat]`` point ``fraction`` (0..1) of the way along the line.

    ``geometry`` is the route polyline (list of ``[lon, lat]``). Walks the line by
    cumulative great-circle distance and linearly interpolates within the segment
    that contains the target distance. Returns ``None`` for empty geometry.
    """
    if not geometry:
        return None
    if len(geometry) == 1:
        return list(geometry[0])

    fraction = min(max(fraction, 0.0), 1.0)

    # Cumulative distance to each vertex.
    cum = [0.0]
    for prev, nxt in zip(geometry, geometry[1:]):
        cum.append(cum[-1] + haversine_miles(prev, nxt))
    total = cum[-1]
    if total == 0:
        return list(geometry[0])

    target = fraction * total
    for i in range(1, len(cum)):
        if cum[i] >= target:
            span = cum[i] - cum[i - 1]
            t = 0.0 if span == 0 else (target - cum[i - 1]) / span
            lon = geometry[i - 1][0] + t * (geometry[i][0] - geometry[i - 1][0])
            lat = geometry[i - 1][1] + t * (geometry[i][1] - geometry[i - 1][1])
            return [lon, lat]
    return list(geometry[-1])
