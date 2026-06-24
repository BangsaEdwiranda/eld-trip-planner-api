"""Hours-of-Service simulator.

Walks the planned route leg by leg and emits an ordered timeline of duty-status
segments that comply with the FMCSA 70hr/8day property-carrier rules
(see documentation/hos-rules.md).

Modeled limits:
- 11-hour driving limit per window
- 14-hour driving window (wall clock; breaks/on-duty do not pause it)
- 30-minute break required after 8 cumulative driving hours. Any >=30-min
  non-driving period (off duty, sleeper, OR on-duty-not-driving such as a fuel
  stop / pickup) satisfies it.
- 70-hour / 8-day cycle (seeded by the trip's current_cycle_used_hours)
- 10-hour off-duty reset of the daily clocks; 34-hour restart of the weekly cycle

The route is modeled as ordered legs. Each leg is a drive of some distance,
optionally followed by an on-duty stop on arrival (pickup, dropoff). Counters
persist across legs, so e.g. fueling-every-1,000-miles is cumulative over the
whole trip and the driver physically drives current -> pickup BEFORE loading.

NOT modeled (out of scope per brief): sleeper-berth splits, adverse conditions,
short-haul exceptions, the rolling 8-day drop-off (the 34-hour restart stands in
for cycle exhaustion instead).

The simulator is pure: given legs + a start time it returns a list of dicts.
No DB or network access — easy to unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings

HOS = settings.HOS_SETTINGS
_EPS = 1e-6


@dataclass
class Segment:
    status: str          # "OFF" | "SB" | "D" | "ON"
    start: datetime
    end: datetime
    location: str = ""
    note: str = ""
    miles: float = 0.0   # cumulative route miles driven when this segment starts

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "location": self.location,
            "note": self.note,
            "miles": round(self.miles, 2),
        }


@dataclass
class _State:
    """Mutable HOS counters carried while stepping along the route."""

    clock: datetime
    cycle_hours: float
    driving_today: float = 0.0            # hours driven in current window (<= 11)
    window_start: datetime | None = None  # start of current 14h window
    driving_since_break: float = 0.0      # hours driven since last >=30-min break
    miles_since_fuel: float = 0.0         # miles driven since last fuel stop
    total_miles: float = 0.0              # cumulative route miles driven so far
    segments: list[Segment] = field(default_factory=list)


def plan_timeline(
    *,
    legs: list[dict],
    start_time: datetime,
    current_cycle_used_hours: float,
) -> list[dict]:
    """Produce a compliant duty-status timeline for the whole trip.

    ``legs`` is an ordered list of dicts::

        {
            "distance_miles": float,
            "duration_hours": float,           # used to derive average speed
            "arrival": {                       # optional on-duty stop on arrival
                "hours": float,
                "location": str,
                "note": str,
            },
        }

    Returns a list of segment dicts (see ``Segment.to_dict``).
    """
    state = _State(clock=start_time, cycle_hours=current_cycle_used_hours)

    for leg in legs:
        avg_speed = _avg_speed(leg["distance_miles"], leg.get("duration_hours"))
        _drive(state, leg["distance_miles"], avg_speed, leg.get("location", "En route"))

        arrival = leg.get("arrival")
        if arrival and arrival.get("hours", 0) > 0:
            _add(state, "ON", arrival["hours"], arrival.get("location", ""), arrival.get("note", ""))
            _credit_break(state, arrival["hours"])

    return [s.to_dict() for s in state.segments]


# --- driving ------------------------------------------------------------------


def _drive(state: _State, distance_miles: float, avg_speed: float, location: str) -> None:
    """Consume ``distance_miles`` of driving, inserting breaks/rests/fuel."""
    remaining = distance_miles
    while remaining > _EPS:
        # Resolve any limit that has been reached before driving further.
        _maybe_reset_cycle(state)
        _maybe_fuel(state)
        _maybe_take_break(state)
        _maybe_take_rest(state)

        hours_to_break = HOS["DRIVING_BEFORE_BREAK"] - state.driving_since_break
        hours_to_drive_limit = HOS["MAX_DRIVING_HOURS"] - state.driving_today
        hours_to_window_end = _hours_left_in_window(state)
        hours_to_cycle_limit = HOS["CYCLE_HOURS"] - state.cycle_hours
        miles_to_fuel = HOS["FUEL_INTERVAL_MILES"] - state.miles_since_fuel

        # §395.3(b): may not DRIVE past 70 on-duty hours. Cap the increment at the
        # cycle line so a driving step never crosses it; the 34h restart is then
        # inserted at the top of the next step (or by _maybe_reset_cycle when an
        # on-duty stop, not driving, is what tips the cycle to 70).
        drivable_hours = max(
            0.0,
            min(
                hours_to_break,
                hours_to_drive_limit,
                hours_to_window_end,
                hours_to_cycle_limit,
            ),
        )
        if drivable_hours <= _EPS:
            # Daily clocks exhausted but the _maybe_* helpers didn't catch it
            # (shouldn't normally happen) — force the 10-hour reset.
            _take_rest(state)
            continue

        drivable_miles = drivable_hours * avg_speed
        leg_miles = min(remaining, drivable_miles, miles_to_fuel)
        leg_hours = leg_miles / avg_speed if avg_speed else 0.0

        _add(state, "D", leg_hours, location, "Driving")
        state.driving_today += leg_hours
        state.driving_since_break += leg_hours
        state.miles_since_fuel += leg_miles
        state.total_miles += leg_miles
        remaining -= leg_miles


# --- segment + counter helpers ------------------------------------------------


def _add(state: _State, status: str, hours: float, location: str, note: str) -> None:
    """Append a segment of the given status/duration and advance the clock."""
    if hours <= 0:
        return
    if status in ("D", "ON") and state.window_start is None:
        state.window_start = state.clock  # window opens on first work of the shift

    end = state.clock + timedelta(hours=hours)
    state.segments.append(
        Segment(
            status=status, start=state.clock, end=end,
            location=location, note=note, miles=state.total_miles,
        )
    )
    state.clock = end

    # On-duty time (driving + on-duty-not-driving) accrues to the weekly cycle.
    if status in ("D", "ON"):
        state.cycle_hours += hours


def _credit_break(state: _State, hours: float) -> None:
    """A >=30-min non-driving period satisfies the 8-hour driving-break rule."""
    if hours >= HOS["BREAK_DURATION_HOURS"] - _EPS:
        state.driving_since_break = 0.0


def _hours_left_in_window(state: _State) -> float:
    if state.window_start is None:
        return HOS["MAX_WINDOW_HOURS"]
    elapsed = (state.clock - state.window_start).total_seconds() / 3600
    return HOS["MAX_WINDOW_HOURS"] - elapsed


def _maybe_fuel(state: _State) -> None:
    """Insert a fuel stop once 1,000 miles have elapsed since the last one."""
    if state.miles_since_fuel >= HOS["FUEL_INTERVAL_MILES"] - _EPS:
        _add(state, "ON", HOS["FUEL_STOP_HOURS"], "Fuel stop", "Fueling")
        state.miles_since_fuel = 0.0
        _credit_break(state, HOS["FUEL_STOP_HOURS"])


def _maybe_take_break(state: _State) -> None:
    """Insert a 30-min off-duty break once 8 cumulative driving hours are reached."""
    if state.driving_since_break >= HOS["DRIVING_BEFORE_BREAK"] - _EPS:
        _add(state, "OFF", HOS["BREAK_DURATION_HOURS"], "Rest area", "30-min break")
        state.driving_since_break = 0.0


def _maybe_take_rest(state: _State) -> None:
    """Insert a 10-hour reset if the 11h driving or 14h window limit is hit."""
    window_done = _hours_left_in_window(state) <= _EPS
    driving_done = state.driving_today >= HOS["MAX_DRIVING_HOURS"] - _EPS
    if window_done or driving_done:
        _take_rest(state)


def _take_rest(state: _State) -> None:
    _add(state, "OFF", HOS["REQUIRED_REST_HOURS"], "Rest stop", "10-hour reset")
    state.driving_today = 0.0
    state.driving_since_break = 0.0
    state.window_start = None


def _maybe_reset_cycle(state: _State) -> None:
    """Insert a 34-hour restart once the 70-hour cycle is exhausted."""
    if state.cycle_hours >= HOS["CYCLE_HOURS"] - _EPS:
        _add(state, "OFF", HOS["RESTART_HOURS"], "Rest stop", "34-hour restart")
        state.cycle_hours = 0.0
        state.driving_today = 0.0
        state.driving_since_break = 0.0
        state.window_start = None


def _avg_speed(distance_miles: float, duration_hours: float | None) -> float:
    if duration_hours and duration_hours > 0:
        return distance_miles / duration_hours
    return HOS["AVG_SPEED_MPH"]
