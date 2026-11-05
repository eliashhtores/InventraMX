from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection, connections
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from inventory import services
from inventory.models import Location, Product, StockItem, StockMovement, Warehouse
from inventory.services import record_movement, record_movements

from .test_api import ApiTestCase


class BatchMovementTests(ApiTestCase):
    def post_batch(self, lines):
        return self.client.post("/api/movements/batch/", lines, format="json")

    def inbound(self, quantity=1, **overrides):
        return {
            "movement_type": "inbound",
            "product": self.product.pk,
            "to_location": self.receiving.pk,
            "quantity": quantity,
            **overrides,
        }

    def test_ten_line_receipt_commits_and_preserves_input_order(self):
        lines = [
            self.inbound(index + 1, reference=f"RECEIPT-1:{index}")
            for index in range(10)
        ]
        lines[0]["occurred_at"] = "2026-01-02T10:00:00-06:00"
        lines[0]["created_at"] = "2000-01-01T00:00:00Z"

        response = self.post_batch(lines)

        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(len(response.json()), 10)
        self.assertEqual(
            [row["reference"] for row in response.json()],
            [line["reference"] for line in lines],
        )
        self.assertEqual(self.qty(self.receiving), 55)
        self.assertEqual(StockMovement.objects.count(), 10)
        self.assertEqual(
            parse_datetime(response.json()[0]["occurred_at"]),
            parse_datetime(lines[0]["occurred_at"]),
        )
        self.assertNotEqual(
            parse_datetime(response.json()[0]["created_at"]),
            parse_datetime(lines[0]["created_at"]),
        )
        call_command("verify_stock", stdout=StringIO())

    def test_multiple_products_and_locations_and_all_types_share_running_balances(self):
        other = Product.objects.create(sku="SKU-2", name="Other")
        lines = [
            self.inbound(10),
            self.inbound(7, product=other.pk),
            {
                "movement_type": "transfer", "product": self.product.pk,
                "from_location": self.receiving.pk, "to_location": self.storage.pk, "quantity": 4,
            },
            {
                "movement_type": "outbound", "product": self.product.pk,
                "from_location": self.storage.pk, "quantity": 1,
            },
            {
                "movement_type": "adjustment", "product": self.product.pk,
                "from_location": self.receiving.pk, "quantity": 2,
            },
            {
                "movement_type": "adjustment", "product": self.product.pk,
                "to_location": self.storage.pk, "quantity": 3,
            },
        ]

        response = self.post_batch(lines)

        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(self.qty(self.receiving), 4)
        self.assertEqual(self.qty(self.storage), 6)
        self.assertEqual(StockItem.objects.get(product=other).quantity, 7)
        call_command("verify_stock", stdout=StringIO())

    def test_invalid_line_is_indexed_and_nothing_is_written(self):
        invalid_lines = [
            self.inbound(0),
            self.inbound(product=999999),
            self.inbound(movement_type="unknown"),
            self.inbound(occurred_at="not-a-date"),
            self.inbound(movement_type="transfer"),
        ]
        for invalid in invalid_lines:
            with self.subTest(line=invalid):
                response = self.post_batch([self.inbound(), invalid])
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIsInstance(response.json(), list, response.json())
                self.assertEqual(response.json()[0], {})
                self.assertTrue(response.json()[1])
                self.assertEqual(StockMovement.objects.count(), 0)
                self.assertEqual(StockItem.objects.count(), 0)

    def test_inactive_product_location_or_warehouse_rejected_before_writing(self):
        for obj in (self.product, self.receiving, self.warehouse):
            with self.subTest(model=type(obj).__name__):
                obj.is_active = False
                obj.save(update_fields=["is_active"])
                response = self.post_batch([self.inbound()])
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(StockMovement.objects.count(), 0)
                self.assertEqual(StockItem.objects.count(), 0)
                obj.is_active = True
                obj.save(update_fields=["is_active"])

    def test_multiple_line_errors_keep_their_original_indexes(self):
        response = self.post_batch([
            self.inbound(0), self.inbound(), self.inbound(occurred_at="not-a-date"),
        ])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(len(response.json()), 3)
        self.assertIn("quantity", response.json()[0])
        self.assertEqual(response.json()[1], {})
        self.assertIn("occurred_at", response.json()[2])
        self.assertEqual(StockItem.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_stock_failure_rolls_back_ledger_new_rows_and_existing_quantities(self):
        self.move(movement_type="inbound", to_location=self.receiving.pk, quantity=5)
        before_stock = list(StockItem.objects.values())
        before_ledger = list(StockMovement.objects.values())
        response = self.post_batch([
            self.inbound(3),
            self.inbound(2, to_location=self.storage.pk),
            {
                "movement_type": "outbound", "product": self.product.pk,
                "from_location": self.receiving.pk, "quantity": 9,
            },
        ])

        self.assertEqual(response.status_code, 400, response.json())
        self.assertEqual(response.json()[:2], [{}, {}])
        self.assertIn("quantity", response.json()[2])
        self.assertEqual(list(StockItem.objects.values()), before_stock)
        self.assertEqual(list(StockMovement.objects.values()), before_ledger)

    def test_later_receipt_does_not_fund_earlier_outbound(self):
        response = self.post_batch([
            {
                "movement_type": "outbound", "product": self.product.pk,
                "from_location": self.receiving.pk, "quantity": 1,
            },
            self.inbound(),
        ])
        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn("quantity", response.json()[0])
        self.assertEqual(StockItem.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_empty_and_non_list_payloads_rejected(self):
        for payload in ([], {}, {"movements": [self.inbound()]}, None, "invalid"):
            with self.subTest(payload=payload):
                response = self.post_batch(payload)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("non_field_errors", response.json())
        self.assertEqual(StockMovement.objects.count(), 0)
        self.assertEqual(StockItem.objects.count(), 0)

    def test_database_failure_after_first_line_rolls_back_everything(self):
        original_save = StockMovement.save

        def fail_second(movement, *args, **kwargs):
            if movement.reference == "FAIL":
                raise RuntimeError("Simulated storage failure")
            return original_save(movement, *args, **kwargs)

        with patch.object(StockMovement, "save", new=fail_second):
            with self.assertRaisesMessage(RuntimeError, "Simulated storage failure"):
                self.post_batch([self.inbound(), self.inbound(reference="FAIL")])
        self.assertEqual(StockMovement.objects.count(), 0)
        self.assertEqual(StockItem.objects.count(), 0)

    def test_all_rows_locked_once_in_pk_order_before_any_updates(self):
        # Reverse creation order so primary-key order differs from product/location order.
        storage_stock = StockItem.objects.create(product=self.product, location=self.storage)
        receiving_stock = StockItem.objects.create(product=self.product, location=self.receiving)
        self.assertLess(storage_stock.pk, receiving_stock.pk)
        with CaptureQueriesContext(connection) as captured:
            response = self.post_batch([
                self.inbound(),
                self.inbound(to_location=self.storage.pk),
                self.inbound(),
            ])
        self.assertEqual(response.status_code, 201, response.json())
        queries = [query["sql"] for query in captured]
        locks = [query for query in queries if "FOR UPDATE" in query]
        self.assertEqual(len(locks), 1)
        self.assertIn('ORDER BY "inventory_stockitem"."id" ASC', locks[0])
        self.assertIn(str(storage_stock.pk), locks[0])
        self.assertIn(str(receiving_stock.pk), locks[0])
        first_write = next(
            index for index, query in enumerate(queries)
            if query.startswith(('UPDATE "inventory_stockitem"', 'INSERT INTO "inventory_stockmovement"'))
        )
        self.assertLess(queries.index(locks[0]), first_write)

    def test_service_rejects_empty_batch(self):
        with self.assertRaisesMessage(ValidationError, "Se requiere al menos un movimiento"):
            record_movements([])


class BatchMovementConcurrencyTests(TransactionTestCase):
    def setUp(self):
        warehouse = Warehouse.objects.create(code="BATCH", name="Batch")
        self.location = Location.objects.create(warehouse=warehouse, code="A")
        self.products = [
            Product.objects.create(sku=f"BATCH-{index}", name=f"Product {index}")
            for index in range(2)
        ]

    def concurrent_requests(self, payloads, endpoints):
        barrier = Barrier(2)
        original_lock = services._lock_stock

        def synchronized_lock(movements):
            barrier.wait(timeout=5)
            return original_lock(movements)

        def post(endpoint, payload):
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '3s'")
                    cursor.execute("SET statement_timeout = '5s'")
                return APIClient().post(endpoint, payload, format="json")
            finally:
                connections.close_all()

        with patch("inventory.services._lock_stock", side_effect=synchronized_lock):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(post, endpoint, payload)
                    for endpoint, payload in zip(endpoints, payloads)
                ]
                return [future.result(timeout=10) for future in futures]

    def test_overlapping_reversed_batches_do_not_deadlock_or_lose_updates(self):
        lines = [
            {
                "movement_type": "inbound", "product": product.pk,
                "to_location": self.location.pk, "quantity": 1,
            }
            for product in self.products
        ]
        for existing_rows in (False, True):
            with self.subTest(existing_rows=existing_rows):
                # The first run exercises simultaneous creation; the second reuses those rows.
                responses = self.concurrent_requests(
                    [lines, list(reversed(lines))], ["/api/movements/batch/"] * 2,
                )
                self.assertEqual([response.status_code for response in responses], [201, 201])
                expected = 4 if existing_rows else 2
                self.assertEqual(StockItem.objects.count(), 2)
                self.assertEqual(
                    list(StockItem.objects.values_list("quantity", flat=True)), [expected, expected],
                )
        self.assertEqual(StockMovement.objects.count(), 8)
        call_command("verify_stock", stdout=StringIO())

    def test_batch_and_single_outbound_cannot_spend_the_same_stock(self):
        product = self.products[0]
        record_movement(
            movement_type="inbound", product=product, to_location=self.location, quantity=5,
        )
        outbound = {
            "movement_type": "outbound", "product": product.pk,
            "from_location": self.location.pk, "quantity": 4,
        }
        responses = self.concurrent_requests(
            [[outbound], outbound], ["/api/movements/batch/", "/api/movements/"],
        )
        self.assertEqual(sorted(response.status_code for response in responses), [201, 400])
        self.assertEqual(StockItem.objects.get(product=product).quantity, 1)
        self.assertEqual(StockMovement.objects.count(), 2)
        call_command("verify_stock", stdout=StringIO())
