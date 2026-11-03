from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import StockItem, StockMovement


def _lock_stock(product, locations):
    """Ensure stock rows exist and lock them in a deterministic order to avoid deadlocks."""
    locations = sorted({location.pk: location for location in locations}.values(), key=lambda loc: loc.pk)
    pks = [StockItem.objects.get_or_create(product=product, location=location)[0].pk for location in locations]
    rows = StockItem.objects.select_for_update().filter(pk__in=pks).order_by("pk")
    return {row.location_id: row for row in rows}


@transaction.atomic
def record_movement(**data):
    """Create a StockMovement and apply it to stock levels atomically."""
    movement = StockMovement(**data)
    involved = [loc for loc in (movement.from_location, movement.to_location) if loc is not None]
    stock = _lock_stock(movement.product, involved)

    if movement.from_location_id:
        source = stock[movement.from_location_id]
        if source.quantity < movement.quantity:
            raise ValidationError(
                {"quantity": f"Insufficient stock at {movement.from_location}: {source.quantity} available."}
            )
        source.quantity -= movement.quantity
        source.save(update_fields=["quantity", "updated_at"])
    if movement.to_location_id:
        target = stock[movement.to_location_id]
        target.quantity += movement.quantity
        target.save(update_fields=["quantity", "updated_at"])
    movement.save()
    return movement
