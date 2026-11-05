from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, connections, transaction
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from inventory.models import Location, Product, Reservation, StockItem, StockMovement, Warehouse
from inventory.services import close_reservation, record_movement, reserve_stock

from .test_api import ApiTestCase


class ReservationTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.move(movement_type="inbound", to_location=self.receiving.pk, quantity=10)
        self.stock = StockItem.objects.get(product=self.product, location=self.receiving)

    def reserve(self, quantity=4, **overrides):
        return self.client.post(
            "/api/reservations/",
            {"stock_item": self.stock.pk, "quantity": quantity, "reference": "ORDER-1", **overrides},
            format="json",
        )

    def action(self, reservation, action):
        return self.client.post(f"/api/reservations/{reservation}/{action}/", {}, format="json")

    def assert_balance(self, quantity, reserved, available):
        response = self.client.get(f"/api/stock/{self.stock.pk}/")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(
            [response.json()[key] for key in ("quantity", "reserved_quantity", "available_quantity")],
            [quantity, reserved, available],
        )

    def test_create_reservation_changes_availability_not_on_hand_or_ledger(self):
        response = self.reserve()
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["product_sku"], self.product.sku)
        self.assertIsNone(data["movement"])
        self.assert_balance(10, 4, 6)
        self.assertEqual(self.stock.reserved_quantity, 4)
        self.assertEqual(self.stock.available_quantity, 6)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_release_frees_availability_and_preserves_audit_history(self):
        reservation = self.reserve().json()["id"]
        response = self.action(reservation, "release")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["status"], "released")
        self.assertIsNone(response.json()["movement"])
        self.assert_balance(10, 0, 10)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(self.action(reservation, "release").status_code, 400)
        self.assertEqual(self.action(reservation, "fulfill").status_code, 400)

    def test_fulfill_records_one_outbound_and_keeps_other_reservations(self):
        reservation = self.reserve(4).json()["id"]
        self.reserve(3, reference="ORDER-2")
        response = self.action(reservation, "fulfill")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["status"], "fulfilled")
        movement = StockMovement.objects.get(pk=response.json()["movement"])
        self.assertEqual(movement.movement_type, "outbound")
        self.assertEqual(movement.product, self.product)
        self.assertEqual(movement.from_location, self.receiving)
        self.assertIsNone(movement.to_location)
        self.assertEqual(movement.quantity, 4)
        self.assertEqual(movement.reference, "ORDER-1")
        self.assert_balance(6, 3, 3)
        self.assertEqual(self.action(reservation, "fulfill").status_code, 400)
        self.assertEqual(self.action(reservation, "release").status_code, 400)
        self.assertEqual(StockMovement.objects.count(), 2)
        call_command("verify_stock", stdout=StringIO())

    def test_fully_reserved_stock_still_appears_with_zero_available(self):
        response = self.reserve(10)
        self.assertEqual(response.status_code, 201, response.json())
        self.assert_balance(10, 10, 0)
        response = self.reserve(1)
        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn("0 disponibles", str(response.json()))

    def test_invalid_requests_do_not_create_reservations(self):
        for overrides in (
            {"quantity": 0}, {"quantity": -1}, {"quantity": 11},
            {"quantity": 1.5}, {"quantity": None}, {"quantity": 2**31},
            {"reference": ""}, {"stock_item": 999999},
        ):
            with self.subTest(overrides=overrides):
                response = self.reserve(**overrides)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(Reservation.objects.count(), 0)
        self.assert_balance(10, 0, 10)

    def test_service_validates_quantity_and_reference(self):
        for quantity, reference in ((0, "ORDER"), (1, ""), (11, "ORDER")):
            with self.subTest(quantity=quantity, reference=reference):
                with self.assertRaises(ValidationError):
                    reserve_stock(stock_item=self.stock, quantity=quantity, reference=reference)
        self.assertEqual(Reservation.objects.count(), 0)

    def test_inactive_stock_blocks_reserve_and_fulfill_but_release_is_allowed(self):
        for obj in (self.product, self.receiving, self.warehouse):
            with self.subTest(model=type(obj).__name__):
                reservation = self.reserve().json()["id"]
                obj.is_active = False
                obj.save(update_fields=["is_active"])
                self.assertEqual(self.reserve().status_code, 400)
                self.assertEqual(self.action(reservation, "fulfill").status_code, 400)
                self.assertEqual(self.action(reservation, "release").status_code, 200)
                obj.is_active = True
                obj.save(update_fields=["is_active"])
        self.assert_balance(10, 0, 10)

    def test_ordinary_debits_cannot_consume_reserved_stock(self):
        self.reserve(7)
        payloads = [
            {"movement_type": "outbound", "from_location": self.receiving.pk},
            {
                "movement_type": "transfer", "from_location": self.receiving.pk,
                "to_location": self.storage.pk,
            },
            {"movement_type": "adjustment", "from_location": self.receiving.pk},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.move(**payload, quantity=4)
                self.assertEqual(response.status_code, 400, response.json())
                self.assertIn("3 disponibles", str(response.json()))
                self.assert_balance(10, 7, 3)
        response = self.move(movement_type="outbound", from_location=self.receiving.pk, quantity=3)
        self.assertEqual(response.status_code, 201, response.json())
        self.assert_balance(7, 7, 0)

    def test_batch_respects_reserved_balance_and_rolls_back_earlier_lines(self):
        self.reserve(7)
        before = list(StockMovement.objects.values())
        response = self.client.post("/api/movements/batch/", [
            {
                "movement_type": "inbound", "product": self.product.pk,
                "to_location": self.storage.pk, "quantity": 5,
            },
            {
                "movement_type": "outbound", "product": self.product.pk,
                "from_location": self.receiving.pk, "quantity": 4,
            },
        ], format="json")
        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn("quantity", response.json()[1])
        self.assert_balance(10, 7, 3)
        self.assertFalse(StockItem.objects.filter(location=self.storage).exists())
        self.assertEqual(list(StockMovement.objects.values()), before)

    def test_fulfillment_failure_rolls_back_status_stock_and_movement(self):
        reservation = self.reserve().json()["id"]
        with patch.object(Reservation, "save", side_effect=RuntimeError("Storage failure")):
            with self.assertRaisesMessage(RuntimeError, "Storage failure"):
                self.action(reservation, "fulfill")
        self.assertEqual(Reservation.objects.get(pk=reservation).status, "active")
        self.assert_balance(10, 4, 6)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_corrupted_stock_cannot_be_fulfilled_or_steal_another_reservation(self):
        reservation = self.reserve(4).json()["id"]
        self.reserve(4, reference="ORDER-2")
        StockItem.objects.filter(pk=self.stock.pk).update(quantity=5)
        self.assertEqual(self.action(reservation, "fulfill").status_code, 400)
        self.assertEqual(Reservation.objects.get(pk=reservation).status, "active")
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_product_totals_do_not_duplicate_stock_across_multiple_reservations(self):
        self.reserve(2)
        self.reserve(3)
        self.move(movement_type="inbound", to_location=self.storage.pk, quantity=7)
        other_stock = StockItem.objects.get(location=self.storage)
        self.reserve(4, stock_item=other_stock.pk)
        self.product.reorder_level = 5
        self.product.save(update_fields=["reorder_level"])
        response = self.client.get(f"/api/products/{self.product.pk}/")
        data = response.json()
        self.assertEqual([data[key] for key in ("total_quantity", "total_reserved", "total_available")], [17, 9, 8])
        listing = self.client.get("/api/products/").json()["results"][0]
        self.assertEqual(listing["total_quantity"], 17)
        self.assertEqual(listing["total_reserved"], 9)
        summary = self.client.get("/api/reports/summary/").json()
        self.assertEqual(summary["total_units"], 17)
        self.assertEqual(summary["low_stock"], [])
        self.assertEqual(
            self.client.patch(
                f"/api/products/{self.product.pk}/", {"name": "Updated"}, format="json",
            ).json()["total_available"],
            8,
        )

    def test_reservations_are_scoped_to_product_and_location(self):
        self.reserve(7)
        other_product = Product.objects.create(sku="OTHER", name="Other")
        record_movement(
            movement_type="inbound", product=other_product, to_location=self.receiving, quantity=5,
        )
        self.move(movement_type="inbound", to_location=self.storage.pk, quantity=6)
        response = self.client.get("/api/stock/")
        rows = {(row["product"], row["location"]): row for row in response.json()["results"]}
        self.assertEqual(rows[(self.product.pk, self.receiving.pk)]["reserved_quantity"], 7)
        self.assertEqual(rows[(other_product.pk, self.receiving.pk)]["available_quantity"], 5)
        self.assertEqual(rows[(self.product.pk, self.storage.pk)]["available_quantity"], 6)
        self.assertEqual(
            self.client.get(f"/api/products/{other_product.pk}/").json()["total_reserved"], 0,
        )

    def test_reservation_filters_and_immutable_fields(self):
        first = self.reserve().json()["id"]
        second = self.reserve(1, reference="ORDER-2").json()["id"]
        self.action(second, "release")
        for params in (
            {"status": "active"}, {"reference": "ORDER-1"}, {"search": "ORDER-1"},
            {"stock_item": self.stock.pk, "status": "active"},
            {"product": self.product.pk, "location": self.receiving.pk, "warehouse": self.warehouse.pk, "status": "active"},
        ):
            with self.subTest(params=params):
                response = self.client.get("/api/reservations/", params)
                self.assertEqual(response.status_code, 200, response.json())
                self.assertEqual([row["id"] for row in response.json()["results"]], [first])
        for params in ({"status": "unknown"}, {"stock_item": "abc"}, {"warehouse": "abc"}):
            self.assertEqual(self.client.get("/api/reservations/", params).status_code, 400)
        self.assertEqual(self.client.get(f"/api/reservations/{first}/").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/reservations/{first}/").status_code, 405)
        self.assertEqual(
            self.client.patch(f"/api/reservations/{first}/", {"quantity": 9}, format="json").status_code, 405,
        )
        response = self.reserve(1, status="fulfilled", movement=1)
        self.assertEqual(response.json()["status"], "active")
        self.assertIsNone(response.json()["movement"])
        self.assertEqual(self.action(999999, "fulfill").status_code, 404)

    def test_reservation_history_protects_stock_and_fulfillment_from_deletion(self):
        reservation = self.reserve().json()["id"]
        self.action(reservation, "fulfill")
        self.assertEqual(self.client.delete(f"/api/products/{self.product.pk}/").status_code, 409)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Reservation.objects.filter(pk=reservation).update(quantity=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Reservation.objects.filter(pk=reservation).update(status="active")

    def test_reconciliation_preserves_reservations_and_refuses_excess_holds(self):
        self.reserve(7)
        StockItem.objects.filter(pk=self.stock.pk).update(quantity=2)
        call_command("verify_stock", rebuild=True, stdout=StringIO())
        self.assert_balance(10, 7, 3)
        StockMovement.objects.update(quantity=5)
        for rebuild in (False, True):
            with self.subTest(rebuild=rebuild):
                with self.assertRaisesMessage(CommandError, "Active reservations exceed ledger stock"):
                    call_command("verify_stock", rebuild=rebuild, stdout=StringIO())
        self.assert_balance(10, 7, 3)

    def test_demo_clear_refuses_inventory_with_reservation_history(self):
        call_command("seed_demo_data", warehouses=1, products=1, stdout=StringIO())
        demo_stock = StockItem.objects.filter(product__sku__startswith="DEMO-", quantity__gt=0).first()
        reservation = reserve_stock(stock_item=demo_stock, quantity=1, reference="ORDER-DEMO")
        close_reservation(reservation, fulfill=False)
        before = StockMovement.objects.count()
        with self.assertRaisesMessage(CommandError, "referenced by reservations"):
            call_command("seed_demo_data", clear=True, warehouses=1, products=1, stdout=StringIO())
        self.assertEqual(StockMovement.objects.count(), before)
        self.assertTrue(Reservation.objects.filter(pk=reservation.pk).exists())

    def test_admin_lists_balances_and_performs_guarded_actions(self):
        user = get_user_model().objects.create_superuser(username="reservations-admin")
        self.client.force_login(user)
        reservation = self.reserve().json()["id"]
        for name in ("stockitem", "reservation"):
            self.assertEqual(self.client.get(reverse(f"admin:inventory_{name}_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:inventory_reservation_add")).status_code, 200)
        url = reverse("admin:inventory_reservation_changelist")
        response = self.client.post(url, {
            "action": "fulfill_selected", "_selected_action": [reservation],
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Reservation.objects.get(pk=reservation).status, "fulfilled")
        self.assert_balance(6, 0, 6)

    def test_view_only_admin_cannot_run_reservation_actions(self):
        user = get_user_model().objects.create_user(username="reservation-viewer", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_reservation"))
        self.client.force_login(user)
        reservation = self.reserve().json()["id"]
        response = self.client.post(reverse("admin:inventory_reservation_changelist"), {
            "action": "fulfill_selected", "_selected_action": [reservation],
        })
        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(Reservation.objects.get(pk=reservation).status, "active")


class ReservationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        warehouse = Warehouse.objects.create(code="RES", name="Reservation")
        self.location = Location.objects.create(warehouse=warehouse, code="A")
        self.product = Product.objects.create(sku="RES", name="Reservation")
        record_movement(
            movement_type="inbound", product=self.product, to_location=self.location, quantity=5,
        )
        self.stock = StockItem.objects.get(product=self.product)

    def race(self, requests):
        barrier = Barrier(2)

        def post(url, data):
            paused = False

            def synchronize(execute, sql, params, many, context):
                nonlocal paused
                if not paused and "FOR UPDATE" in sql and '"inventory_stockitem"' in sql:
                    paused = True
                    barrier.wait(timeout=5)
                return execute(sql, params, many, context)

            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '3s'")
                    cursor.execute("SET statement_timeout = '5s'")
                with connection.execute_wrapper(synchronize):
                    return APIClient().post(url, data, format="json")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(post, url, data) for url, data in requests]
            return [future.result(timeout=10) for future in futures]

    def test_concurrent_reservations_cannot_overbook(self):
        request = ("/api/reservations/", {
            "stock_item": self.stock.pk, "quantity": 4, "reference": "ORDER",
        })
        responses = self.race([request, request])
        self.assertEqual(sorted(response.status_code for response in responses), [201, 400])
        self.assertEqual(Reservation.objects.count(), 1)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.available_quantity, 1)
        self.assertEqual(self.stock.quantity, 5)

    def test_reservation_and_batch_outbound_share_stock_lock(self):
        responses = self.race([
            ("/api/reservations/", {"stock_item": self.stock.pk, "quantity": 4, "reference": "ORDER"}),
            ("/api/movements/batch/", [{
                "movement_type": "outbound", "product": self.product.pk,
                "from_location": self.location.pk, "quantity": 4,
            }]),
        ])
        self.assertEqual(sorted(response.status_code for response in responses), [201, 400])
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.available_quantity, 1)
        call_command("verify_stock", stdout=StringIO())

    def test_concurrent_fulfillment_records_exactly_one_outbound(self):
        reservation = reserve_stock(stock_item=self.stock, quantity=4, reference="ORDER")
        request = (f"/api/reservations/{reservation.pk}/fulfill/", {})
        responses = self.race([request, request])
        self.assertEqual(sorted(response.status_code for response in responses), [200, 400])
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 1)
        self.assertEqual(StockMovement.objects.filter(movement_type="outbound").count(), 1)
        call_command("verify_stock", stdout=StringIO())

    def test_release_and_fulfill_cannot_both_transition_the_same_reservation(self):
        reservation = reserve_stock(stock_item=self.stock, quantity=4, reference="ORDER")
        responses = self.race([
            (f"/api/reservations/{reservation.pk}/release/", {}),
            (f"/api/reservations/{reservation.pk}/fulfill/", {}),
        ])
        self.assertEqual(sorted(response.status_code for response in responses), [200, 400])
        reservation.refresh_from_db()
        self.stock.refresh_from_db()
        expected = 1 if reservation.status == "fulfilled" else 5
        self.assertEqual(self.stock.quantity, expected)
        self.assertEqual(self.stock.available_quantity, expected)
        call_command("verify_stock", stdout=StringIO())
