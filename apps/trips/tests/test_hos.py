"""HOS simulator scenarios — validated against the independent compliance check."""

from datetime import datetime, timedelta, timezone

from django.test import TestCase

from ..services import hos
from ._helpers import HOSComplianceMixin, SPEED, _dt, _dur, _legs


class HOSScenarioTests(HOSComplianceMixin, TestCase):
    start = datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc)

    def plan(self, distances, cycle=0.0):
        return hos.plan_timeline(
            legs=_legs(distances),
            start_time=self.start,
            current_cycle_used_hours=cycle,
        )

    def test_pickup_happens_after_driving_to_pickup(self):
        # current->pickup is 55mi (1h). The driver must DRIVE first, then load.
        timeline = self.plan([55, 110])
        self.assertEqual(timeline[0]["status"], "D", "first action is driving to pickup")
        pickup = next(s for s in timeline if s["note"] == "Pickup (loading)")
        # Pickup starts exactly when the first driving leg ends (1h in).
        self.assertEqual(_dt(pickup, "start"), self.start + timedelta(hours=1))
        self.assert_hos_compliant(timeline)

    def test_short_trip_has_pickup_dropoff_and_no_rest(self):
        timeline = self.plan([55, 110])  # 3h total driving
        notes = [s["note"] for s in timeline]
        self.assertIn("Pickup (loading)", notes)
        self.assertIn("Dropoff (unloading)", notes)
        self.assertNotIn("10-hour reset", notes)
        self.assertAlmostEqual(sum(_dur(s) for s in timeline if s["status"] == "D"), 3, places=1)
        self.assert_hos_compliant(timeline)

    def test_total_driving_time_is_conserved(self):
        timeline = self.plan([300, 900])  # 1200 mi
        driving = sum(_dur(s) for s in timeline if s["status"] == "D")
        self.assertAlmostEqual(driving, 1200 / SPEED, places=1)
        self.assert_hos_compliant(timeline)

    def test_30_min_break_before_8h_continuous_driving(self):
        # 600 mi single drive ~ 10.9h driving -> must break by the 8h mark.
        timeline = self.plan([600])
        self.assertTrue(any(s["note"] == "30-min break" for s in timeline))
        self.assert_hos_compliant(timeline)

    def test_long_trip_inserts_rest_and_spans_multiple_days(self):
        timeline = self.plan([200, 1300])  # 1500 mi -> >11h driving, needs rests
        self.assertTrue(any(s["note"] == "10-hour reset" for s in timeline))
        self.assert_hos_compliant(timeline)

    def test_fuel_stop_at_least_every_1000_miles(self):
        timeline = self.plan([200, 2300])  # 2500 mi -> expect >= 2 fuel stops
        fuels = [s for s in timeline if s["note"] == "Fueling"]
        self.assertGreaterEqual(len(fuels), 2)
        self.assert_hos_compliant(timeline)

    def test_cycle_near_limit_triggers_34h_restart(self):
        # 69h already used -> only 1h of cycle left -> 34h restart kicks in early.
        timeline = self.plan([100, 1500], cycle=69)
        self.assertTrue(any(s["note"] == "34-hour restart" for s in timeline))
        self.assert_hos_compliant(timeline, cycle_used=69)

    def test_driver_does_not_drive_past_70h_cycle(self):
        # Seeded at 60h, only 10h of cycle remain. The driver must stop driving
        # at the 70h line and take a 34h restart -- never drive into hour 71.
        # Regression: a driving step used to ignore the cycle and overshoot 70h.
        timeline = self.plan([700], cycle=60)  # ~12.7h of driving wanted
        self.assertTrue(any(s["note"] == "34-hour restart" for s in timeline))
        self.assert_hos_compliant(timeline, cycle_used=60)

    def test_single_leg_route_still_compliant(self):
        # Fallback shape: one leg with only a dropoff.
        timeline = hos.plan_timeline(
            legs=_legs([1400]),
            start_time=self.start,
            current_cycle_used_hours=0,
        )
        self.assert_hos_compliant(timeline)

    def test_avg_speed_falls_back_to_55_without_duration(self):
        # Used to convert miles<->hours; falls back to 55 mph when the router gave
        # distance but no usable duration.
        self.assertEqual(hos._avg_speed(110, None), 55)
        self.assertEqual(hos._avg_speed(110, 0), 55)
        self.assertAlmostEqual(hos._avg_speed(110, 2.0), 55.0)  # real speed when given
