"""Data model for trip planning and generated ELD logs.

A `Trip` captures the four inputs from the assessment brief. After planning, the
computed route summary and the ordered duty-status timeline are persisted, and
the timeline is grouped into one `LogSheet` per calendar day for rendering.
"""

from django.db import models


class DutyStatus(models.TextChoices):
    OFF_DUTY = "OFF", "Off Duty"
    SLEEPER = "SB", "Sleeper Berth"
    DRIVING = "D", "Driving"
    ON_DUTY = "ON", "On Duty (not driving)"


class Trip(models.Model):
    """A single trip plan request and its computed results."""

    # --- Inputs (from the brief) ---
    current_location = models.CharField(max_length=255)
    pickup_location = models.CharField(max_length=255)
    dropoff_location = models.CharField(max_length=255)
    current_cycle_used_hours = models.FloatField(
        default=0,
        help_text="Hours already used in the 70hr/8day cycle at trip start.",
    )

    # --- Resolved coordinates (filled in during planning) ---
    current_coords = models.JSONField(null=True, blank=True)  # [lon, lat]
    pickup_coords = models.JSONField(null=True, blank=True)
    dropoff_coords = models.JSONField(null=True, blank=True)

    # --- Computed route summary ---
    total_distance_miles = models.FloatField(null=True, blank=True)
    total_duration_hours = models.FloatField(null=True, blank=True)
    route_geometry = models.JSONField(
        null=True, blank=True,
        help_text="GeoJSON-style list of [lon, lat] points for the map.",
    )
    stops = models.JSONField(
        null=True, blank=True,
        help_text="Ordered stops: pickup, fuel, rests, breaks, dropoff.",
    )
    timezone = models.CharField(
        max_length=64, blank=True,
        help_text="IANA zone of the current location; all log times are in it.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Trip {self.pk}: {self.pickup_location} -> {self.dropoff_location}"


class LogSheet(models.Model):
    """One ELD daily log sheet (one calendar day) belonging to a trip."""

    trip = models.ForeignKey(
        Trip, related_name="log_sheets", on_delete=models.CASCADE
    )
    date = models.DateField()
    day_index = models.PositiveIntegerField(
        help_text="0-based day number within the trip."
    )
    from_location = models.CharField(max_length=255, blank=True)
    to_location = models.CharField(max_length=255, blank=True)
    total_miles_driving = models.FloatField(default=0)

    # Per-status totals for the day, e.g. {"OFF": 9.5, "SB": 0, "D": 11, "ON": 3.5}
    totals = models.JSONField(default=dict)

    class Meta:
        ordering = ["trip", "day_index"]
        unique_together = ("trip", "day_index")

    def __str__(self) -> str:
        return f"LogSheet {self.date} (trip {self.trip_id})"


class LogSegment(models.Model):
    """A single duty-status interval drawn on a log sheet's grid."""

    log_sheet = models.ForeignKey(
        LogSheet, related_name="segments", on_delete=models.CASCADE
    )
    status = models.CharField(max_length=3, choices=DutyStatus.choices)
    start_minute = models.PositiveIntegerField(
        help_text="Minutes from local midnight (0-1440)."
    )
    end_minute = models.PositiveIntegerField(
        help_text="Minutes from local midnight (0-1440)."
    )
    location = models.CharField(max_length=255, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["log_sheet", "start_minute"]

    def __str__(self) -> str:
        return f"{self.status} {self.start_minute}-{self.end_minute}"
