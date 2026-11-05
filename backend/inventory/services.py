from django.core.exceptions import ValidationError as ModelValidationError
from django.db import transaction
from django.utils.translation import gettext as _
from rest_framework.exceptions import ValidationError

from .models import Lot, Product, Reservation, StockItem, StockMovement


def _lock_stock(movements):
    """Ensure stock rows exist and lock them in a deterministic order to avoid deadlocks."""
    products = {
        product.pk: product
        for product in Product.objects.select_for_update(no_key=True)
        .filter(pk__in={movement.product_id for movement in movements}).order_by("pk")
    }
    lots = {
        lot.pk: lot for lot in Lot.objects.filter(
            pk__in={movement.lot_id for movement in movements if movement.lot_id is not None}
        )
    }
    for index, movement in enumerate(movements):
        try:
            if movement.product_id not in products:
                raise ModelValidationError({"product": _("El producto ya no existe.")})
            movement.product = products[movement.product_id]
            if movement.lot_id is not None:
                if movement.lot_id not in lots:
                    raise ModelValidationError({"lot": _("El número de lote ya no existe.")})
                movement.lot = lots[movement.lot_id]
            movement.clean()
        except ModelValidationError as exc:
            errors = [{} for _ in movements]
            errors[index] = exc.message_dict
            raise ValidationError(errors) from exc
    pairs = {
        (movement.product_id, location_id, movement.lot_id)
        for movement in movements
        for location_id in (movement.from_location_id, movement.to_location_id)
        if location_id is not None
    }
    pks = [
        StockItem.objects.get_or_create(product_id=product_id, location_id=location_id, lot_id=lot_id)[0].pk
        for product_id, location_id, lot_id in sorted(
            pairs, key=lambda pair: (pair[0], pair[1], pair[2] or 0)
        )
    ]
    rows = StockItem.objects.select_for_update().filter(pk__in=pks).order_by("pk")
    return {(row.product_id, row.location_id, row.lot_id): row for row in rows}


def _apply_movement(movement, stock, *, reserved_quantity=0):
    if movement.from_location_id:
        source = stock[(movement.product_id, movement.from_location_id, movement.lot_id)]
        available = source.available_quantity + reserved_quantity
        if available < movement.quantity:
            raise ValidationError(
                {"quantity": _("Existencias insuficientes en %(location)s: %(available)s disponibles.") % {
                    "location": movement.from_location, "available": available,
                }}
            )
        source.quantity -= movement.quantity
        source.save(update_fields=["quantity", "updated_at"])
    if movement.to_location_id:
        target = stock[(movement.product_id, movement.to_location_id, movement.lot_id)]
        target.quantity += movement.quantity
        target.save(update_fields=["quantity", "updated_at"])
    movement.save()
    return movement


@transaction.atomic
def _record_movements(lines, *, indexed_errors):
    if not lines:
        raise ValidationError({"non_field_errors": [_("Se requiere al menos un movimiento.")]})
    movements = [StockMovement(**data) for data in lines]
    try:
        stock = _lock_stock(movements)
    except ValidationError as exc:
        if not indexed_errors and isinstance(exc.detail, list):
            raise ValidationError(exc.detail[0]) from exc
        raise
    for index, movement in enumerate(movements):
        try:
            _apply_movement(movement, stock)
        except ValidationError as exc:
            if not indexed_errors:
                raise
            errors = [{} for _ in movements]
            errors[index] = exc.detail
            raise ValidationError(errors) from exc
    return movements


def record_movement(**data):
    """Create a StockMovement and apply it to stock levels atomically."""
    return _record_movements([data], indexed_errors=False)[0]


def record_movements(lines):
    """Lock all affected stock rows once and apply the ordered lines in one transaction."""
    return _record_movements(lines, indexed_errors=True)


def _validate_reservable_stock(stock):
    errors = {}
    if not stock.product.is_active:
        errors["stock_item"] = _("El producto está inactivo.")
    elif not stock.location.is_active:
        errors["stock_item"] = _("La ubicación está inactiva.")
    elif not stock.location.warehouse.is_active:
        errors["stock_item"] = _("El almacén está inactivo.")
    if errors:
        raise ValidationError(errors)
    try:
        stock.clean()
    except ModelValidationError as exc:
        raise ValidationError(exc.message_dict) from exc


@transaction.atomic
def reserve_stock(*, stock_item, quantity, reference):
    stock = StockItem.objects.select_for_update().get(pk=stock_item.pk)
    _validate_reservable_stock(stock)
    reservation = Reservation(stock_item=stock, quantity=quantity, reference=reference)
    try:
        reservation.full_clean()
    except ModelValidationError as exc:
        raise ValidationError(exc.message_dict) from exc
    available = stock.available_quantity
    if quantity > available:
        raise ValidationError({
            "quantity": _("Existencias disponibles insuficientes: %(available)s disponibles.") % {
                "available": available,
            },
        })
    reservation.save()
    return reservation


@transaction.atomic
def close_reservation(reservation, *, fulfill):
    # Stock always locks before reservations, matching ordinary movement writers.
    stock = StockItem.objects.select_for_update().get(pk=reservation.stock_item_id)
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status != Reservation.Status.ACTIVE:
        raise ValidationError({"status": _("Solo se pueden liberar o surtir reservas activas.")})
    if fulfill:
        _validate_reservable_stock(stock)
        movement = StockMovement(
            movement_type=StockMovement.Type.OUTBOUND,
            product=stock.product,
            lot=stock.lot,
            from_location=stock.location,
            quantity=reservation.quantity,
            reference=reservation.reference,
        )
        _apply_movement(
            movement, {(stock.product_id, stock.location_id, stock.lot_id): stock},
            reserved_quantity=reservation.quantity,
        )
        reservation.movement = movement
        reservation.status = Reservation.Status.FULFILLED
    else:
        reservation.status = Reservation.Status.RELEASED
    reservation.save(update_fields=["status", "movement", "updated_at"])
    return reservation
