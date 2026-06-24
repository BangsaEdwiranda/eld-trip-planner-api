"""Timezone service: coords -> IANA zone, and start = 'now' in that zone."""

from datetime import datetime, timezone
from unittest import mock

from django.conf import settings
from django.test import TestCase

from ..services import timezone as trip_tz


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
