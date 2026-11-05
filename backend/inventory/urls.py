from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("warehouses", views.WarehouseViewSet, basename="warehouse")
router.register("locations", views.LocationViewSet, basename="location")
router.register("products", views.ProductViewSet, basename="product")
router.register("lots", views.LotViewSet, basename="lot")
router.register("stock", views.StockItemViewSet, basename="stock")
router.register("movements", views.StockMovementViewSet, basename="movement")
router.register("reservations", views.ReservationViewSet, basename="reservation")

urlpatterns = [
    path("health/", views.health, name="health"),
    path("reports/summary/", views.summary_report, name="report-summary"),
    path("", include(router.urls)),
]
