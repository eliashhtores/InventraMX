from django.core.exceptions import ValidationError as ModelValidationError
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from .models import Location, Lot, Product, Reservation, StockItem, StockMovement, Warehouse, validate_product_lot
from .services import record_movement, record_movements, reserve_stock


class WarehouseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Warehouse
        fields = ["id", "code", "name", "address", "is_active", "created_at"]
        read_only_fields = ["created_at"]


class LocationSerializer(serializers.ModelSerializer):
    warehouse_code = serializers.CharField(source="warehouse.code", read_only=True)

    class Meta:
        model = Location
        fields = ["id", "warehouse", "warehouse_code", "code", "barcode", "location_type", "is_active"]

    def validate(self, attrs):
        if self.instance and attrs.get("warehouse", self.instance.warehouse) != self.instance.warehouse:
            raise serializers.ValidationError(
                {"warehouse": _("No se puede cambiar el almacén después de crear la ubicación.")}
            )
        return attrs


class ModelValidationSerializer(serializers.ModelSerializer):
    def create(self, validated_data):
        try:
            return super().create(validated_data)
        except ModelValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc

    def update(self, instance, validated_data):
        try:
            return super().update(instance, validated_data)
        except ModelValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc


class ProductSerializer(ModelValidationSerializer):
    total_quantity = serializers.SerializerMethodField()
    total_reserved = serializers.SerializerMethodField()
    total_available = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "sku",
            "barcode",
            "lot_tracked",
            "name",
            "description",
            "unit",
            "reorder_level",
            "is_active",
            "total_quantity",
            "total_reserved",
            "total_available",
            "created_at",
        ]
        read_only_fields = ["created_at"]

    def get_total_quantity(self, obj) -> int:
        # Annotated by ProductViewSet.get_queryset; freshly created products have no stock yet.
        return getattr(obj, "total_quantity", 0)

    def get_total_reserved(self, obj) -> int:
        return getattr(obj, "total_reserved", 0)

    def get_total_available(self, obj) -> int:
        return self.get_total_quantity(obj) - self.get_total_reserved(obj)


class LotSerializer(ModelValidationSerializer):
    product_sku = serializers.CharField(source="product.sku", read_only=True)

    class Meta:
        model = Lot
        fields = ["id", "product", "product_sku", "lot_number", "expiry_date", "created_at"]
        read_only_fields = ["created_at"]
        extra_kwargs = {"expiry_date": {"required": False, "allow_null": True}}

    def validate(self, attrs):
        expiry = attrs.get("expiry_date", self.instance.expiry_date if self.instance else None)
        if expiry is None and (self.instance is None or self.instance.expiry_date is not None):
            raise serializers.ValidationError(
                {"expiry_date": _("La fecha de caducidad es obligatoria para los números de lote nuevos.")}
            )
        return attrs


