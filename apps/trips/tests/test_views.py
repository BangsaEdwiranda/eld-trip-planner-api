"""API layer: the geocode endpoint, input validation, stop location, and the
full POST /api/trips/ flow (orchestration + persistence + serialization)."""

import json
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from .. import views
from ..models import Trip
from ..services import routing


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


class TripInputValidationTests(TestCase):
    """POST /api/trips/ rejects bad input before any network call."""

    url = reverse("trip-list")

    def _post(self, body):
        return self.client.post(self.url, json.dumps(body), content_type="application/json")

    def test_cycle_hours_over_70_is_rejected(self):
        resp = self._post({
            "current_location": "A", "pickup_location": "B",
            "dropoff_location": "C", "current_cycle_used_hours": 99,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("current_cycle_used_hours", resp.json())

    def test_negative_cycle_hours_is_rejected(self):
        resp = self._post({
            "current_location": "A", "pickup_location": "B",
            "dropoff_location": "C", "current_cycle_used_hours": -1,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("current_cycle_used_hours", resp.json())

    def test_missing_field_is_rejected(self):
        resp = self._post({"current_location": "A", "pickup_location": "B"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("dropoff_location", resp.json())


class PlanTripIntegrationTests(TestCase):
    """End-to-end POST /api/trips/: orchestration + persistence + serialization.

    The network layer (geocode/route/reverse-geocode) is mocked; everything else
    — leg building, HOS sim, stop location, ELD sheets, DB writes, serializer —
    runs for real. Timezone resolution runs for real too (offline).
    """

    url = reverse("trip-list")

    def setUp(self):
        cache.clear()

    BODY = {
        "current_location": "San Francisco, CA",
        "pickup_location": "Sacramento, CA",
        "dropoff_location": "Reno, NV",
        "current_cycle_used_hours": 0,
    }
    COORDS = [[-122.43, 37.77], [-121.47, 38.58], [-119.81, 39.53]]  # current, pickup, dropoff
    ROUTE = {
        "distance_miles": 222.0,
        "duration_hours": 4.0,
        "geometry": [[-122.43, 37.77], [-121.47, 38.58], [-119.81, 39.53]],
        "legs": [
            {"distance_miles": 90.0, "duration_hours": 1.6},
            {"distance_miles": 132.0, "duration_hours": 2.4},
        ],
    }

    @mock.patch("apps.trips.services.routing.reverse_geocode_many", return_value=[])
    @mock.patch("apps.trips.services.routing.route")
    @mock.patch("apps.trips.services.routing.geocode_many")
    def test_plans_persists_and_serializes_a_trip(self, geocode_many, route, _rev):
        geocode_many.return_value = self.COORDS
        route.return_value = self.ROUTE

        resp = self.client.post(self.url, json.dumps(self.BODY), content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        data = resp.json()

        # Persisted: one Trip with at least one sheet and segments.
        self.assertEqual(Trip.objects.count(), 1)
        trip = Trip.objects.get()
        self.assertGreaterEqual(trip.log_sheets.count(), 1)
        self.assertTrue(trip.log_sheets.first().segments.exists())

        # Serialized route summary + timezone (resolved for real from SF coords).
        self.assertEqual(data["total_distance_miles"], 222.0)
        self.assertEqual(data["timezone"], "America/Los_Angeles")

        # Pickup/dropoff stops carry their exact geocoded coords.
        stops = {s["type"]: s for s in data["stops"]}
        self.assertEqual(stops["Pickup (loading)"]["coords"], [-121.47, 38.58])
        self.assertEqual(stops["Dropoff (unloading)"]["coords"], [-119.81, 39.53])

        # Every sheet is a full, off-duty-padded 24h.
        for sheet in data["log_sheets"]:
            self.assertEqual(round(sum(sheet["totals"].values()), 2), 24.0)

    @mock.patch(
        "apps.trips.services.routing.geocode_many",
        side_effect=routing.RoutingError("We couldn't find a location matching 'X'."),
    )
    def test_routing_failure_returns_400_detail(self, _geo):
        resp = self.client.post(self.url, json.dumps(self.BODY), content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("detail", resp.json())
        self.assertEqual(Trip.objects.count(), 0)  # nothing persisted on failure
