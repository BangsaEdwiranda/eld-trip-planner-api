"""Router for the trips API."""

from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import PlaceSuggestView, TripViewSet

router = DefaultRouter()
router.register(r"trips", TripViewSet, basename="trip")

urlpatterns = [
    path("geocode/", PlaceSuggestView.as_view(), name="geocode"),
    *router.urls,
]
