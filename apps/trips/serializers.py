"""DRF serializers for trip input and the planned trip + ELD log output."""

from rest_framework import serializers

from .models import LogSegment, LogSheet, Trip


class TripInputSerializer(serializers.Serializer):
    """The four inputs from the assessment brief."""

    current_location = serializers.CharField(max_length=255)
    pickup_location = serializers.CharField(max_length=255)
    dropoff_location = serializers.CharField(max_length=255)
    current_cycle_used_hours = serializers.FloatField(min_value=0, max_value=70)


class LogSegmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LogSegment
        fields = ["status", "start_minute", "end_minute", "location", "note"]


class LogSheetSerializer(serializers.ModelSerializer):
    segments = LogSegmentSerializer(many=True, read_only=True)

    class Meta:
        model = LogSheet
        fields = [
            "date",
            "day_index",
            "from_location",
            "to_location",
            "total_miles_driving",
            "totals",
            "segments",
        ]


class TripSerializer(serializers.ModelSerializer):
    """Full trip output: inputs, route summary, and the ELD log sheets."""

    log_sheets = LogSheetSerializer(many=True, read_only=True)

    class Meta:
        model = Trip
        fields = [
            "id",
            "current_location",
            "pickup_location",
            "dropoff_location",
            "current_cycle_used_hours",
            "current_coords",
            "pickup_coords",
            "dropoff_coords",
            "total_distance_miles",
            "total_duration_hours",
            "route_geometry",
            "stops",
            "timezone",
            "log_sheets",
            "created_at",
        ]
