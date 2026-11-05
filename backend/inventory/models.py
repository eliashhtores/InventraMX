from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def validate_product_lot(product, lot):
    if lot is not None and lot.product_id != product.pk:
        raise ValidationError({"lot": _("El número de lote debe pertenecer al producto.")})
    if product.lot_tracked and lot is None:
        raise ValidationError({"lot": _("Se requiere un número de lote para un producto con control por lote.")})
    if not product.lot_tracked and lot is not None:
        raise ValidationError({"lot": _("Los productos sin control por lote no deben indicar un número de lote.")})


class Warehouse(models.Model):
    code = models.CharField(_("código"), max_length=20, unique=True)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]
        verbose_name = _("almacén")
        verbose_name_plural = _("almacenes")

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
    barcode = models.CharField(_("código de barras"), max_length=100, unique=True, null=True, blank=True)
    location_type = models.CharField(max_length=20, choices=Type.choices, default=Type.STORAGE)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["warehouse__code", "code"]
        verbose_name = _("ubicación")
        verbose_name_plural = _("ubicaciones")
        constraints = [
            models.UniqueConstraint(fields=["warehouse", "code"], name="unique_location_code_per_warehouse"),
        ]

    def __str__(self):
        return f"{self.warehouse.code}/{self.code}"

    def save(self, *args, **kwargs):
        self.barcode = self.barcode or None
        return super().save(*args, **kwargs)


class Product(models.Model):
    sku = models.CharField(_("SKU"), max_length=64, unique=True)
    barcode = models.CharField(_("código de barras"), max_length=100, unique=True, null=True, blank=True)
    lot_tracked = models.BooleanField(default=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    unit = models.CharField(max_length=20, default="pz")
    reorder_level = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sku"]
        verbose_name = _("producto")
        verbose_name_plural = _("productos")

    def __str__(self):
        return f"{self.sku} - {self.name}"

    def clean(self):
        super().clean()
        if self.pk:
            previous = Product.objects.get(pk=self.pk)
            if previous.lot_tracked != self.lot_tracked and (
                self.movements.exists()
                or self.stock_items.filter(reservations__isnull=False).exists()
                or self.stock_items.filter(quantity__gt=0).exists()
                or self.lots.exists()
            ):
                raise ValidationError(
                    {"lot_tracked": _(
                        "No se puede cambiar el control por lote cuando existen "
                        "existencias, números de lote, movimientos o reservas."
                    )}
                )

    @transaction.atomic
    def save(self, *args, **kwargs):
        if self.pk:
            Product.objects.select_for_update(no_key=True).get(pk=self.pk)
        self.clean()
        self.barcode = self.barcode or None
        return super().save(*args, **kwargs)


class Lot(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="lots")
    lot_number = models.CharField(max_length=100)
    expiry_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["product__sku", "lot_number"]
        verbose_name = _("número de lote")
        verbose_name_plural = _("números de lote")
        constraints = [
            models.UniqueConstraint(fields=["product", "lot_number"], name="unique_lot_per_product"),
        ]

    def clean(self):
        super().clean()
        previous = Lot.objects.filter(pk=self.pk).first() if self.pk else None
        if self.expiry_date is None and (previous is None or previous.expiry_date is not None):
            raise ValidationError({
                "expiry_date": _("La fecha de caducidad es obligatoria para los números de lote nuevos."),
            })
        if not self.product_id:
            return
        if not self.product.lot_tracked:
            raise ValidationError({"product": _("Los números de lote requieren un producto con control por lote.")})
        if previous is not None:
            if previous.product_id != self.product_id:
                raise ValidationError({"product": _("No se puede cambiar el producto de un número de lote.")})
            changed = any(
                getattr(previous, field) != getattr(self, field)
                for field in ("lot_number", "expiry_date")
            )
            if changed and (self.movements.exists() or self.stock_items.exists()):
                raise ValidationError({"lot_number": _(
                    "No se pueden modificar los datos del número de lote cuando existen existencias o movimientos."
                )})

    @transaction.atomic
    def save(self, *args, **kwargs):
        Product.objects.select_for_update(no_key=True).get(pk=self.product_id)
        self.clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product.sku}/{self.lot_number}"


class StockItemQuerySet(models.QuerySet):
    def with_balances(self):
        return self.annotate(
            _reserved_quantity=Coalesce(
                models.Sum("reservations__quantity", filter=models.Q(reservations__status="active")),
                models.Value(0),
            )
        )


class StockItem(models.Model):
    """Quantity on hand of a product at a specific location."""

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_items")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="stock_items")
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="stock_items", null=True, blank=True)
    quantity = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
    objects = StockItemQuerySet.as_manager()

    @property
    def reserved_quantity(self):
        if hasattr(self, "_reserved_quantity"):
            return self._reserved_quantity
        return self.reservations.filter(status=Reservation.Status.ACTIVE).aggregate(
            total=models.Sum("quantity")
        )["total"] or 0

    @property
    def available_quantity(self):
        return self.quantity - self.reserved_quantity

    class Meta:
        ordering = ["product__sku", "location__code"]
        verbose_name = _("existencia")
        verbose_name_plural = _("existencias")
        constraints = [
            models.UniqueConstraint(
                fields=["product", "location", "lot"], nulls_distinct=False,
                name="unique_stock_per_product_location_lot",
            ),
        ]

    def __str__(self):
        lot_label = f" [{self.lot.lot_number}]" if self.lot_id else ""
        return f"{self.product.sku}{lot_label} @ {self.location}: {self.quantity}"

    def clean(self):
        super().clean()
        validate_product_lot(self.product, self.lot)


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
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="movements", null=True, blank=True)
    from_location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name="outgoing_movements"
    )
    to_location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name="incoming_movements"
    )
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("movimiento")
        verbose_name_plural = _("movimientos")
        indexes = [
            models.Index(fields=["-created_at", "-id"], name="movement_created_idx"),
            models.Index(fields=["movement_type", "-created_at"], name="movement_type_created_idx"),
            models.Index(fields=["product", "-created_at"], name="movement_product_created_idx"),
        ]

    def __str__(self):
        return f"{self.get_movement_type_display()} {self.product.sku} x{self.quantity}"

    def clean(self):
        super().clean()
        validate_product_lot(self.product, self.lot)


class Reservation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        RELEASED = "released", "Released"
        FULFILLED = "fulfilled", "Fulfilled"

    stock_item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="reservations")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    reference = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    movement = models.OneToOneField(
        StockMovement, on_delete=models.PROTECT, null=True, blank=True,
        related_name="reservation",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("reserva")
        verbose_name_plural = _("reservas")
        indexes = [
            models.Index(fields=["stock_item", "status"], name="reservation_stock_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name="reservation_quantity_positive"),
            models.CheckConstraint(
                condition=(
                    models.Q(status="fulfilled", movement__isnull=False)
                    | models.Q(status__in=["active", "released"], movement__isnull=True)
                ),
                name="reservation_status_movement",
            ),
        ]

    def __str__(self):
        return f"{self.reference}: {self.stock_item} x{self.quantity} ({self.status})"
