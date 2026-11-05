import random
from datetime import timedelta

from django.core.exceptions import ValidationError as ModelValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventory.models import Location, Lot, Product, Reservation, StockItem, StockMovement, Warehouse
from inventory.services import close_reservation, record_movements, reserve_stock


WAREHOUSE_DATA = [
    ("DEMO-MTY", "Monterrey", "Av. Miguel Alemán 100, Apodaca, Nuevo León, 66600"),
    ("DEMO-GDL", "Guadalajara", "Av. Vallarta 2500, Guadalajara, Jalisco, 44130"),
    ("DEMO-CDMX", "Ciudad de México", "Av. Insurgentes Sur 1600, Ciudad de México, 03900"),
    ("DEMO-QRO", "Querétaro", "Av. 5 de Febrero 1300, Querétaro, 76120"),
    ("DEMO-MER", "Mérida", "Calle 60 400, Mérida, Yucatán, 97000"),
]

PRODUCT_DATA = [
    ("DEMO-EMB-001", "Caja de cartón mediana", "Pieza", 20),
    ("DEMO-EMB-002", "Caja de cartón grande", "Pieza", 15),
    ("DEMO-CIN-001", "Cinta de empaque transparente", "Rollo", 12),
    ("DEMO-CIN-002", "Cinta de empaque reforzada", "Rollo", 8),
    ("DEMO-PRO-001", "Película stretch", "Rollo", 10),
    ("DEMO-PRO-002", "Película burbuja", "Rollo", 8),
    ("DEMO-ETQ-001", "Etiqueta térmica", "Paquete", 15),
    ("DEMO-ETQ-002", "Etiqueta de envío", "Paquete", 10),
    ("DEMO-BOL-001", "Bolsa de polietileno", "Paquete", 20),
    ("DEMO-GUA-001", "Guantes de protección", "Par", 12),
    ("DEMO-LIM-001", "Limpiador multiusos", "Litro", 6),
    ("DEMO-HER-001", "Cúter de seguridad", "Pieza", 5),
]

LOCATION_DATA = [
    ("REC-01", Location.Type.RECEIVING),
    ("ALM-01", Location.Type.STORAGE),
    ("SUR-01", Location.Type.PICKING),
    ("EMB-01", Location.Type.SHIPPING),
]

TRACKED_SKUS = {"DEMO-EMB-002", "DEMO-CIN-001", "DEMO-LIM-001"}
LOT_NUMBERS = ("DEMO-SEED-001", "DEMO-SEED-002", "DEMO-SEED-003")


