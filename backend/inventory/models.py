from django.core.validators import MinValueValidator
from django.db import models


class Warehouse(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class Location(models.Model):
    class Type(models.TextChoices):
        RECEIVING = "receiving", "Receiving"
        STORAGE = "storage", "Storage"
        PICKING = "picking", "Picking"
        SHIPPING = "shipping", "Shipping"

    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="locations")
    code = models.CharField(max_length=40)
    location_type = models.CharField(max_length=20, choices=Type.choices, default=Type.STORAGE)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["warehouse__code", "code"]
        constraints = [
            models.UniqueConstraint(fields=["warehouse", "code"], name="unique_location_code_per_warehouse"),
        ]

    def __str__(self):
        return f"{self.warehouse.code}/{self.code}"


class Product(models.Model):
    sku = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    unit = models.CharField(max_length=20, default="pcs")
    reorder_level = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sku"]

    def __str__(self):
        return f"{self.sku} - {self.name}"


class StockItem(models.Model):
    """Quantity on hand of a product at a specific location."""

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_items")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="stock_items")
    quantity = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product__sku", "location__code"]
        constraints = [
            models.UniqueConstraint(fields=["product", "location"], name="unique_stock_per_product_location"),
        ]

    def __str__(self):
        return f"{self.product.sku} @ {self.location}: {self.quantity}"


class StockMovement(models.Model):
    """Immutable ledger entry. Creating one updates the related StockItem rows.

    * inbound:    adds ``quantity`` to ``to_location``
    * outbound:   removes ``quantity`` from ``from_location``
    * transfer:   moves ``quantity`` from ``from_location`` to ``to_location``
    * adjustment: adds to ``to_location`` *or* removes from ``from_location``
    """

    class Type(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"
        TRANSFER = "transfer", "Transfer"
        ADJUSTMENT = "adjustment", "Adjustment"

    movement_type = models.CharField(max_length=20, choices=Type.choices)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="movements")
    from_location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name="outgoing_movements"
    )
    to_location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name="incoming_movements"
    )
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.get_movement_type_display()} {self.product.sku} x{self.quantity}"
