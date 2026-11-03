from django.db import DatabaseError, connection
from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .models import Location, Product, StockItem, StockMovement, Warehouse
from .serializers import (
    LocationSerializer,
    ProductSerializer,
    StockItemSerializer,
    StockMovementSerializer,
    WarehouseSerializer,
)


def _query_param_filter(queryset, request, mapping, choices=None):
    """Apply simple equality filters from query params, e.g. ?warehouse=1.

    ``choices`` maps a param to its allowed values; unknown values are rejected with 400.
    """
    choices = choices or {}
    for param, lookup in mapping.items():
        value = request.query_params.get(param)
        if value not in (None, ""):
            if lookup.endswith("_id") and not value.isdigit():
                raise ValidationError({param: "Must be a numeric id."})
            if param in choices and value not in choices[param]:
                raise ValidationError({param: f"Must be one of: {', '.join(choices[param])}."})
            if lookup.endswith("is_active"):
                value = value.lower() in {"1", "true", "yes"}
            queryset = queryset.filter(**{lookup: value})
    return queryset


class WarehouseViewSet(viewsets.ModelViewSet):
    queryset = Warehouse.objects.all()
    serializer_class = WarehouseSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["code", "name"]


class LocationViewSet(viewsets.ModelViewSet):
    serializer_class = LocationSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["code", "warehouse__code"]

    def get_queryset(self):
        queryset = Location.objects.select_related("warehouse")
        return _query_param_filter(
            queryset,
            self.request,
            {"warehouse": "warehouse_id", "location_type": "location_type", "is_active": "is_active"},
            choices={"location_type": Location.Type.values},
        )


class ProductViewSet(viewsets.ModelViewSet):
    serializer_class = ProductSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["sku", "name"]

    def get_queryset(self):
        queryset = Product.objects.annotate(total_quantity=Coalesce(Sum("stock_items__quantity"), Value(0)))
        return _query_param_filter(queryset, self.request, {"is_active": "is_active"})


class StockItemViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = StockItemSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["product__sku", "product__name", "location__code"]

    def get_queryset(self):
        queryset = StockItem.objects.select_related("product", "location__warehouse").filter(quantity__gt=0)
        return _query_param_filter(
            queryset,
            self.request,
            {"product": "product_id", "location": "location_id", "warehouse": "location__warehouse_id"},
        )


class StockMovementViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Movements are an append-only ledger: they can be created and read, never edited."""

    serializer_class = StockMovementSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["product__sku", "reference"]

    def get_queryset(self):
        queryset = StockMovement.objects.select_related(
            "product", "from_location__warehouse", "to_location__warehouse"
        )
        return _query_param_filter(
            queryset,
            self.request,
            {"movement_type": "movement_type", "product": "product_id"},
            choices={"movement_type": StockMovement.Type.values},
        )


@api_view(["GET"])
def summary_report(request):
    products = Product.objects.filter(is_active=True).annotate(
        total_quantity=Coalesce(Sum("stock_items__quantity"), Value(0))
    )
    # reorder_level=0 means "no reorder alert" for the product.
    low_stock = products.filter(reorder_level__gt=0, total_quantity__lte=F("reorder_level")).order_by(
        "total_quantity", "sku"
    )
    movement_counts = StockMovement.objects.aggregate(
        **{t.value: Count("id", filter=Q(movement_type=t.value)) for t in StockMovement.Type}
    )
    return Response(
        {
            "warehouses": Warehouse.objects.count(),
            "locations": Location.objects.count(),
            "products": Product.objects.count(),
            "total_units": StockItem.objects.aggregate(total=Coalesce(Sum("quantity"), Value(0)))["total"],
            "movements": movement_counts,
            "low_stock": [
                {
                    "id": p.id,
                    "sku": p.sku,
                    "name": p.name,
                    "total_quantity": p.total_quantity,
                    "reorder_level": p.reorder_level,
                }
                for p in low_stock[:50]
            ],
        }
    )


@api_view(["GET"])
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return Response({"status": "unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({"status": "ok"})
