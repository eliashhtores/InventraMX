from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import StockItem, StockMovement


def _locked_stock(product, location):
    stock, _ = StockItem.objects.get_or_create(product=product, location=location)
    return StockItem.objects.select_for_update().get(pk=stock.pk)


@transaction.atomic
def record_movement(**data):
    """Create a StockMovement and apply it to stock levels atomically."""
    movement = StockMovement(**data)
    if movement.from_location_id:
        source = _locked_stock(movement.product, movement.from_location)
        if source.quantity < movement.quantity:
            raise ValidationError(
                {"quantity": f"Insufficient stock at {movement.from_location}: {source.quantity} available."}
            )
        source.quantity -= movement.quantity
        source.save(update_fields=["quantity", "updated_at"])
    if movement.to_location_id:
        target = _locked_stock(movement.product, movement.to_location)
        target.quantity += movement.quantity
        target.save(update_fields=["quantity", "updated_at"])
    movement.save()
    return movement
