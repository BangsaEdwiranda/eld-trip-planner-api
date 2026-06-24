"""Pure polyline geometry: projecting a distance fraction onto the route."""

from django.test import TestCase

from ..services import geo


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
