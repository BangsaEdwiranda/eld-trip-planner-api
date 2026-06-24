from django.contrib import admin

from .models import LogSegment, LogSheet, Trip


class LogSegmentInline(admin.TabularInline):
    model = LogSegment
    extra = 0


class LogSheetInline(admin.TabularInline):
    model = LogSheet
    extra = 0
    show_change_link = True


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "pickup_location",
        "dropoff_location",
        "total_distance_miles",
        "total_duration_hours",
        "created_at",
    )
    inlines = [LogSheetInline]


@admin.register(LogSheet)
class LogSheetAdmin(admin.ModelAdmin):
    list_display = ("id", "trip", "date", "day_index", "total_miles_driving")
    inlines = [LogSegmentInline]