class Command(BaseCommand):
    help = "Create reproducible, Mexico-based demo inventory data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--warehouses",
            type=int,
            default=3,
            help=f"Number of demo warehouses to create (1-{len(WAREHOUSE_DATA)}).",
        )
        parser.add_argument(
            "--products",
            type=int,
            default=8,
            help=f"Number of demo products to create (1-{len(PRODUCT_DATA)}).",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=42,
            help="Random seed used for reproducible opening quantities.",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete data created by this command before seeding.",
        )

    def handle(self, *args, **options):
        warehouse_count = options["warehouses"]
        product_count = options["products"]
        if not 1 <= warehouse_count <= len(WAREHOUSE_DATA):
            raise CommandError(f"--warehouses must be between 1 and {len(WAREHOUSE_DATA)}.")
        if not 1 <= product_count <= len(PRODUCT_DATA):
            raise CommandError(f"--products must be between 1 and {len(PRODUCT_DATA)}.")

        rng = random.Random(options["seed"])
        self.occurred_at = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(days=7)
        try:
            with transaction.atomic():
                cleared = self._clear_demo_data() if options["clear"] else None
                warehouses = [self._get_warehouse(data) for data in WAREHOUSE_DATA[:warehouse_count]]
                products = []
                lots = {}
                for data in PRODUCT_DATA[:product_count]:
                    product = self._get_product(data)
                    products.append(product)
                    lots[product.pk] = self._get_lots(product)
                if any(not item.is_active for item in warehouses + products):
                    raise CommandError("Demo warehouses and products must be active before seeding.")
                warehouse_locations = {}
                for warehouse in warehouses:
                    locations = self._get_locations(warehouse)
                    warehouse_locations[warehouse.pk] = locations
                    if any(not location.is_active for location in locations.values()):
                        raise CommandError("Demo locations must be active before seeding.")
                    for product in products:
                        for lot in lots[product.pk]:
                            for location in locations.values():
                                StockItem.objects.get_or_create(product=product, location=location, lot=lot)
                for warehouse in warehouses:
                    locations = warehouse_locations[warehouse.pk]
                    lines = []
                    for product in products:
                        for lot in lots[product.pk]:
                            lines.extend(self._movement_lines(product, warehouse, locations, rng, lot))
                    self._ensure_movements(lines)
                self._seed_reservations(products, warehouses[0], lots)
                if len(warehouses) > 1:
                    self._seed_cross_warehouse(products[-1], warehouses, lots[products[-1].pk][0])
                    self._seed_local_shortage(products[0], warehouses[1])
                    if len(products) > 1:
                        self._seed_global_shortage(products[-1], warehouses)
        except (ModelValidationError, ValidationError, IntegrityError) as exc:
            raise CommandError(f"Demo data could not be seeded; all changes were rolled back: {exc}") from exc

        if cleared:
            self.stdout.write(cleared)
        self.stdout.write(
            self.style.SUCCESS(
                f"Demo data ready: {len(warehouses)} warehouses, "
                f"{len(warehouses) * len(LOCATION_DATA)} locations, "
                f"and {len(products)} products "
                f"({sum(product.lot_tracked for product in products)} with lot tracking)."
            )
        )
        legacy = [product.sku for product in products if product.sku in TRACKED_SKUS and not product.lot_tracked]
        if legacy:
            self.stdout.write(
                "Existing tracking modes preserved for: " + ", ".join(legacy) +
                ". Use --clear only if their demo history can safely be replaced."
            )

    def _clear_demo_data(self):
        warehouse_codes = [code for code, _, _ in WAREHOUSE_DATA]
        product_skus = [sku for sku, _, _, _ in PRODUCT_DATA]
        warehouse_ids = list(Warehouse.objects.filter(code__in=warehouse_codes).values_list("pk", flat=True))
        product_ids = list(Product.objects.filter(sku__in=product_skus).values_list("pk", flat=True))
        location_codes = [code for code, _ in LOCATION_DATA]
        location_ids = list(Location.objects.filter(
            warehouse_id__in=warehouse_ids, code__in=location_codes
        ).values_list("pk", flat=True))

        movements = StockMovement.objects.filter(
            Q(product_id__in=product_ids)
            | Q(from_location_id__in=location_ids)
            | Q(to_location_id__in=location_ids)
        )
        if movements.exclude(reference__startswith="DEMO-SEED:").exists():
            raise CommandError(
                "Cannot clear demo products or locations that have non-demo stock movements. "
                "Remove or reassign those movements first."
            )
        if movements.exclude(product_id__in=product_ids).exists() or movements.filter(
            (Q(from_location__isnull=False) & ~Q(from_location_id__in=location_ids))
            | (Q(to_location__isnull=False) & ~Q(to_location_id__in=location_ids))
        ).exists():
            raise CommandError("Cannot clear demo movements linked to non-demo inventory.")
        if Lot.objects.filter(product_id__in=product_ids).exclude(lot_number__in=LOT_NUMBERS).exists():
            raise CommandError("Cannot clear demo products referenced by lots.")
        has_extra_locations = Location.objects.filter(warehouse_id__in=warehouse_ids).exclude(
            code__in=location_codes
        ).exists()
        if has_extra_locations:
            raise CommandError(
                "Cannot clear demo warehouses that contain locations not created by this command."
            )

        reservations = Reservation.objects.filter(
            Q(stock_item__product_id__in=product_ids) | Q(stock_item__location_id__in=location_ids)
        )
        if reservations.exclude(reference__startswith="DEMO-SEED:").exists():
            raise CommandError("Cannot clear demo inventory referenced by reservations.")
        if reservations.filter(movement__isnull=False).exclude(
            movement_id__in=movements.values("pk")
        ).exists():
            raise CommandError("Cannot clear reservations linked to non-demo movements.")
        if Reservation.objects.filter(movement_id__in=movements.values("pk")).exclude(
            pk__in=reservations.values("pk")
        ).exists():
            raise CommandError("Cannot clear demo movements referenced by non-demo reservations.")
        stocks = StockItem.objects.filter(
            Q(product_id__in=product_ids) | Q(location_id__in=location_ids)
        )
        if stocks.exclude(product_id__in=product_ids, location_id__in=location_ids).exists():
            raise CommandError("Cannot clear demo stock linked to non-demo products or locations.")

        reservations_deleted, _ = reservations.delete()
        movements_deleted, _ = movements.delete()
        stock_items_deleted, _ = stocks.delete()
        lots_deleted, _ = Lot.objects.filter(product_id__in=product_ids).delete()
        locations_deleted, _ = Location.objects.filter(pk__in=location_ids).delete()
        warehouses_deleted, _ = Warehouse.objects.filter(code__in=warehouse_codes).delete()
        products_deleted, _ = Product.objects.filter(sku__in=product_skus).delete()

        return (
            "Cleared demo data: "
            f"{warehouses_deleted} warehouses, {locations_deleted} locations, "
            f"{products_deleted} products, {movements_deleted} movements, "
            f"{stock_items_deleted} stock records, {lots_deleted} lots, "
            f"{reservations_deleted} reservations."
        )

    @staticmethod
    def _get_warehouse(data):
        code, name, address = data
        warehouse, _ = Warehouse.objects.get_or_create(
            code=code,
            defaults={"name": f"{name} - Almacén de prueba", "address": address},
        )
        return warehouse

    @staticmethod
    def _get_product(data):
        sku, name, unit, reorder_level = data
        product, _ = Product.objects.get_or_create(
            sku=sku,
            defaults={
                "name": name,
                "description": "Artículo de demostración para pruebas de inventario.",
                "unit": unit,
                "reorder_level": reorder_level,
                "lot_tracked": sku in TRACKED_SKUS,
                "barcode": f"000100{[row[0] for row in PRODUCT_DATA].index(sku) + 1:04}",
            },
        )
        return product

    @staticmethod
    def _get_locations(warehouse):
        return {
            code: Location.objects.get_or_create(
                warehouse=warehouse,
                code=code,
                defaults={
                    "location_type": location_type,
                    "barcode": f"000200{[row[0] for row in WAREHOUSE_DATA].index(warehouse.code) + 1:02}"
                               f"{index + 1:02}",
                },
            )[0]
            for index, (code, location_type) in enumerate(LOCATION_DATA)
        }

    def _get_lots(self, product):
        if not product.lot_tracked:
            return [None]
        today = timezone.localdate()
        return [
            Lot.objects.get_or_create(
                product=product, lot_number=number, defaults={"expiry_date": expiry},
            )[0]
            for number, expiry in zip(
                LOT_NUMBERS, (today + timedelta(days=180), today + timedelta(days=365), today - timedelta(days=1)),
            )
        ]

    def _ensure_movements(self, lines):
        pending = []
        for line in lines:
            matches = StockMovement.objects.filter(reference=line["reference"])
            if matches.count() > 1:
                raise CommandError(f"Duplicate demo movement reference: {line['reference']}.")
            existing = matches.first()
            if existing is None:
                pending.append(line)
                continue
            for field in ("product", "lot", "from_location", "to_location"):
                expected = line.get(field)
                if getattr(existing, f"{field}_id") != (expected.pk if expected else None):
                    raise CommandError(f"Conflicting demo movement: {line['reference']}.")
            if existing.movement_type != line["movement_type"]:
                raise CommandError(f"Conflicting demo movement: {line['reference']}.")
        if pending:
            record_movements(pending)

    def _movement_lines(self, product, warehouse, locations, rng, lot):
        reference_prefix = self._reference_prefix(product, warehouse, lot)
        receiving = locations["REC-01"]
        storage = locations["ALM-01"]
        picking = locations["SUR-01"]
        shipping = locations["EMB-01"]

        inbound = StockMovement.objects.filter(reference=f"{reference_prefix}:IN").first()
        quantity = rng.randint(40, 100)
        if inbound is not None:
            quantity = inbound.quantity
        storage_quantity = max(1, quantity // 2)
        picking_quantity = max(1, storage_quantity // 2)
        common = {"product": product, "lot": lot}
        return [
            dict(
                common, reference=f"{reference_prefix}:IN",
                movement_type=StockMovement.Type.INBOUND, to_location=receiving, quantity=quantity,
                occurred_at=self.occurred_at, notes="Existencias iniciales de demostración.",
            ),
            dict(
                common, reference=f"{reference_prefix}:TO-STORAGE",
                movement_type=StockMovement.Type.TRANSFER, from_location=receiving, to_location=storage,
                quantity=storage_quantity, occurred_at=self.occurred_at + timedelta(days=1),
                notes="Acomodo de demostración en almacenamiento.",
            ),
            dict(
                common, reference=f"{reference_prefix}:TO-PICKING",
                movement_type=StockMovement.Type.TRANSFER, from_location=storage, to_location=picking,
                quantity=picking_quantity, occurred_at=self.occurred_at + timedelta(days=2),
                notes="Reposición de demostración para preparación de pedidos.",
            ),
            dict(
                common, reference=f"{reference_prefix}:OUT",
                movement_type=StockMovement.Type.OUTBOUND, from_location=picking,
                quantity=max(1, picking_quantity // 2), occurred_at=self.occurred_at + timedelta(days=3),
                notes="Salida de demostración.",
            ),
            dict(
                common, reference=f"{reference_prefix}:ADJUST",
                movement_type=StockMovement.Type.ADJUSTMENT, to_location=shipping,
                quantity=2, occurred_at=self.occurred_at + timedelta(days=4),
                notes="Ajuste positivo de demostración.",
            ),
        ]

    @staticmethod
    def _reference_prefix(product, warehouse, lot=None):
        prefix = f"DEMO-SEED:{product.sku}:{warehouse.code}"
        return f"{prefix}:{lot.lot_number}" if lot else prefix

    def _seed_reservations(self, products, warehouse, lots):
        for product in products:
            for lot in lots[product.pk]:
                prefix = self._reference_prefix(product, warehouse, lot)
                stock = StockItem.objects.get(
                    product=product, lot=lot, location__warehouse=warehouse, location__code="ALM-01",
                )
                for status in Reservation.Status:
                    reference = f"{prefix}:RES-{status.value.upper()}"
                    if self._existing_reservation(reference, stock, 1):
                        continue
                    reservation = reserve_stock(stock_item=stock, quantity=1, reference=reference)
                    if status != Reservation.Status.ACTIVE:
                        close_reservation(reservation, fulfill=status == Reservation.Status.FULFILLED)
                reference = f"{prefix}:RES-FULL"
                shipping = StockItem.objects.get(
                    product=product, lot=lot, location__warehouse=warehouse, location__code="EMB-01",
                )
                if not self._existing_reservation(reference, shipping, 2):
                    reserve_stock(stock_item=shipping, quantity=2, reference=reference)

    @staticmethod
    def _existing_reservation(reference, stock, quantity):
        matches = Reservation.objects.filter(reference=reference)
        if matches.count() > 1:
            raise CommandError(f"Duplicate demo reservation reference: {reference}.")
        existing = matches.first()
        if existing and (existing.stock_item_id != stock.pk or existing.quantity != quantity):
            raise CommandError(f"Conflicting demo reservation: {reference}.")
        return existing

    def _seed_cross_warehouse(self, product, warehouses, lot):
        source, target = warehouses[:2]
        self._ensure_movements([{
            "reference": f"{self._reference_prefix(product, source, lot)}:CROSS",
            "product": product, "lot": lot, "movement_type": StockMovement.Type.TRANSFER,
            "from_location": Location.objects.get(warehouse=source, code="REC-01"),
            "to_location": Location.objects.get(warehouse=target, code="REC-01"),
            "quantity": 2, "occurred_at": self.occurred_at + timedelta(days=5),
            "notes": "Transferencia entre almacenes de demostración.",
        }])

    def _seed_local_shortage(self, product, warehouse):
        lines = []
        for stock in StockItem.objects.filter(product=product, location__warehouse=warehouse).select_related(
            "location", "lot",
        ):
            reference = f"{self._reference_prefix(product, warehouse, stock.lot)}:SHORT:{stock.location.code}"
            if StockMovement.objects.filter(reference=reference).exists() or stock.quantity == 0:
                continue
            lines.append({
                "reference": reference, "product": product, "lot": stock.lot,
                "movement_type": StockMovement.Type.OUTBOUND, "from_location": stock.location,
                "quantity": stock.quantity, "occurred_at": self.occurred_at + timedelta(days=6),
                "notes": "Agotamiento local de demostración; otro almacén conserva existencias.",
            })
        self._ensure_movements(lines)

    def _seed_global_shortage(self, product, warehouses):
        stocks = list(StockItem.objects.filter(
            product=product, location__warehouse__in=warehouses,
        ).select_related("location__warehouse", "lot").order_by("pk"))
        excess = sum(stock.quantity for stock in stocks) - max(0, product.reorder_level - 1)
        lines = []
        for stock in stocks:
            reference = (
                f"{self._reference_prefix(product, stock.location.warehouse, stock.lot)}"
                f":GLOBAL-SHORT:{stock.location.code}"
            )
            quantity = min(max(0, excess), stock.available_quantity)
            if not quantity or StockMovement.objects.filter(reference=reference).exists():
                continue
            lines.append({
                "reference": reference, "product": product, "lot": stock.lot,
                "movement_type": StockMovement.Type.ADJUSTMENT, "from_location": stock.location,
                "quantity": quantity, "occurred_at": self.occurred_at + timedelta(days=6),
                "notes": "Ajuste negativo de demostración para alerta de reorden global.",
            })
            excess -= quantity
        self._ensure_movements(lines)
