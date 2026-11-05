from django.db import DatabaseError, connection
from django.db.models import Count, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils.translation import gettext as _
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .models import Location, Lot, Product, Reservation, StockItem, StockMovement, Warehouse
from .services import close_reservation
from .serializers import (
    LocationSerializer,
    LotSerializer,
    ProductSerializer,
    ReservationSerializer,
    StockItemSerializer,
    StockMovementQuerySerializer,
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
                raise ValidationError({param: _("Debe ser un identificador numérico.")})
            if param in choices and value not in choices[param]:
                raise ValidationError({param: _("Debe ser uno de: %(choices)s.") % {
                    "choices": ", ".join(choices[param]),
                }})
            if lookup.endswith("is_active") or lookup == "lot_tracked":
                value = value.lower() in {"1", "true", "yes"}
            queryset = queryset.filter(**{lookup: value})
    return queryset


class WarehouseViewSet(viewsets.ModelViewSet):
    serializer_class = WarehouseSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["code", "name"]

    def get_queryset(self):
        return _query_param_filter(
            Warehouse.objects.all(), self.request, {"is_active": "is_active"},
            choices={"is_active": ["true", "false"]},
        )


class LocationViewSet(viewsets.ModelViewSet):
    serializer_class = LocationSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["code", "warehouse__code", "barcode"]

    def get_queryset(self):
        queryset = Location.objects.select_related("warehouse")
        return _query_param_filter(
            queryset,
            self.request,
            {
                "warehouse": "warehouse_id", "location_type": "location_type",
                "is_active": "is_active", "barcode": "barcode",
                "warehouse_is_active": "warehouse__is_active",
            },
            choices={
                "location_type": Location.Type.values,
                "warehouse_is_active": ["true", "false"],
            },
        )


class ProductViewSet(viewsets.ModelViewSet):
    serializer_class = ProductSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["sku", "name", "barcode"]

    def get_queryset(self):
        reserved = (
            Reservation.objects.filter(
                stock_item__product_id=OuterRef("pk"), status=Reservation.Status.ACTIVE,
            )
            .order_by().values("stock_item__product_id").annotate(total=Sum("quantity")).values("total")
        )
        queryset = Product.objects.annotate(
            total_quantity=Coalesce(Sum("stock_items__quantity"), Value(0)),
            total_reserved=Coalesce(Subquery(reserved), Value(0)),
        )
        return _query_param_filter(
            queryset, self.request,
            {"is_active": "is_active", "barcode": "barcode", "lot_tracked": "lot_tracked"},
            choices={"lot_tracked": ["true", "false"]},
        )


class LotViewSet(viewsets.ModelViewSet):
    serializer_class = LotSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["lot_number", "product__sku"]

    def get_queryset(self):
        return _query_param_filter(
            Lot.objects.select_related("product"), self.request,
            {"product": "product_id", "lot_number": "lot_number"},
        )


class StockItemViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = StockItemSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = [
        "product__sku", "product__name", "location__code", "location__warehouse__code", "lot__lot_number",
    ]

    def get_queryset(self):
        queryset = StockItem.objects.with_balances().select_related(
            "product", "location__warehouse", "lot"
        ).filter(quantity__gt=0)
        reservable = self.request.query_params.get("reservable")
        if reservable not in (None, "", "true", "false"):
            raise ValidationError({"reservable": _("Debe ser uno de: %(choices)s.") % {
                "choices": "true, false",
            }})
        if reservable == "true":
            queryset = queryset.filter(
                product__is_active=True, location__is_active=True,
                location__warehouse__is_active=True, quantity__gt=F("_reserved_quantity"),
            )
        return _query_param_filter(
            queryset,
            self.request,
            {
                "product": "product_id", "location": "location_id",
                "warehouse": "location__warehouse_id", "lot": "lot_id",
            },
        )


class ReservationViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet,
):
    serializer_class = ReservationSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["reference", "stock_item__product__sku", "stock_item__lot__lot_number"]

    def get_queryset(self):
        queryset = Reservation.objects.select_related(
            "stock_item__product", "stock_item__location__warehouse", "stock_item__lot",
        )
        return _query_param_filter(
            queryset, self.request,
            {
                "status": "status", "stock_item": "stock_item_id",
                "product": "stock_item__product_id", "location": "stock_item__location_id",
                "warehouse": "stock_item__location__warehouse_id",
                "reference": "reference",
                "lot": "stock_item__lot_id",
            },
            choices={"status": Reservation.Status.values},
        )

    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        reservation = close_reservation(self.get_object(), fulfill=False)
        return Response(self.get_serializer(reservation).data)

    @action(detail=True, methods=["post"])
    def fulfill(self, request, pk=None):
        reservation = close_reservation(self.get_object(), fulfill=True)
        return Response(self.get_serializer(reservation).data)


class StockMovementViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Movements are an append-only ledger: they can be created and read, never edited."""

    serializer_class = StockMovementSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["product__sku", "reference", "lot__lot_number"]

    @action(detail=False, methods=["post"])
    def batch(self, request):
        serializer = self.get_serializer(data=request.data, many=True, allow_empty=False)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def get_queryset(self):
        queryset = StockMovement.objects.select_related(
            "product", "from_location__warehouse", "to_location__warehouse", "lot"
        )
        queryset = _query_param_filter(
            queryset,
            self.request,
            {"movement_type": "movement_type", "product": "product_id", "lot": "lot_id"},
            choices={"movement_type": StockMovement.Type.values},
        )
        params = StockMovementQuerySerializer(
            data={
                key: value
                for key, value in self.request.query_params.items()
                if value != ""
            }
        )
        params.is_valid(raise_exception=True)
        values = params.validated_data
        for param, lookup in (
            ("created_at_after", "created_at__gte"),
            ("created_at_before", "created_at__lte"),
            ("occurred_at_after", "occurred_at__gte"),
            ("occurred_at_before", "occurred_at__lte"),
            ("reference", "reference__icontains"),
        ):
            if param in values:
                queryset = queryset.filter(**{lookup: values[param]})
        for param, source, target in (
            ("warehouse", "from_location__warehouse_id", "to_location__warehouse_id"),
            ("location", "from_location_id", "to_location_id"),
        ):
            if param in values:
                queryset = queryset.filter(
                    Q(**{source: values[param]}) | Q(**{target: values[param]})
                )
        ordering = values["ordering"].split(",")
        if not any(field in {"id", "-id"} for field in ordering):
            ordering.append("-id" if ordering[0].startswith("-") else "id")
        return queryset.order_by(*ordering)


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
    warehouse_quantities = {}
    warehouse_totals = {}
    stock_totals = (
        StockItem.objects.order_by()
        .values("location__warehouse_id", "product_id")
        .annotate(total=Sum("quantity"))
    )
    for row in stock_totals:
        warehouse_id = row["location__warehouse_id"]
        warehouse_quantities[(warehouse_id, row["product_id"])] = row["total"]
        warehouse_totals[warehouse_id] = warehouse_totals.get(warehouse_id, 0) + row["total"]
    monitored_products = list(
        Product.objects.filter(is_active=True, reorder_level__gt=0)
        .values("id", "sku", "name", "reorder_level")
    )
    warehouse_breakdown = []
    for warehouse in Warehouse.objects.all():
        local_low_stock = []
        for product in monitored_products:
            quantity = warehouse_quantities.get((warehouse.pk, product["id"]), 0)
            if quantity <= product["reorder_level"]:
                local_low_stock.append({**product, "total_quantity": quantity})
        local_low_stock.sort(key=lambda product: (product["total_quantity"], product["sku"]))
        warehouse_breakdown.append(
            {
                "id": warehouse.pk,
                "code": warehouse.code,
                "name": warehouse.name,
                "is_active": warehouse.is_active,
                "total_units": warehouse_totals.get(warehouse.pk, 0),
                "low_stock_count": len(local_low_stock),
                "low_stock": local_low_stock[:50],
            }
        )
    return Response(
        {
            "warehouses": Warehouse.objects.count(),
            "locations": Location.objects.count(),
            "products": products.count(),
            "total_units": StockItem.objects.aggregate(total=Coalesce(Sum("quantity"), Value(0)))["total"],
            "movements": movement_counts,
            "warehouse_breakdown": warehouse_breakdown,
            "low_stock_count": low_stock.count(),
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
