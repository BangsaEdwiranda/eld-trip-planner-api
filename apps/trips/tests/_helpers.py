"""Shared helpers for the trips test suite.

The constants mirror the HOS limits locally (so the checks stay *independent* of
config, not a mirror of it), plus leg builders and ``HOSComplianceMixin`` — an
independent re-implementation of the FMCSA limits that walks a generated timeline
and fails if ANY rule is broken. The HOS and ELD suites run their output through
it, so the simulator is validated against the rules, not against itself.
"""

from datetime import datetime

EPS = 1e-3
SPEED = 55  # mph, matches HOS_SETTINGS["AVG_SPEED_MPH"]

# Limits (kept local so the test is an independent check, not a mirror of config).
MAX_DRIVING = 11
MAX_WINDOW = 14
DRIVE_BEFORE_BREAK = 8
BREAK_MIN = 0.5
REST_MIN = 10
RESTART_MIN = 34
CYCLE_MAX = 70


def _legs(distances, speed=SPEED, pickup=1.0, dropoff=1.0):
    """Build HOS legs from a list of leg distances (miles).

    The last leg gets a dropoff stop; a 2-leg trip also gets a pickup stop after
    leg 1 (mirrors current->pickup->dropoff).
    """
    legs = []
    for i, dist in enumerate(distances):
        leg = {"distance_miles": dist, "duration_hours": dist / speed}
        if len(distances) == 2 and i == 0:
            leg["arrival"] = {"hours": pickup, "location": "Pickup", "note": "Pickup (loading)"}
        if i == len(distances) - 1:
            leg["arrival"] = {"hours": dropoff, "location": "Dropoff", "note": "Dropoff (unloading)"}
        legs.append(leg)
    return legs


def _dt(seg, key):
    return datetime.fromisoformat(seg[key])


def _dur(seg):
    return (_dt(seg, "end") - _dt(seg, "start")).total_seconds() / 3600


class HOSComplianceMixin:
    """Independent validator of the FMCSA limits over a generated timeline."""

    def assert_hos_compliant(self, timeline, cycle_used=0.0):
        self.assertTrue(timeline, "timeline should not be empty")

        driving_today = 0.0
        driving_since_break = 0.0
        window_start = None
        prev_end = None
        cycle_hours = cycle_used  # seeded on-duty hours in the 70hr/8day cycle

        for seg in timeline:
            start, end = _dt(seg, "start"), _dt(seg, "end")
            dur = _dur(seg)

            self.assertGreater(dur, 0, "segments must have positive duration")
            self.assertLessEqual(start, end, "segment start must precede end")
            if prev_end is not None:
                self.assertEqual(
                    start, prev_end, "timeline must be contiguous (no gaps/overlaps)"
                )
            prev_end = end

            if seg["status"] == "D":
                if window_start is None:
                    window_start = start
                driving_today += dur
                driving_since_break += dur
                cycle_hours += dur
                window_elapsed = (end - window_start).total_seconds() / 3600

                self.assertLessEqual(
                    driving_today, MAX_DRIVING + EPS,
                    f"11h driving limit broken: {driving_today:.2f}h",
                )
                self.assertLessEqual(
                    window_elapsed, MAX_WINDOW + EPS,
                    f"14h window broken: drove at {window_elapsed:.2f}h into window",
                )
                self.assertLessEqual(
                    driving_since_break, DRIVE_BEFORE_BREAK + EPS,
                    f"drove {driving_since_break:.2f}h without a 30-min break",
                )
                # §395.3(b): may not DRIVE after 70 on-duty hours in the cycle.
                self.assertLessEqual(
                    cycle_hours, CYCLE_MAX + EPS,
                    f"drove past the 70h cycle: {cycle_hours:.2f}h on duty",
                )
            elif seg["status"] == "ON":
                if window_start is None:
                    window_start = start
                cycle_hours += dur  # on-duty-not-driving also accrues to the cycle
                if dur >= BREAK_MIN - EPS:  # >=30-min non-driving satisfies the break
                    driving_since_break = 0.0
            else:  # OFF / SB
                if dur >= RESTART_MIN - EPS:  # 34h+ restarts the weekly cycle
                    cycle_hours = 0.0
                if dur >= REST_MIN - EPS:   # 10h+ resets the daily clocks
                    driving_today = 0.0
                    driving_since_break = 0.0
                    window_start = None
                elif dur >= BREAK_MIN - EPS:
                    driving_since_break = 0.0
