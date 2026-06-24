"""Split an HOS timeline into per-day ELD log sheets.

Takes the segment list from ``hos.plan_timeline`` (absolute datetimes) and groups
it into one sheet per calendar day, splitting any segment that crosses midnight.
Each segment is expressed in minutes-from-local-midnight so the frontend can draw
it directly on the 24-hour grid (see docs/eld-log-format.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta

_MINUTES_PER_DAY = 24 * 60


def build_log_sheets(segments: list[dict]) -> list[dict]:
    """Return a list of per-day sheets, each with grid-ready segments and totals."""
    if not segments:
        return []

    # Index segments by date, splitting across midnight as needed.
    by_date: dict = {}
    for seg in segments:
        start = datetime.fromisoformat(seg["start"])
        end = datetime.fromisoformat(seg["end"])
        for piece_start, piece_end in _split_at_midnight(start, end):
            day = piece_start.date()
            by_date.setdefault(day, []).append(
                {
                    "status": seg["status"],
                    "start_minute": _minutes(piece_start),
                    "end_minute": _minutes(piece_end) or _MINUTES_PER_DAY,
                    "location": seg.get("location", ""),
                    "note": seg.get("note", ""),
                }
            )

    sheets = []
    for day_index, day in enumerate(sorted(by_date)):
        day_segments = _pad_to_full_day(
            sorted(by_date[day], key=lambda s: s["start_minute"])
        )
        sheets.append(
            {
                "date": day.isoformat(),
                "day_index": day_index,
                "segments": day_segments,
                "totals": _totals(day_segments),
                "total_miles_driving": 0,  # filled by caller if per-day miles known
            }
        )
    return sheets


def _pad_to_full_day(day_segments: list[dict]) -> list[dict]:
    """Fill the day's edges with off-duty so the sheet spans the full 24h.

    A real RODS page always totals 24h: time before the driver goes on duty (and
    after the trip's final duty status) is logged as OFF. The interior of the trip
    is already contiguous, so this only adds a leading OFF on day 0 (midnight ->
    first duty) and a trailing OFF on the last day (last duty -> midnight).
    """
    if not day_segments:
        return [_off(0, _MINUTES_PER_DAY)]

    padded = []
    if day_segments[0]["start_minute"] > 0:
        padded.append(_off(0, day_segments[0]["start_minute"]))
    padded.extend(day_segments)
    if day_segments[-1]["end_minute"] < _MINUTES_PER_DAY:
        padded.append(_off(day_segments[-1]["end_minute"], _MINUTES_PER_DAY))
    return padded


def _off(start_minute: int, end_minute: int) -> dict:
    return {
        "status": "OFF",
        "start_minute": start_minute,
        "end_minute": end_minute,
        "location": "",
        "note": "Off duty",
    }


def _split_at_midnight(start: datetime, end: datetime):
    """Yield (start, end) pieces that never cross a midnight boundary."""
    cursor = start
    while cursor.date() < end.date():
        next_midnight = datetime.combine(
            cursor.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=cursor.tzinfo,
        )
        yield cursor, next_midnight
        cursor = next_midnight
    if cursor < end:
        yield cursor, end


def _minutes(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _totals(day_segments: list[dict]) -> dict:
    """Per-status hours for the day, rounded so they sum to the exact day length.

    Sum the exact integer minutes per status first, then round each to 2 decimals
    with the largest-remainder method, so the four totals always add up to the
    day's true length (24.00h for a full sheet) — no rounding drift, and no error
    from rounding segment-by-segment.
    """
    statuses = ("OFF", "SB", "D", "ON")
    minutes = {s: 0 for s in statuses}
    for seg in day_segments:
        minutes[seg["status"]] += seg["end_minute"] - seg["start_minute"]

    # Work in whole 0.01-hour units so the totals stay exact.
    target_units = round(sum(minutes.values()) / 60 * 100)
    exact = {s: minutes[s] / 60 * 100 for s in statuses}
    units = {s: int(exact[s]) for s in statuses}  # floor each to 0.01h
    leftover = target_units - sum(units.values())
    # Hand the leftover 0.01h units to the largest fractional parts.
    for s in sorted(statuses, key=lambda s: exact[s] - units[s], reverse=True)[:leftover]:
        units[s] += 1
    return {s: round(units[s] / 100, 2) for s in statuses}