class StockItemSerializer(serializers.ModelSerializer):
    lot_number = serializers.CharField(source="lot.lot_number", read_only=True, default=None)
    expiry_date = serializers.DateField(source="lot.expiry_date", read_only=True, default=None)
    reserved_quantity = serializers.IntegerField(read_only=True)
    available_quantity = serializers.IntegerField(read_only=True)
    product_sku = serializers.CharField(source="product.sku", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    location_code = serializers.CharField(source="location.code", read_only=True)
    warehouse_code = serializers.CharField(source="location.warehouse.code", read_only=True)

    class Meta:
        model = StockItem
        fields = [
            "id",
            "product",
            "product_sku",
            "product_name",
            "lot",
            "lot_number",
            "expiry_date",
            "location",
            "location_code",
            "warehouse_code",
            "quantity",
            "reserved_quantity",
            "available_quantity",
            "updated_at",
        ]
        read_only_fields = fields


class ReservationSerializer(serializers.ModelSerializer):
    lot = serializers.IntegerField(source="stock_item.lot_id", read_only=True, allow_null=True)
    lot_number = serializers.CharField(source="stock_item.lot.lot_number", read_only=True, default=None)
    expiry_date = serializers.DateField(source="stock_item.lot.expiry_date", read_only=True, default=None)
    product_sku = serializers.CharField(source="stock_item.product.sku", read_only=True)
    location_label = serializers.StringRelatedField(source="stock_item.location", read_only=True)

    class Meta:
        model = Reservation
        fields = [
            "id", "stock_item", "product_sku", "location_label", "quantity", "reference",
            "status", "movement", "created_at", "updated_at",
            "lot", "lot_number", "expiry_date",
        ]
        read_only_fields = ["status", "movement", "created_at", "updated_at"]

    def create(self, validated_data):
        return reserve_stock(**validated_data)


class StockMovementQuerySerializer(serializers.Serializer):
    created_at_after = serializers.DateTimeField(required=False)
    created_at_before = serializers.DateTimeField(required=False)
    occurred_at_after = serializers.DateTimeField(required=False)
    occurred_at_before = serializers.DateTimeField(required=False)
    warehouse = serializers.IntegerField(required=False, min_value=1, max_value=9223372036854775807)
    location = serializers.IntegerField(required=False, min_value=1, max_value=9223372036854775807)
    reference = serializers.CharField(required=False)
    ordering = serializers.CharField(required=False, default="-created_at,-id")

    def validate_ordering(self, value):
        fields = [field.strip() for field in value.split(",")]
        allowed = {
            "created_at", "-created_at", "occurred_at", "-occurred_at",
            "id", "-id", "quantity", "-quantity",
        }
        if any(field not in allowed for field in fields):
            raise serializers.ValidationError(
                _("Usa created_at, occurred_at, id o quantity separados por comas, "
                  "con el prefijo '-' opcional.")
            )
        return ",".join(fields)

    def validate(self, attrs):
        for field in ("created_at", "occurred_at"):
            after = attrs.get(f"{field}_after")
            before = attrs.get(f"{field}_before")
            if after is not None and before is not None and after > before:
                raise serializers.ValidationError(
                    {f"{field}_before": _("Debe ser igual o posterior a %(field)s.") % {"field": f"{field}_after"}}
                )
        return attrs


class StockMovementListSerializer(serializers.ListSerializer):
    def to_internal_value(self, data):
        try:
            return super().to_internal_value(data)
        except serializers.ValidationError as exc:
            # DRF 3.18 uses indexed dictionaries; keep line errors aligned with stock failures.
            if isinstance(exc.detail, dict) and all(isinstance(key, int) for key in exc.detail):
                raise serializers.ValidationError(
                    [exc.detail.get(index, {}) for index in range(len(data))]
                ) from exc
            raise

    def create(self, validated_data):
        return record_movements(validated_data)


class StockMovementSerializer(serializers.ModelSerializer):
    lot_number = serializers.CharField(source="lot.lot_number", read_only=True, default=None)
    expiry_date = serializers.DateField(source="lot.expiry_date", read_only=True, default=None)
    product_sku = serializers.CharField(source="product.sku", read_only=True)
    from_location_label = serializers.StringRelatedField(source="from_location", read_only=True)
    to_location_label = serializers.StringRelatedField(source="to_location", read_only=True)

    class Meta:
        model = StockMovement
        list_serializer_class = StockMovementListSerializer
        fields = [
            "id",
            "movement_type",
            "product",
            "lot",
            "lot_number",
            "expiry_date",
            "product_sku",
            "from_location",
            "from_location_label",
            "to_location",
            "to_location_label",
            "quantity",
            "reference",
            "notes",
            "occurred_at",
            "created_at",
        ]
        read_only_fields = ["created_at"]

    def validate(self, attrs):
        movement_type = attrs.get("movement_type")
        source = attrs.get("from_location")
        target = attrs.get("to_location")
        Type = StockMovement.Type
        errors = {}
        try:
            validate_product_lot(attrs["product"], attrs.get("lot"))
        except ModelValidationError as exc:
            errors.update(exc.message_dict)

        if movement_type == Type.INBOUND:
            if not target:
                errors["to_location"] = _("Las entradas requieren una ubicación de destino.")
            if source:
                errors["from_location"] = _("Las entradas no deben indicar una ubicación de origen.")
        elif movement_type == Type.OUTBOUND:
            if not source:
                errors["from_location"] = _("Las salidas requieren una ubicación de origen.")
            if target:
                errors["to_location"] = _("Las salidas no deben indicar una ubicación de destino.")
        elif movement_type == Type.TRANSFER:
            if not source:
                errors["from_location"] = _("Las transferencias requieren una ubicación de origen.")
            if not target:
                errors["to_location"] = _("Las transferencias requieren una ubicación de destino.")
            if source and target and source == target:
                errors["to_location"] = _("Las ubicaciones de origen y destino deben ser diferentes.")
        elif movement_type == Type.ADJUSTMENT:
            if bool(source) == bool(target):
                errors["non_field_errors"] = _(
                    "Los ajustes requieren exactamente una ubicación: to_location para aumentar "
                    "o from_location para disminuir las existencias."
                )

        if not attrs["product"].is_active:
            errors["product"] = _("El producto está inactivo.")
        for field, location in (("from_location", source), ("to_location", target)):
            if location and field not in errors:
                if not location.is_active:
                    errors[field] = _("La ubicación está inactiva.")
                elif not location.warehouse.is_active:
                    errors[field] = _("El almacén está inactivo.")

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        return record_movement(**validated_data)
