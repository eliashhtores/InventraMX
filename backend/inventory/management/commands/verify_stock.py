from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Sum

from inventory.models import Reservation, StockItem, StockMovement


class Command(BaseCommand):
    help = "Verify stock quantities against the movement ledger, optionally rebuilding them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Repair mismatched quantities and create missing stock rows from the ledger.",
        )

    def handle(self, *args, **options):
        with transaction.atomic():
            # Block writers, including inserts for missing stock rows, until reconciliation commits.
            tables = ", ".join(
                connection.ops.quote_name(model._meta.db_table)
                for model in (StockItem, StockMovement, Reservation)
            )
            with connection.cursor() as cursor:
                cursor.execute(f"LOCK TABLE {tables} IN EXCLUSIVE MODE")

            expected = {}
            for field, sign in (("to_location_id", 1), ("from_location_id", -1)):
                totals = (
                    StockMovement.objects.filter(**{f"{field}__isnull": False})
                    .order_by()
                    .values("product_id", field, "lot_id")
                    .annotate(total=Sum("quantity"))
                )
                for row in totals:
                    key = (row["product_id"], row[field], row["lot_id"])
                    expected[key] = expected.get(key, 0) + sign * row["total"]

            actual = {
                (product_id, location_id, lot_id): quantity
                for product_id, location_id, lot_id, quantity in StockItem.objects.values_list(
                    "product_id", "location_id", "lot_id", "quantity"
                )
            }
            keys = sorted(
                expected.keys() | actual.keys(), key=lambda key: (key[0], key[1], key[2] or 0)
            )
            mismatches = []
            for product_id, location_id, lot_id in keys:
                key = (product_id, location_id, lot_id)
                quantity = expected.get(key, 0)
                if key not in actual or actual[key] != quantity:
                    mismatches.append((product_id, location_id, lot_id, quantity))
                    lot_label = f" lot_id={lot_id}" if lot_id is not None else ""
                    self.stdout.write(
                        f"product_id={product_id} location_id={location_id}: "
                        f"stored={actual.get(key, 'missing')} expected={quantity}{lot_label}"
                    )

            if any(quantity < 0 for quantity in expected.values()):
                raise CommandError(
                    "Ledger contains negative stock totals; no stock was changed. "
                    "Investigate the ledger before rebuilding."
                )
            reserved = (
                Reservation.objects.filter(status=Reservation.Status.ACTIVE)
                .order_by().values("stock_item__product_id", "stock_item__location_id", "stock_item__lot_id")
                .annotate(total=Sum("quantity"))
            )
            over_reserved = False
            for row in reserved:
                key = (
                    row["stock_item__product_id"], row["stock_item__location_id"], row["stock_item__lot_id"],
                )
                if row["total"] > expected.get(key, 0):
                    over_reserved = True
                    self.stdout.write(
                        f"product_id={key[0]} location_id={key[1]}: "
                        f"reserved={row['total']} exceeds ledger={expected.get(key, 0)} lot_id={key[2]}"
                    )
            if over_reserved:
                raise CommandError(
                    "Active reservations exceed ledger stock; no stock was changed. "
                    "Release excess reservations before rebuilding."
                )
            if mismatches and not options["rebuild"]:
                raise CommandError(
                    f"Found {len(mismatches)} stock mismatch(es); no stock was changed. "
                    "Run with --rebuild to repair."
                )
            for product_id, location_id, lot_id, quantity in mismatches:
                StockItem.objects.update_or_create(
                    product_id=product_id,
                    location_id=location_id,
                    lot_id=lot_id,
                    defaults={"quantity": quantity},
                )

        if mismatches:
            self.stdout.write(self.style.SUCCESS(f"Rebuilt {len(mismatches)} stock item(s)."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Stock verified: {len(keys)} product/location pair(s); no mismatches."
                    " Balances are checked separately per lot."
                )
            )
