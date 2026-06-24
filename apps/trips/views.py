"""API views: plan a trip and retrieve planned trips with their ELD logs."""

from __future__ import annotations

from django.conf import settings
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import LogSegment, LogSheet, Trip
from .serializers import TripInputSerializer, TripSerializer
from .services import eld, geo, hos, routing
from .services import timezone as trip_tz

HOS = settings.HOS_SETTINGS

# Place-autocomplete tuning.
_SUGGEST_MIN_CHARS = 2   # ignore very short queries (noise, rate-limit friendly)
_SUGGEST_MAX_LIMIT = 10  # cap how many suggestions a caller can request


class PlaceSuggestView(APIView):
    """``GET /api/geocode/?q=<text>&limit=<n>`` — place suggestions for the
    frontend's location autocomplete.

    Returns ``{"results": [{"label": str, "coords": [lon, lat]}, ...]}``. Short or
    empty queries return an empty list (200) so a typeahead can call freely; an
    upstream geocoder failure surfaces as a 400 with ``detail``.
    """

    def get(self, request, *args, **kwargs):
        query = request.query_params.get("q", "").strip()
        if len(query) < _SUGGEST_MIN_CHARS:
            return Response({"results": []})

        try:
            limit = int(request.query_params.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, _SUGGEST_MAX_LIMIT))

        try:
            results = routing.suggest_places(query, limit=limit)
        except routing.RoutingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"results": results})


class TripViewSet(viewsets.ModelViewSet):
    """CRUD for trips. ``create`` runs the full plan: route -> HOS -> ELD logs."""

    queryset = Trip.objects.prefetch_related("log_sheets__segments")
    serializer_class = TripSerializer

    def create(self, request, *args, **kwargs):
        input_serializer = TripInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        try:
            trip = self._plan_trip(data)
        except routing.RoutingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        output = TripSerializer(trip)
        return Response(output.data, status=status.HTTP_201_CREATED)

    def _plan_trip(self, data: dict) -> Trip:
        # 1) Geocode the three inputs (concurrently — they're independent).
        current, pickup, dropoff = routing.geocode_many(
            [data["current_location"], data["pickup_location"], data["dropoff_location"]]
        )

        # 2) Route current -> pickup -> dropoff.
        route_info = routing.route([current, pickup, dropoff])

        # 3) Build legs (drive then on-duty stop) and simulate Hours of Service.
        #    The driver drives current -> pickup, loads, then drives -> dropoff.
        #    The duty timeline runs in the current location's local zone, starting
        #    "now" (when the driver goes on duty); FMCSA defines no fixed shift start.
        tz_name = trip_tz.timezone_for(current)
        legs = _build_legs(route_info, data["pickup_location"], data["dropoff_location"])
        timeline = hos.plan_timeline(
            legs=legs,
            start_time=trip_tz.start_time_for(current),
            current_cycle_used_hours=data["current_cycle_used_hours"],
        )

        # 4) Locate each stop on the route and name the place (city, ST), so rests
        #    and fuel stops carry real coordinates + locations, not generic labels.
        _locate_stops(timeline, route_info, pickup, dropoff)

        # 5) Split the (enriched) timeline into per-day ELD log sheets.
        sheets = eld.build_log_sheets(timeline)

        # 6) Persist everything.
        trip = Trip.objects.create(
            current_location=data["current_location"],
            pickup_location=data["pickup_location"],
            dropoff_location=data["dropoff_location"],
            current_cycle_used_hours=data["current_cycle_used_hours"],
            current_coords=current,
            pickup_coords=pickup,
            dropoff_coords=dropoff,
            total_distance_miles=round(route_info["distance_miles"], 1),
            total_duration_hours=round(route_info["duration_hours"], 2),
            route_geometry=route_info["geometry"],
            stops=_extract_stops(timeline),
            timezone=tz_name,
        )

        for sheet in sheets:
            log_sheet = LogSheet.objects.create(
                trip=trip,
                date=sheet["date"],
                day_index=sheet["day_index"],
                total_miles_driving=sheet["total_miles_driving"],
                totals=sheet["totals"],
            )
            LogSegment.objects.bulk_create(
                LogSegment(
                    log_sheet=log_sheet,
                    status=seg["status"],
                    start_minute=seg["start_minute"],
                    end_minute=seg["end_minute"],
                    location=seg["location"],
                    note=seg["note"],
                )
                for seg in sheet["segments"]
            )

        return trip


def _build_legs(route_info: dict, pickup_label: str, dropoff_label: str) -> list[dict]:
    """Turn the routed waypoints into HOS legs.

    Leg 1 = current -> pickup, then 1h loading; leg 2 = pickup -> dropoff, then
    1h unloading. Falls back to a single drive-then-dropoff leg if the routing
    backend didn't return per-leg data.
    """
    route_legs = route_info.get("legs") or []
    if len(route_legs) >= 2:
        to_pickup, to_dropoff = route_legs[0], route_legs[1]
    else:
        to_pickup = {"distance_miles": 0.0, "duration_hours": 0.0}
        to_dropoff = {
            "distance_miles": route_info["distance_miles"],
            "duration_hours": route_info["duration_hours"],
        }

    return [
        {
            **to_pickup,
            "location": "En route to pickup",
            "arrival": {
                "hours": HOS["PICKUP_HOURS"],
                "location": pickup_label,
                "note": "Pickup (loading)",
            },
        },
        {
            **to_dropoff,
            "location": "En route to dropoff",
            "arrival": {
                "hours": HOS["DROPOFF_HOURS"],
                "location": dropoff_label,
                "note": "Dropoff (unloading)",
            },
        },
    ]


def _extract_stops(timeline: list[dict]) -> list[dict]:
    """Pull the non-driving events (pickup, fuel, rests, dropoff) for the map."""
    return [
        {
            "type": seg["note"],
            "status": seg["status"],
            "start": seg["start"],
            "end": seg["end"],
            "location": seg["location"],
            "coords": seg.get("coords"),  # [lon, lat] for the map marker
        }
        for seg in timeline
        if seg["status"] != "D"
    ]


def _locate_stops(
    timeline: list[dict], route_info: dict, pickup: list[float], dropoff: list[float]
) -> None:
    """Give every non-driving stop a position + a real place name (mutates timeline).

    Pickup/dropoff use their known geocoded coordinates; breaks, rests and fuel
    stops are projected onto the route polyline by how far the driver has travelled
    (their cumulative ``miles``), then reverse-geocoded to ``"City, ST"`` so the log
    remarks read like a real RODS instead of "Rest stop". Reverse-geocoding is
    best-effort — an unresolved point keeps its generic label.
    """
    geometry = route_info.get("geometry") or []
    total_miles = route_info.get("distance_miles") or 0.0

    to_name = []  # (segment, coords) for the synthetic stops needing a place name
    for seg in timeline:
        if seg["status"] == "D":
            continue
        note = seg.get("note", "").lower()
        if note.startswith("pickup"):
            seg["coords"] = pickup
        elif note.startswith("dropoff"):
            seg["coords"] = dropoff
        else:
            fraction = seg["miles"] / total_miles if total_miles else 0.0
            seg["coords"] = geo.point_at_fraction(geometry, fraction)
            to_name.append(seg)

    labels = routing.reverse_geocode_many([seg["coords"] for seg in to_name])
    for seg, label in zip(to_name, labels):
        if label:
            seg["location"] = label
