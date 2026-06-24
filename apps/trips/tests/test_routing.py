"""routing.py: country-filtered geocoding, reverse geocoding + caching, and the
concurrent geocode helper. All network calls are mocked."""

from unittest import mock

import requests
from django.core.cache import cache
from django.test import TestCase

from ..services import routing


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
