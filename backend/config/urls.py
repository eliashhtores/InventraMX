"""URL configuration for the admin and REST API."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("inventory.urls")),
]
