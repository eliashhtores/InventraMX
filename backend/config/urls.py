"""URL configuration: the backend only exposes the REST API under /api/."""

from django.urls import include, path

urlpatterns = [
    path("api/", include("inventory.urls")),
]
