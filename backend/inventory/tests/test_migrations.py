from datetime import datetime, timezone

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class MovementOccurredAtMigrationTests(TransactionTestCase):
    def test_existing_movements_keep_their_original_timestamps(self):
        before = [("inventory", "0003_stockmovement_product_created_index")]
        after = [("inventory", "0004_stockmovement_occurred_at")]
        executor = MigrationExecutor(connection)
        self.addCleanup(self.restore_schema)
        executor.migrate(before)
        apps = executor.loader.project_state(before).apps
        warehouse = apps.get_model("inventory", "Warehouse").objects.create(code="OLD", name="Old")
        location = apps.get_model("inventory", "Location").objects.create(warehouse=warehouse, code="A")
        product = apps.get_model("inventory", "Product").objects.create(sku="OLD", name="Old")
        movement_model = apps.get_model("inventory", "StockMovement")
        original = {}
        for year in (2023, 2025):
            movement = movement_model.objects.create(
                movement_type="inbound", product=product, to_location=location, quantity=5,
            )
            timestamp = datetime(year, 1, 2, 12, 30, tzinfo=timezone.utc)
            movement_model.objects.filter(pk=movement.pk).update(created_at=timestamp)
            original[movement.pk] = timestamp

        executor = MigrationExecutor(connection)
        executor.migrate(after)
        apps = executor.loader.project_state(after).apps
        movements = apps.get_model("inventory", "StockMovement").objects.all()
        self.assertEqual(movements.count(), 2)
        for movement in movements:
            self.assertEqual(movement.occurred_at, original[movement.pk])
            self.assertEqual(movement.created_at, original[movement.pk])

    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


class BarcodeLotMigrationTests(TransactionTestCase):
    def test_existing_stock_movements_and_reservations_remain_untracked(self):
        before = [("inventory", "0005_reservation")]
        after = [("inventory", "0006_barcodes_and_lots")]
        self.addCleanup(self.restore_schema)
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        apps = executor.loader.project_state(before).apps
        warehouse = apps.get_model("inventory", "Warehouse").objects.create(code="OLD", name="Old")
        location = apps.get_model("inventory", "Location").objects.create(warehouse=warehouse, code="A")
        product = apps.get_model("inventory", "Product").objects.create(sku="OLD", name="Old")
        stock = apps.get_model("inventory", "StockItem").objects.create(
            product=product, location=location, quantity=10,
        )
        movement = apps.get_model("inventory", "StockMovement").objects.create(
            product=product, to_location=location, movement_type="inbound", quantity=10,
        )
        reservation = apps.get_model("inventory", "Reservation").objects.create(
            stock_item=stock, quantity=4, reference="EXISTING",
        )
        original_time = movement.created_at
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        apps = executor.loader.project_state(after).apps
        new_product = apps.get_model("inventory", "Product").objects.get(pk=product.pk)
        new_stock = apps.get_model("inventory", "StockItem").objects.get(pk=stock.pk)
        new_movement = apps.get_model("inventory", "StockMovement").objects.get(pk=movement.pk)
        new_reservation = apps.get_model("inventory", "Reservation").objects.get(pk=reservation.pk)
        self.assertFalse(new_product.lot_tracked)
        self.assertIsNone(new_product.barcode)
        self.assertIsNone(apps.get_model("inventory", "Location").objects.get(pk=location.pk).barcode)
        self.assertIsNone(new_stock.lot_id)
        self.assertEqual(new_stock.quantity, 10)
        self.assertIsNone(new_movement.lot_id)
        self.assertEqual(new_movement.created_at, original_time)
        self.assertEqual(new_reservation.stock_item_id, stock.pk)
        self.assertEqual(new_reservation.quantity, 4)
        self.assertEqual(new_reservation.status, "active")

    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


class LotNumberMigrationTests(TransactionTestCase):
    def test_existing_lot_numbers_and_inventory_links_are_preserved(self):
        before = [("inventory", "0006_barcodes_and_lots")]
        after = [("inventory", "0007_lot_number")]
        self.addCleanup(self.restore_schema)
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        apps = executor.loader.project_state(before).apps
        warehouse = apps.get_model("inventory", "Warehouse").objects.create(code="OLD", name="Old")
        location = apps.get_model("inventory", "Location").objects.create(warehouse=warehouse, code="A")
        product = apps.get_model("inventory", "Product").objects.create(
            sku="OLD", name="Old", lot_tracked=True,
        )
        lot_model = apps.get_model("inventory", "Lot")
        lot = lot_model.objects.create(
            product=product, code="000123", batch="REMOVED", expiry_date="2027-01-31",
        )
        no_expiry = lot_model.objects.create(product=product, code="NO-EXPIRY")
        stock = apps.get_model("inventory", "StockItem").objects.create(
            product=product, location=location, lot=lot, quantity=10,
        )
        movement = apps.get_model("inventory", "StockMovement").objects.create(
            product=product, to_location=location, lot=lot, movement_type="inbound", quantity=10,
        )
        reservation = apps.get_model("inventory", "Reservation").objects.create(
            stock_item=stock, quantity=4, reference="EXISTING",
        )
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        apps = executor.loader.project_state(after).apps
        lot_model = apps.get_model("inventory", "Lot")
        renamed = lot_model.objects.get(pk=lot.pk)
        self.assertEqual(renamed.lot_number, "000123")
        self.assertEqual(renamed.product_id, product.pk)
        self.assertEqual(renamed.expiry_date.isoformat(), "2027-01-31")
        self.assertEqual(renamed.created_at, lot.created_at)
        self.assertFalse(hasattr(renamed, "batch"))
        self.assertFalse(hasattr(renamed, "code"))
        self.assertIsNone(lot_model.objects.get(pk=no_expiry.pk).expiry_date)
        new_stock = apps.get_model("inventory", "StockItem").objects.get(pk=stock.pk)
        self.assertEqual(new_stock.lot_id, lot.pk)
        self.assertEqual(new_stock.quantity, 10)
        new_movement = apps.get_model("inventory", "StockMovement").objects.get(pk=movement.pk)
        self.assertEqual(new_movement.lot_id, lot.pk)
        self.assertEqual(new_movement.quantity, 10)
        self.assertEqual(new_movement.created_at, movement.created_at)
        new_reservation = apps.get_model("inventory", "Reservation").objects.get(pk=reservation.pk)
        self.assertEqual(new_reservation.stock_item_id, stock.pk)
        self.assertEqual(new_reservation.quantity, 4)
        with self.assertRaises(IntegrityError), transaction.atomic():
            lot_model.objects.create(product_id=product.pk, lot_number="000123")

    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
