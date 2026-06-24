"""HOS simulator + ELD sheet tests.

The centrepiece is ``assert_hos_compliant`` — an independent re-implementation of
the FMCSA limits that walks a generated timeline and fails if ANY rule is broken.
Every scenario runs through it, so the simulator is validated against the rules
rather than against its own output. No network required.
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from . import views
from .services import eld, geo, hos, routing
from .services import timezone as trip_tz

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


class ELDSheetTests(HOSComplianceMixin, TestCase):
    start = datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc)

    def test_multi_day_trip_produces_multiple_sheets_summing_to_24(self):
        timeline = hos.plan_timeline(
            legs=_legs([400, 1600]),  # 2000 mi
            start_time=self.start,
            current_cycle_used_hours=0,
        )
        self.assert_hos_compliant(timeline)
        sheets = eld.build_log_sheets(timeline)
        self.assertGreater(len(sheets), 1)
        # Every sheet — including the padded first/last day — totals EXACTLY 24h
        # (totals are rounded so they sum without drift; no 24.01 artifacts).
        for sheet in sheets:
            self.assertEqual(
                round(sum(sheet["totals"].values()), 2), 24.0,
                msg="every sheet's status totals must sum to exactly 24.00h",
            )

    def test_status_totals_round_without_drift(self):
        # A day split into thirds that don't divide evenly into 0.01h must still
        # sum to exactly 24.00 (largest-remainder rounding, not naive per-status).
        timeline = [
            {"status": "OFF", "start": "2026-06-23T00:00:00+00:00", "end": "2026-06-23T08:00:00+00:00", "location": "", "note": ""},
            {"status": "D",   "start": "2026-06-23T08:00:00+00:00", "end": "2026-06-23T16:20:00+00:00", "location": "", "note": ""},
            {"status": "ON",  "start": "2026-06-23T16:20:00+00:00", "end": "2026-06-24T00:00:00+00:00", "location": "", "note": ""},
        ]
        totals = eld.build_log_sheets(timeline)[0]["totals"]
        self.assertEqual(round(sum(totals.values()), 2), 24.0)

    def test_first_and_last_day_padded_with_off_duty(self):
        # Start at 08:00 -> day 0 must open with OFF from midnight (0) to 08:00 (480);
        # the last day must close with OFF running to midnight (1440).
        timeline = hos.plan_timeline(
            legs=_legs([400, 1600]),
            start_time=self.start,  # 08:00
            current_cycle_used_hours=0,
        )
        sheets = eld.build_log_sheets(timeline)
        first = sheets[0]["segments"][0]
        self.assertEqual((first["status"], first["start_minute"], first["end_minute"]), ("OFF", 0, 480))
        last = sheets[-1]["segments"][-1]
        self.assertEqual((last["status"], last["end_minute"]), ("OFF", 1440))
        # Contiguity preserved: every day covers 0..1440 with no gaps.
        for sheet in sheets:
            segs = sheet["segments"]
            self.assertEqual(segs[0]["start_minute"], 0)
            self.assertEqual(segs[-1]["end_minute"], 1440)
            for a, b in zip(segs, segs[1:]):
                self.assertEqual(a["end_minute"], b["start_minute"])

    def test_segments_never_cross_midnight(self):
        timeline = hos.plan_timeline(
            legs=_legs([400, 1600]),
            start_time=self.start,
            current_cycle_used_hours=0,
        )
        for sheet in eld.build_log_sheets(timeline):
            for seg in sheet["segments"]:
                self.assertGreaterEqual(seg["start_minute"], 0)
                self.assertLessEqual(seg["end_minute"], 24 * 60)
                self.assertLess(seg["start_minute"], seg["end_minute"])


class PlaceSuggestViewTests(TestCase):
    """The /api/geocode/ autocomplete endpoint. Network (suggest_places) mocked."""

    url = reverse("geocode")

    def test_short_query_returns_empty_without_calling_geocoder(self):
        with mock.patch.object(routing, "suggest_places") as suggest:
            resp = self.client.get(self.url, {"q": "S"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"results": []})
        suggest.assert_not_called()

    def test_returns_suggestions(self):
        fake = [{"label": "San Francisco, CA, USA", "coords": [-122.42, 37.77]}]
        with mock.patch.object(routing, "suggest_places", return_value=fake) as suggest:
            resp = self.client.get(self.url, {"q": "San Fran", "limit": "5"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"results": fake})
        suggest.assert_called_once_with("San Fran", limit=5)

    def test_limit_is_capped(self):
        with mock.patch.object(routing, "suggest_places", return_value=[]) as suggest:
            self.client.get(self.url, {"q": "Reno", "limit": "999"})
        self.assertEqual(suggest.call_args.kwargs["limit"], 10)

    def test_geocoder_failure_returns_400(self):
        with mock.patch.object(
            routing, "suggest_places", side_effect=routing.RoutingError("down")
        ):
            resp = self.client.get(self.url, {"q": "Reno"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("detail", resp.json())


class GeocodeCountryFilterTests(TestCase):
    """The configured country filter is sent to both geocoders (requests mocked)."""

    def _resp(self, payload):
        m = mock.Mock()
        m.raise_for_status.return_value = None
        m.json.return_value = payload
        return m

    @mock.patch("apps.trips.services.routing.settings")
    @mock.patch("apps.trips.services.routing.requests.get")
    def test_ors_autocomplete_includes_boundary_country(self, get, fake_settings):
        fake_settings.ORS_BASE_URL = "https://ors.test"
        fake_settings.ORS_API_KEY = "k"
        fake_settings.GEOCODE_COUNTRIES = "US"
        get.return_value = self._resp({"features": []})

        routing._suggest_ors("Reno", 5)

        self.assertEqual(get.call_args.kwargs["params"]["boundary.country"], "US")

    @mock.patch("apps.trips.services.routing.settings")
    @mock.patch("apps.trips.services.routing.requests.get")
    def test_nominatim_search_includes_countrycodes_lowercased(self, get, fake_settings):
        fake_settings.NOMINATIM_BASE_URL = "https://nom.test"
        fake_settings.GEOCODER_USER_AGENT = "test"
        fake_settings.GEOCODE_COUNTRIES = "US,CA"
        get.return_value = self._resp([])

        routing._suggest_nominatim("Reno", 5)

        self.assertEqual(get.call_args.kwargs["params"]["countrycodes"], "us,ca")

    @mock.patch("apps.trips.services.routing.settings")
    @mock.patch("apps.trips.services.routing.requests.get")
    def test_blank_setting_sends_no_filter(self, get, fake_settings):
        fake_settings.NOMINATIM_BASE_URL = "https://nom.test"
        fake_settings.GEOCODER_USER_AGENT = "test"
        fake_settings.GEOCODE_COUNTRIES = ""
        get.return_value = self._resp([])

        routing._suggest_nominatim("Reno", 5)

        self.assertNotIn("countrycodes", get.call_args.kwargs["params"])


class TimezoneServiceTests(TestCase):
    """Coords -> local zone, and start = 'now' in that zone (offline, no network)."""

    def test_resolves_known_us_coordinates_to_their_zone(self):
        # [lon, lat]
        self.assertEqual(trip_tz.timezone_for([-104.99, 39.74]), "America/Denver")
        self.assertEqual(trip_tz.timezone_for([-74.0, 40.71]), "America/New_York")

    def test_unresolvable_point_uses_fallback(self):
        # When the finder yields no zone, fall back to the configured default.
        with mock.patch.object(trip_tz, "_tz_finder") as finder:
            finder.return_value.timezone_at.return_value = None
            self.assertEqual(trip_tz.timezone_for([-150.0, 0.0]), settings.FALLBACK_TIMEZONE)

    def test_start_time_is_now_in_location_zone_truncated_to_minute(self):
        # 14:22:43 UTC, seen from Denver (UTC-6 in June) -> 08:22, seconds dropped.
        utc_now = datetime(2026, 6, 23, 14, 22, 43, 500000, tzinfo=timezone.utc)
        start = trip_tz.start_time_for([-104.99, 39.74], now=utc_now)
        self.assertEqual(str(start.tzinfo), "America/Denver")
        self.assertEqual((start.hour, start.minute, start.second), (8, 22, 0))


class GeoInterpolationTests(TestCase):
    """Projecting a distance fraction onto the route polyline (pure math)."""

    def test_endpoints_and_midpoint_of_a_straight_line(self):
        geom = [[0.0, 0.0], [10.0, 0.0]]  # 10° of longitude along the equator
        self.assertEqual(geo.point_at_fraction(geom, 0.0), [0.0, 0.0])
        self.assertEqual(geo.point_at_fraction(geom, 1.0), [10.0, 0.0])
        mid = geo.point_at_fraction(geom, 0.5)
        self.assertAlmostEqual(mid[0], 5.0, places=4)

    def test_fraction_lands_in_the_correct_leg(self):
        geom = [[0.0, 0.0], [2.0, 0.0], [10.0, 0.0]]  # legs of length 2 then 8
        # 0.1 of the total (10) = distance 1.0 -> still in the first leg.
        self.assertAlmostEqual(geo.point_at_fraction(geom, 0.1)[0], 1.0, places=4)

    def test_empty_geometry_is_none(self):
        self.assertIsNone(geo.point_at_fraction([], 0.5))


class ReverseGeocodeTests(TestCase):
    """coords -> 'City, ST', best-effort (requests mocked)."""

    def setUp(self):
        cache.clear()  # results are cached; isolate each test

    def _resp(self, payload):
        m = mock.Mock()
        m.raise_for_status.return_value = None
        m.json.return_value = payload
        return m

    @mock.patch("apps.trips.services.routing._reverse_uncached", return_value="Reno, NV")
    def test_result_is_cached(self, uncached):
        first = routing.reverse_geocode([-119.81, 39.53])
        second = routing.reverse_geocode([-119.81, 39.53])  # same point → cache hit
        self.assertEqual(first, "Reno, NV")
        self.assertEqual(second, "Reno, NV")
        self.assertEqual(uncached.call_count, 1)  # upstream hit only once

    @mock.patch("apps.trips.services.routing.settings")
    @mock.patch("apps.trips.services.routing.requests.get")
    def test_ors_reverse_builds_city_state(self, get, fake_settings):
        fake_settings.ORS_API_KEY = "k"
        fake_settings.ORS_BASE_URL = "https://ors.test"
        get.return_value = self._resp(
            {"features": [{"properties": {"locality": "Reno", "region_a": "NV"}}]}
        )
        self.assertEqual(routing.reverse_geocode([-119.8, 39.5]), "Reno, NV")

    @mock.patch("apps.trips.services.routing.settings")
    @mock.patch("apps.trips.services.routing.requests.get")
    def test_falls_back_to_empty_on_network_error(self, get, fake_settings):
        fake_settings.ORS_API_KEY = ""  # skip ORS, go straight to Nominatim
        fake_settings.NOMINATIM_BASE_URL = "https://nom.test"
        fake_settings.GEOCODER_USER_AGENT = "test"
        get.side_effect = requests.RequestException("boom")
        self.assertEqual(routing.reverse_geocode([-119.8, 39.5]), "")

    def test_none_coords_returns_empty(self):
        self.assertEqual(routing.reverse_geocode(None), "")

    @mock.patch("apps.trips.services.routing.reverse_geocode")
    def test_many_preserves_order_and_dedups(self, one):
        one.side_effect = lambda c: f"{c[0]}"
        out = routing.reverse_geocode_many([[1.0, 1.0], [2.0, 2.0], [1.0, 1.0], None])
        self.assertEqual(out, ["1.0", "2.0", "1.0", ""])
        self.assertEqual(one.call_count, 2)  # the duplicate point is resolved once


class GeocodeManyTests(TestCase):
    """Concurrent geocoding of the three locations (geocode mocked)."""

    @mock.patch("apps.trips.services.routing.geocode")
    def test_preserves_input_order(self, one):
        one.side_effect = lambda p: [len(p), 0.0]  # deterministic by place length
        out = routing.geocode_many(["AA", "B", "CCC"])
        self.assertEqual(out, [[2, 0.0], [1, 0.0], [3, 0.0]])

    @mock.patch("apps.trips.services.routing.geocode")
    def test_propagates_routing_error(self, one):
        one.side_effect = routing.RoutingError("no match for 'X'")
        with self.assertRaises(routing.RoutingError):
            routing.geocode_many(["X", "Y", "Z"])


class LocateStopsTests(TestCase):
    """views._locate_stops attaches coords + a real place to each non-driving stop."""

    @mock.patch("apps.trips.views.routing.reverse_geocode_many")
    def test_enriches_coords_and_city(self, many):
        many.return_value = ["Truckee, CA"]  # one synthetic stop to name
        timeline = [
            {"status": "D", "note": "Driving", "location": "En route", "miles": 0.0},
            {"status": "OFF", "note": "10-hour reset", "location": "Rest stop", "miles": 50.0},
            {"status": "ON", "note": "Pickup (loading)", "location": "Sacramento, CA", "miles": 100.0},
            {"status": "ON", "note": "Dropoff (unloading)", "location": "Reno, NV", "miles": 200.0},
        ]
        route_info = {"geometry": [[-122.0, 38.0], [-120.0, 39.0], [-119.0, 39.5]], "distance_miles": 200.0}
        views._locate_stops(timeline, route_info, pickup=[-121.0, 38.5], dropoff=[-119.8, 39.5])

        self.assertIsNotNone(timeline[1]["coords"])          # rest interpolated
        self.assertEqual(timeline[1]["location"], "Truckee, CA")
        self.assertEqual(timeline[2]["coords"], [-121.0, 38.5])  # pickup -> exact coords
        self.assertEqual(timeline[3]["coords"], [-119.8, 39.5])  # dropoff -> exact coords
        many.assert_called_once()  # only the synthetic stop needed naming
