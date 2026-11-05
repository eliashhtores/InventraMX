from django import forms
from django.contrib import admin, messages
from django.utils.translation import gettext as _
from rest_framework.exceptions import ValidationError

from inventory.models import Location, Lot, Product, Reservation, StockItem, StockMovement, Warehouse
from inventory.services import close_reservation


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name", "address")


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("code", "barcode", "warehouse", "location_type", "is_active")
    list_filter = ("location_type", "is_active")
    search_fields = ("code", "barcode", "warehouse__code", "warehouse__name")
    list_select_related = ("warehouse",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("sku", "barcode", "name", "lot_tracked", "unit", "reorder_level", "is_active")
    list_filter = ("is_active", "lot_tracked")
    search_fields = ("sku", "barcode", "name", "description")


class LotAdminForm(forms.ModelForm):
    class Meta:
        model = Lot
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "expiry_date" in self.fields:
            self.fields["expiry_date"].required = self.instance._state.adding or self.instance.expiry_date is not None


@admin.register(Lot)
class LotAdmin(admin.ModelAdmin):
    form = LotAdminForm
    list_display = ("product", "lot_number", "expiry_date")
    list_select_related = ("product",)
    search_fields = ("product__sku", "lot_number")
    list_filter = ("expiry_date",)

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and (obj.stock_items.exists() or obj.movements.exists()):
            return ("product", "lot_number", "expiry_date")
        return ("product",) if obj is not None else ()


@admin.register(StockItem)
class StockItemAdmin(admin.ModelAdmin):
    list_display = ("product", "location", "lot", "quantity", "reserved_quantity", "available_quantity", "updated_at")
    list_select_related = ("product", "location", "lot")
    search_fields = ("product__sku", "product__name", "location__code", "lot__lot_number")

    def get_queryset(self, request):
        return super().get_queryset(request).with_balances()

    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = (
        "occurred_at", "created_at", "movement_type", "product", "lot", "quantity", "from_location", "to_location",
    )
    list_filter = ("movement_type",)
    list_select_related = ("product", "lot", "from_location", "to_location")
    search_fields = ("product__sku", "product__name", "reference", "lot__lot_number")

    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ("reference", "stock_item", "quantity", "status", "movement", "created_at")
    list_filter = ("status",)
    list_select_related = ("stock_item__product", "stock_item__lot", "stock_item__location__warehouse", "movement")
    search_fields = ("reference", "stock_item__product__sku", "stock_item__lot__lot_number")
    actions = ("release_selected", "fulfill_selected")

    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def _close_selected(self, request, queryset, *, fulfill):
        for reservation in queryset.order_by("stock_item_id", "pk"):
            try:
                close_reservation(reservation, fulfill=fulfill)
            except ValidationError as exc:
                self.message_user(
                    request, _("Reserva %(id)s: %(error)s") % {
                        "id": reservation.pk, "error": exc.detail,
                    }, level=messages.ERROR,
                )
            else:
                self.message_user(
                    request, _("Reserva %(id)s actualizada.") % {"id": reservation.pk}, level=messages.SUCCESS,
                )

    def has_manage_permission(self, request):
        return request.user.has_perm("inventory.change_reservation")

    @admin.action(description=_("Liberar las reservas seleccionadas"), permissions=["manage"])
    def release_selected(self, request, queryset):
        self._close_selected(request, queryset, fulfill=False)

    @admin.action(description=_("Surtir las reservas seleccionadas"), permissions=["manage"])
    def fulfill_selected(self, request, queryset):
        self._close_selected(request, queryset, fulfill=True)
