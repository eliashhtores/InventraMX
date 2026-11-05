from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Event
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection, connections, transaction
from django.db.models import F, Sum
from django.db.models.signals import pre_save
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventory.management.commands.seed_demo_data import PRODUCT_DATA
from inventory.models import Location, Lot, Product, Reservation, StockItem, StockMovement, Warehouse
from inventory.services import close_reservation, record_movement, record_movements, reserve_stock


class SeedDemoDataCommandTests(TestCase):
    def test_seed_creates_mexico_demo_inventory_and_is_idempotent(self):
        call_command("seed_demo_data", warehouses=2, products=3, seed=7, stdout=StringIO())

        self.assertEqual(Warehouse.objects.count(), 2)
        self.assertEqual(Location.objects.count(), 8)
        self.assertEqual(Product.objects.count(), 3)
        movement_count = StockMovement.objects.count()
        self.assertGreater(movement_count, 82)
        self.assertEqual(StockItem.objects.count(), 56)
        self.assertEqual(Lot.objects.count(), 6)
        self.assertEqual(Reservation.objects.count(), 28)

        quantities = list(StockItem.objects.order_by("pk").values_list("quantity", flat=True))
        references = list(StockMovement.objects.order_by("pk").values_list("reference", flat=True))
        lot_metadata = list(Lot.objects.order_by("pk").values())
        reservation_history = list(Reservation.objects.order_by("pk").values())
        call_command("seed_demo_data", warehouses=2, products=3, seed=99, stdout=StringIO())

        self.assertEqual(Warehouse.objects.count(), 2)
        self.assertEqual(Location.objects.count(), 8)
        self.assertEqual(Product.objects.count(), 3)
        self.assertEqual(StockMovement.objects.count(), movement_count)
        self.assertEqual(Lot.objects.count(), 6)
        self.assertEqual(Reservation.objects.count(), 28)
        self.assertEqual(
            list(StockItem.objects.order_by("pk").values_list("quantity", flat=True)),
            quantities,
        )
        self.assertEqual(
            list(StockMovement.objects.order_by("pk").values_list("reference", flat=True)),
            references,
        )
        self.assertEqual(list(Lot.objects.order_by("pk").values()), lot_metadata)
        self.assertEqual(list(Reservation.objects.order_by("pk").values()), reservation_history)

    def test_invalid_counts_fail_without_creating_data(self):
        with self.assertRaises(CommandError):
            call_command("seed_demo_data", warehouses=0, stdout=StringIO())

        self.assertEqual(Warehouse.objects.count(), 0)
        self.assertEqual(Product.objects.count(), 0)

    def test_clear_recreates_demo_data_and_preserves_unrelated_inventory(self):
        call_command("seed_demo_data", warehouses=2, products=3, seed=7, stdout=StringIO())
        old_demo_movement_ids = set(
            StockMovement.objects.filter(reference__startswith="DEMO-SEED:").values_list("pk", flat=True)
        )

        warehouse = Warehouse.objects.create(code="CUSTOM", name="Custom warehouse")
        location = Location.objects.create(warehouse=warehouse, code="A-01")
        product = Product.objects.create(sku="CUSTOM-1", name="Custom product")
        unrelated_movement = record_movement(
            movement_type=StockMovement.Type.INBOUND,
            product=product,
            to_location=location,
            quantity=9,
            reference="CUSTOM-REF",
        )

        output = StringIO()
        call_command("seed_demo_data", warehouses=2, products=3, seed=7, clear=True, stdout=output)

        new_demo_movement_ids = set(
            StockMovement.objects.filter(reference__startswith="DEMO-SEED:").values_list("pk", flat=True)
        )
        self.assertTrue(old_demo_movement_ids.isdisjoint(new_demo_movement_ids))
        self.assertEqual(len(new_demo_movement_ids), len(old_demo_movement_ids))
        self.assertEqual(Warehouse.objects.filter(code__startswith="DEMO-").count(), 2)
        self.assertEqual(Location.objects.filter(warehouse__code__startswith="DEMO-").count(), 8)
        self.assertEqual(Product.objects.filter(sku__startswith="DEMO-").count(), 3)
        self.assertEqual(StockMovement.objects.get(pk=unrelated_movement.pk).quantity, 9)
        self.assertEqual(StockItem.objects.get(product=product, location=location).quantity, 9)
        self.assertIn("Cleared demo data:", output.getvalue())

    def test_clear_refuses_to_delete_demo_records_used_by_non_demo_movements(self):
        call_command("seed_demo_data", warehouses=1, products=1, seed=7, stdout=StringIO())
        product = Product.objects.get(sku="DEMO-EMB-001")
        location = Location.objects.get(warehouse__code="DEMO-MTY", code="REC-01")
        unrelated_movement = record_movement(
            movement_type=StockMovement.Type.INBOUND,
            product=product,
            to_location=location,
            quantity=1,
            reference="CUSTOM-REF",
        )

        with self.assertRaises(CommandError):
            call_command("seed_demo_data", warehouses=1, products=1, clear=True, stdout=StringIO())

        self.assertTrue(StockMovement.objects.filter(pk=unrelated_movement.pk).exists())
        self.assertEqual(Warehouse.objects.filter(code="DEMO-MTY").count(), 1)

    def test_seed_covers_barcodes_lots_dates_balances_and_summary_alerts(self):
        with patch(
            "inventory.management.commands.seed_demo_data.record_movements", wraps=record_movements,
        ) as batches:
            call_command("seed_demo_data", warehouses=2, products=3, seed=7, stdout=StringIO())
        self.assertTrue(any(len(call.args[0]) > 5 for call in batches.call_args_list))
        self.assertTrue(all(product.barcode.startswith("000") for product in Product.objects.all()))
        self.assertTrue(all(location.barcode.startswith("000") for location in Location.objects.all()))
        self.assertEqual(Product.objects.filter(lot_tracked=True).count(), 2)
        self.assertTrue(StockItem.objects.filter(lot__isnull=True).exists())
        self.assertTrue(StockItem.objects.filter(lot__isnull=False).exists())
        self.assertEqual(Lot.objects.filter(expiry_date__isnull=True).count(), 0)
        self.assertEqual(Lot.objects.filter(expiry_date__lt=timezone.localdate()).count(), 2)
        self.assertEqual(Lot.objects.filter(expiry_date__gt=timezone.localdate()).count(), 4)
        self.assertFalse(StockItem.objects.filter(lot__isnull=False).exclude(product_id=F("lot__product_id")).exists())
        self.assertEqual(
            set(StockMovement.objects.values_list("movement_type", flat=True)),
            set(StockMovement.Type.values),
        )
        self.assertTrue(StockMovement.objects.filter(
            movement_type="transfer", reference__endswith=":CROSS",
        ).exclude(from_location__warehouse_id=F("to_location__warehouse_id")).exists())
        self.assertTrue(StockMovement.objects.filter(
            occurred_at__lt=F("created_at") - timedelta(days=6),
        ).exists())
        self.assertFalse(StockMovement.objects.filter(created_at__lt=timezone.now() - timedelta(minutes=5)).exists())
        self.assertEqual(set(Reservation.objects.values_list("status", flat=True)), set(Reservation.Status.values))
        fulfilled = Reservation.objects.filter(status="fulfilled").select_related("movement", "stock_item")
        for reservation in fulfilled:
            self.assertEqual(reservation.movement.lot_id, reservation.stock_item.lot_id)
            self.assertEqual(reservation.movement.product_id, reservation.stock_item.product_id)
            self.assertEqual(reservation.movement.from_location_id, reservation.stock_item.location_id)
        for stock in StockItem.objects.with_balances():
            self.assertGreaterEqual(stock.available_quantity, 0)
            self.assertEqual(stock.quantity, stock.reserved_quantity + stock.available_quantity)
            if stock.reservations.filter(reference__endswith=":RES-FULL").exists():
                self.assertEqual(stock.quantity, 2)
                self.assertEqual(stock.available_quantity, 0)
        report = self.client.get("/api/reports/summary/").json()
        self.assertEqual(report["low_stock_count"], 1)
        self.assertEqual(report["low_stock"][0]["sku"], "DEMO-CIN-001")
        local = next(row for row in report["warehouse_breakdown"] if row["code"] == "DEMO-GDL")
        self.assertIn(
            {"sku": "DEMO-EMB-001", "total_quantity": 0},
            [{"sku": row["sku"], "total_quantity": row["total_quantity"]} for row in local["low_stock"]],
        )
        global_quantity = StockItem.objects.filter(product__sku="DEMO-EMB-001").aggregate(total=Sum("quantity"))["total"]
        self.assertGreater(global_quantity, Product.objects.get(sku="DEMO-EMB-001").reorder_level)
        output = StringIO()
        call_command("verify_stock", stdout=output)
        self.assertIn("no mismatches", output.getvalue())

    def test_default_and_maximum_sizes_reconcile(self):
        for counts in ({}, {"warehouses": 5, "products": 12}):
            with self.subTest(counts=counts):
                call_command("seed_demo_data", clear=True, stdout=StringIO(), **counts)
                output = StringIO()
                call_command("verify_stock", stdout=output)
                self.assertIn("no mismatches", output.getvalue())

    def test_existing_untracked_products_are_not_converted_or_overwritten(self):
        sku, name, unit, reorder_level = PRODUCT_DATA[1]
        product = Product.objects.create(
            sku=sku, name="Custom demo name", unit=unit, reorder_level=reorder_level,
        )
        warehouse = Warehouse.objects.create(code="DEMO-MTY", name="Old warehouse")
        receiving = Location.objects.create(warehouse=warehouse, code="REC-01")
        movement = record_movement(
            movement_type="inbound", product=product, to_location=receiving, quantity=50,
            reference=f"DEMO-SEED:{sku}:DEMO-MTY:IN",
        )
        output = StringIO()
        call_command("seed_demo_data", warehouses=1, products=2, stdout=output)
        product.refresh_from_db()
        self.assertFalse(product.lot_tracked)
        self.assertEqual(product.name, "Custom demo name")
        self.assertEqual(StockMovement.objects.get(pk=movement.pk).quantity, 50)
        self.assertFalse(Lot.objects.filter(product=product).exists())
        self.assertIn("Existing tracking modes preserved", output.getvalue())
        call_command("verify_stock", stdout=StringIO())

    def test_rerun_preserves_closed_demo_holds(self):
        call_command("seed_demo_data", warehouses=1, products=2, stdout=StringIO())
        hold = Reservation.objects.filter(status="active", reference__endswith=":RES-ACTIVE").first()
        close_reservation(hold, fulfill=False)
        movements = StockMovement.objects.count()
        reservations = Reservation.objects.count()
        call_command("seed_demo_data", warehouses=1, products=2, seed=99, stdout=StringIO())
        hold.refresh_from_db()
        self.assertEqual(hold.status, "released")
        self.assertEqual(StockMovement.objects.count(), movements)
        self.assertEqual(Reservation.objects.count(), reservations)
        call_command("verify_stock", stdout=StringIO())

    def test_service_failure_rolls_back_whole_seed_including_clear(self):
        call_command("seed_demo_data", warehouses=1, products=2, stdout=StringIO())
        movements = list(StockMovement.objects.order_by("pk").values())
        reservations = list(Reservation.objects.order_by("pk").values())
        lots = list(Lot.objects.order_by("pk").values())
        with patch(
            "inventory.management.commands.seed_demo_data.reserve_stock",
            side_effect=ValidationError({"quantity": "forced failure"}),
        ):
            with self.assertRaisesMessage(CommandError, "all changes were rolled back"):
                call_command("seed_demo_data", warehouses=2, products=3, clear=True, stdout=StringIO())
        self.assertEqual(list(StockMovement.objects.order_by("pk").values()), movements)
        self.assertEqual(list(Reservation.objects.order_by("pk").values()), reservations)
        self.assertEqual(list(Lot.objects.order_by("pk").values()), lots)
        self.assertEqual(Warehouse.objects.count(), 1)

    def test_clear_preserves_unrelated_data_even_with_demo_prefix(self):
        call_command("seed_demo_data", warehouses=1, products=2, stdout=StringIO())
        warehouse = Warehouse.objects.create(code="CUSTOM", name="Custom")
        location = Location.objects.create(warehouse=warehouse, code="CUSTOM")
        product = Product.objects.create(sku="CUSTOM", name="Custom", lot_tracked=True)
        lot = Lot.objects.create(product=product, lot_number="DEMO-SEED-001", expiry_date="2027-12-31")
        movement = record_movement(
            product=product, lot=lot, movement_type="inbound", to_location=location,
            quantity=9, reference="DEMO-SEED:unrelated",
        )
        call_command("seed_demo_data", warehouses=1, products=2, clear=True, stdout=StringIO())
        self.assertTrue(StockMovement.objects.filter(pk=movement.pk).exists())
        self.assertTrue(Lot.objects.filter(pk=lot.pk).exists())
        self.assertEqual(StockItem.objects.get(product=product).quantity, 9)

    def test_clear_refuses_user_lots_and_shared_inventory(self):
        call_command("seed_demo_data", warehouses=1, products=2, stdout=StringIO())
        product = Product.objects.get(sku="DEMO-EMB-002")
        lot = Lot.objects.create(product=product, lot_number="USER-LOT", expiry_date="2027-12-31")
        with self.assertRaisesMessage(CommandError, "referenced by lots"):
            call_command("seed_demo_data", warehouses=1, products=2, clear=True, stdout=StringIO())
        lot.delete()
        warehouse = Warehouse.objects.create(code="CUSTOM", name="Custom")
        location = Location.objects.create(warehouse=warehouse, code="CUSTOM")
        StockItem.objects.create(product=Product.objects.get(sku="DEMO-EMB-001"), location=location)
        with self.assertRaisesMessage(CommandError, "non-demo products or locations"):
            call_command("seed_demo_data", warehouses=1, products=2, clear=True, stdout=StringIO())

    def test_conflicting_reference_fails_without_partial_writes(self):
        product = Product.objects.create(sku="CUSTOM", name="Custom")
        warehouse = Warehouse.objects.create(code="CUSTOM", name="Custom")
        location = Location.objects.create(warehouse=warehouse, code="CUSTOM")
        record_movement(
            product=product, to_location=location, movement_type="inbound", quantity=5,
            reference="DEMO-SEED:DEMO-EMB-001:DEMO-MTY:IN",
        )
        with self.assertRaisesMessage(CommandError, "Conflicting demo movement"):
            call_command("seed_demo_data", warehouses=2, products=3, stdout=StringIO())
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_expanding_counts_preserves_existing_history(self):
        call_command("seed_demo_data", warehouses=1, products=2, stdout=StringIO())
        original = list(StockMovement.objects.order_by("pk").values())
        call_command("seed_demo_data", warehouses=2, products=3, stdout=StringIO())
        call_command("seed_demo_data", warehouses=3, products=3, seed=99, stdout=StringIO())
        self.assertEqual(
            list(StockMovement.objects.filter(pk__in=[row["id"] for row in original]).order_by("pk").values()),
            original,
        )
        call_command("verify_stock", stdout=StringIO())

    def test_conflicting_reservation_reference_rolls_back_seed(self):
        warehouse = Warehouse.objects.create(code="CUSTOM", name="Custom")
        product = Product.objects.create(sku="CUSTOM", name="Custom")
        location = Location.objects.create(warehouse=warehouse, code="CUSTOM")
        record_movement(product=product, to_location=location, movement_type="inbound", quantity=5)
        stock = StockItem.objects.get(product=product)
        reserve_stock(
            stock_item=stock, quantity=1, reference="DEMO-SEED:DEMO-EMB-001:DEMO-MTY:RES-ACTIVE",
        )
        with self.assertRaisesMessage(CommandError, "Conflicting demo reservation"):
            call_command("seed_demo_data", warehouses=1, products=1, stdout=StringIO())
        self.assertEqual(Warehouse.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_duplicate_demo_movement_reference_is_rejected(self):
        call_command("seed_demo_data", warehouses=1, products=1, stdout=StringIO())
        record_movement(
            product=Product.objects.get(), to_location=Location.objects.get(code="REC-01"),
            movement_type="inbound", quantity=1,
            reference="DEMO-SEED:DEMO-EMB-001:DEMO-MTY:IN",
        )
        with self.assertRaisesMessage(CommandError, "Duplicate demo movement reference"):
            call_command("seed_demo_data", warehouses=1, products=1, stdout=StringIO())

    def test_products_lots_and_all_empty_inventory_exist_before_first_movement(self):
        phases = []

        def check_order(sender, instance, **kwargs):
            if not instance._state.adding:
                return
            if sender is Lot:
                self.assertTrue(Product.objects.filter(pk=instance.product_id).exists())
                self.assertIsNotNone(instance.expiry_date)
                self.assertFalse(StockItem.objects.exists())
                self.assertFalse(StockMovement.objects.exists())
                phases.append("lot")
            elif sender is StockItem:
                self.assertEqual(Lot.objects.count(), 6)
                self.assertFalse(StockMovement.objects.exists())
                if instance.product.lot_tracked:
                    self.assertTrue(Lot.objects.filter(pk=instance.lot_id, product_id=instance.product_id).exists())
                phases.append("stock")
            elif sender is StockMovement:
                self.assertEqual(Product.objects.count(), 3)
                self.assertEqual(Lot.objects.count(), 6)
                self.assertEqual(StockItem.objects.count(), 56)
                if "movement" not in phases:
                    # The service applies the first inbound before saving its ledger row.
                    self.assertEqual(StockItem.objects.exclude(quantity=0).count(), 1)
                phases.append("movement")

        for model in (Lot, StockItem, StockMovement):
            pre_save.connect(check_order, sender=model)
        try:
            call_command("seed_demo_data", warehouses=2, products=3, stdout=StringIO())
        finally:
            for model in (Lot, StockItem, StockMovement):
                pre_save.disconnect(check_order, sender=model)
        self.assertLess(phases.index("lot"), phases.index("stock"))
        self.assertLess(phases.index("stock"), phases.index("movement"))

    def test_rerun_preserves_legacy_demo_lots_without_expiry(self):
        call_command("seed_demo_data", warehouses=1, products=2, stdout=StringIO())
        lot = Lot.objects.first()
        Lot.objects.filter(pk=lot.pk).update(expiry_date=None)
        call_command("seed_demo_data", warehouses=1, products=2, stdout=StringIO())
        lot.refresh_from_db()
        self.assertIsNone(lot.expiry_date)
        call_command("seed_demo_data", warehouses=1, products=2, clear=True, stdout=StringIO())
        self.assertFalse(Lot.objects.filter(expiry_date__isnull=True).exists())


class VerifyStockCommandTests(TestCase):
    def setUp(self):
        warehouse = Warehouse.objects.create(code="VERIFY", name="Verify warehouse")
        other_warehouse = Warehouse.objects.create(code="OTHER", name="Other warehouse")
        self.source = Location.objects.create(warehouse=warehouse, code="A-01")
        self.target = Location.objects.create(warehouse=other_warehouse, code="A-01")
        self.product = Product.objects.create(sku="VERIFY-1", name="Verify product")

    def record(self, movement_type, quantity, **locations):
        return record_movement(
            movement_type=movement_type,
            product=self.product,
            quantity=quantity,
            **locations,
        )

    def test_empty_inventory_passes(self):
        output = StringIO()
        call_command("verify_stock", stdout=output)
        self.assertIn("0 product/location pair(s); no mismatches", output.getvalue())

    def test_all_movement_types_and_cross_warehouse_transfer_reconcile(self):
        self.record(StockMovement.Type.INBOUND, 20, to_location=self.source)
        self.record(StockMovement.Type.OUTBOUND, 3, from_location=self.source)
        self.record(
            StockMovement.Type.TRANSFER, 5,
            from_location=self.source, to_location=self.target,
        )
        self.record(StockMovement.Type.ADJUSTMENT, 2, to_location=self.source)
        self.record(StockMovement.Type.ADJUSTMENT, 1, from_location=self.target)
        output = StringIO()

        call_command("verify_stock", stdout=output)

        self.assertIn("2 product/location pair(s); no mismatches", output.getvalue())
        self.assertEqual(StockItem.objects.get(location=self.source).quantity, 14)
        self.assertEqual(StockItem.objects.get(location=self.target).quantity, 4)

    def test_verification_reports_all_mismatches_without_writing(self):
        self.record(StockMovement.Type.INBOUND, 20, to_location=self.source)
        StockItem.objects.filter(location=self.source).update(quantity=99)
        StockItem.objects.create(product=self.product, location=self.target, quantity=7)
        before = list(StockItem.objects.order_by("pk").values())
        output = StringIO()

        with self.assertRaisesMessage(CommandError, "Found 2 stock mismatch(es)"):
            call_command("verify_stock", stdout=output)

        self.assertIn(
            f"product_id={self.product.pk} location_id={self.source.pk}: stored=99 expected=20",
            output.getvalue(),
        )
        self.assertIn(
            f"product_id={self.product.pk} location_id={self.target.pk}: stored=7 expected=0",
            output.getvalue(),
        )
        self.assertEqual(list(StockItem.objects.order_by("pk").values()), before)

    def test_rebuild_repairs_quantities_without_changing_ledger_and_is_idempotent(self):
        self.record(StockMovement.Type.INBOUND, 20, to_location=self.source)
        StockItem.objects.filter(location=self.source).update(quantity=99)
        StockItem.objects.create(product=self.product, location=self.target, quantity=7)
        ledger = list(StockMovement.objects.order_by("pk").values())
        output = StringIO()

        call_command("verify_stock", rebuild=True, stdout=output)

        self.assertIn("Rebuilt 2 stock item(s)", output.getvalue())
        self.assertEqual(StockItem.objects.get(location=self.source).quantity, 20)
        self.assertEqual(StockItem.objects.get(location=self.target).quantity, 0)
        repaired = list(StockItem.objects.order_by("pk").values())
        call_command("verify_stock", rebuild=True, stdout=StringIO())
        call_command("verify_stock", stdout=StringIO())
        self.assertEqual(list(StockItem.objects.order_by("pk").values()), repaired)
        self.assertEqual(list(StockMovement.objects.order_by("pk").values()), ledger)

    def test_missing_stock_rows_are_reported_and_recreated(self):
        self.record(StockMovement.Type.INBOUND, 20, to_location=self.source)
        self.record(
            StockMovement.Type.TRANSFER, 5,
            from_location=self.source, to_location=self.target,
        )
        StockItem.objects.all().delete()
        output = StringIO()

        with self.assertRaisesMessage(CommandError, "Found 2 stock mismatch(es)"):
            call_command("verify_stock", stdout=output)

        self.assertIn("stored=missing expected=15", output.getvalue())
        self.assertIn("stored=missing expected=5", output.getvalue())
        self.assertEqual(StockItem.objects.count(), 0)
        call_command("verify_stock", rebuild=True, stdout=StringIO())
        self.assertEqual(StockItem.objects.get(location=self.source).quantity, 15)
        self.assertEqual(StockItem.objects.get(location=self.target).quantity, 5)

    def test_zero_balance_and_no_movement_rows_are_handled(self):
        self.record(StockMovement.Type.INBOUND, 5, to_location=self.source)
        self.record(StockMovement.Type.OUTBOUND, 5, from_location=self.source)
        StockItem.objects.create(product=self.product, location=self.target, quantity=0)
        call_command("verify_stock", stdout=StringIO())
        StockItem.objects.filter(location=self.source).delete()

        with self.assertRaisesMessage(CommandError, "Found 1 stock mismatch(es)"):
            call_command("verify_stock", stdout=StringIO())

        call_command("verify_stock", rebuild=True, stdout=StringIO())
        self.assertEqual(StockItem.objects.get(location=self.source).quantity, 0)
        self.assertEqual(StockItem.objects.count(), 2)

    def test_products_at_same_location_are_reconciled_separately(self):
        self.record(StockMovement.Type.INBOUND, 5, to_location=self.source)
        other_product = Product.objects.create(sku="VERIFY-2", name="Other product")
        record_movement(
            movement_type=StockMovement.Type.INBOUND,
            product=other_product,
            to_location=self.source,
            quantity=9,
        )
        StockItem.objects.update(quantity=1)

        call_command("verify_stock", rebuild=True, stdout=StringIO())

        self.assertEqual(StockItem.objects.get(product=self.product).quantity, 5)
        self.assertEqual(StockItem.objects.get(product=other_product).quantity, 9)

    def test_negative_ledger_totals_refuse_any_repair(self):
        self.record(StockMovement.Type.INBOUND, 5, to_location=self.source)
        StockItem.objects.filter(location=self.source).update(quantity=99)
        StockMovement.objects.create(
            movement_type=StockMovement.Type.OUTBOUND,
            product=self.product,
            from_location=self.target,
            quantity=1,
        )
        before = list(StockItem.objects.order_by("pk").values())
        ledger = list(StockMovement.objects.order_by("pk").values())

        for rebuild in (False, True):
            with self.subTest(rebuild=rebuild):
                with self.assertRaisesMessage(CommandError, "Ledger contains negative stock totals"):
                    call_command("verify_stock", rebuild=rebuild, stdout=StringIO())
                self.assertEqual(list(StockItem.objects.order_by("pk").values()), before)
                self.assertEqual(list(StockMovement.objects.order_by("pk").values()), ledger)


class VerifyStockConcurrencyTests(TransactionTestCase):
    def test_rebuild_blocks_stock_and_ledger_writes_until_committed(self):
        warehouse = Warehouse.objects.create(code="VERIFY", name="Verify warehouse")
        location = Location.objects.create(warehouse=warehouse, code="A-01")
        product = Product.objects.create(sku="VERIFY-1", name="Verify product")
        record_movement(
            movement_type=StockMovement.Type.INBOUND,
            product=product,
            to_location=location,
            quantity=5,
        )
        StockItem.objects.update(quantity=99)
        locked = Event()
        release = Event()
        output = StringIO()
        write = output.write

        def pause_at_mismatch(message):
            if "stored=99 expected=5" in message:
                locked.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("Timed out waiting to finish stock reconciliation.")
            return write(message)

        def rebuild():
            try:
                call_command("verify_stock", rebuild=True, stdout=output)
            finally:
                connections.close_all()

        with patch.object(output, "write", side_effect=pause_at_mismatch):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(rebuild)
                try:
                    self.assertTrue(locked.wait(timeout=10), "Rebuild did not reach its locked snapshot.")
                    for model in (StockItem, StockMovement, Reservation):
                        with self.subTest(model=model.__name__):
                            with self.assertRaisesMessage(OperationalError, "lock timeout"):
                                with transaction.atomic():
                                    with connection.cursor() as cursor:
                                        cursor.execute("SET LOCAL lock_timeout = '100ms'")
                                    if model is StockItem:
                                        StockItem.objects.filter(product=product).update(quantity=8)
                                    elif model is StockMovement:
                                        StockMovement.objects.create(
                                            movement_type=StockMovement.Type.INBOUND,
                                            product=product,
                                            to_location=location,
                                            quantity=3,
                                        )
                                    else:
                                        Reservation.objects.create(
                                            stock_item=StockItem.objects.get(product=product),
                                            quantity=1,
                                            reference="LOCK-TEST",
                                        )
                finally:
                    release.set()
                future.result(timeout=10)

        self.assertEqual(StockItem.objects.get(product=product).quantity, 5)
        record_movement(
            movement_type=StockMovement.Type.INBOUND,
            product=product,
            to_location=location,
            quantity=3,
        )
        self.assertEqual(StockItem.objects.get(product=product).quantity, 8)
        call_command("verify_stock", stdout=StringIO())
