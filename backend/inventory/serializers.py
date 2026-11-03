from rest_framework import serializers

from .models import Location, Product, StockItem, StockMovement, Warehouse
from .services import record_movement


class WarehouseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Warehouse
        fields = ["id", "code", "name", "address", "is_active", "created_at"]
        read_only_fields = ["created_at"]


class LocationSerializer(serializers.ModelSerializer):
    warehouse_code = serializers.CharField(source="warehouse.code", read_only=True)

    class Meta:
        model = Location
        fields = ["id", "warehouse", "warehouse_code", "code", "location_type", "is_active"]

    def validate(self, attrs):
        if self.instance and attrs.get("warehouse", self.instance.warehouse) != self.instance.warehouse:
            raise serializers.ValidationError(
                {"warehouse": "Warehouse cannot be changed after location creation."}
            )
        return attrs


class ProductSerializer(serializers.ModelSerializer):
    total_quantity = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "sku",
            "name",
            "description",
            "unit",
            "reorder_level",
            "is_active",
            "total_quantity",
            "created_at",
        ]
        read_only_fields = ["created_at"]

    def get_total_quantity(self, obj) -> int:
        # Annotated by ProductViewSet.get_queryset; freshly created products have no stock yet.
        return getattr(obj, "total_quantity", 0)


class StockItemSerializer(serializers.ModelSerializer):
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
            "location",
            "location_code",
            "warehouse_code",
            "quantity",
            "updated_at",
        ]
        read_only_fields = fields


class StockMovementSerializer(serializers.ModelSerializer):
    product_sku = serializers.CharField(source="product.sku", read_only=True)
    from_location_label = serializers.StringRelatedField(source="from_location", read_only=True)
    to_location_label = serializers.StringRelatedField(source="to_location", read_only=True)

    class Meta:
        model = StockMovement
        fields = [
            "id",
            "movement_type",
            "product",
            "product_sku",
            "from_location",
            "from_location_label",
            "to_location",
            "to_location_label",
            "quantity",
            "reference",
            "notes",
            "created_at",
        ]
        read_only_fields = ["created_at"]

    def validate(self, attrs):
        movement_type = attrs.get("movement_type")
        source = attrs.get("from_location")
        target = attrs.get("to_location")
        Type = StockMovement.Type
        errors = {}

        if movement_type == Type.INBOUND:
            if not target:
                errors["to_location"] = "Inbound movements require a destination location."
            if source:
                errors["from_location"] = "Inbound movements must not have a source location."
        elif movement_type == Type.OUTBOUND:
            if not source:
                errors["from_location"] = "Outbound movements require a source location."
            if target:
                errors["to_location"] = "Outbound movements must not have a destination location."
        elif movement_type == Type.TRANSFER:
            if not source:
                errors["from_location"] = "Transfers require a source location."
            if not target:
                errors["to_location"] = "Transfers require a destination location."
            if source and target and source == target:
                errors["to_location"] = "Source and destination must be different."
        elif movement_type == Type.ADJUSTMENT:
            if bool(source) == bool(target):
                errors["non_field_errors"] = (
                    "Adjustments require exactly one location: to_location to increase "
                    "or from_location to decrease stock."
                )

        if not attrs["product"].is_active:
            errors["product"] = "Product is inactive."
        for field, location in (("from_location", source), ("to_location", target)):
            if location and field not in errors:
                if not location.is_active:
                    errors[field] = "Location is inactive."
                elif not location.warehouse.is_active:
                    errors[field] = "Warehouse is inactive."

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        return record_movement(**validated_data)
