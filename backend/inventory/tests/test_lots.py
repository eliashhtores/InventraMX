from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as ModelValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, connections, transaction
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from inventory.models import Location, Lot, Product, Reservation, StockItem, StockMovement, Warehouse
from inventory.services import close_reservation, record_movement, reserve_stock

from .test_api import ApiTestCase


class BarcodeTests(ApiTestCase):
    def test_barcodes_are_optional_unique_strings_with_exact_lookup(self):
        for endpoint, first, second in (
            ("/api/products/", {"sku": "P-1", "name": "First"}, {"sku": "P-2", "name": "Second"}),
            (
                "/api/locations/",
                {"warehouse": self.warehouse.pk, "code": "B-1"},
                {"warehouse": self.warehouse.pk, "code": "B-2"},
            ),
        ):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(endpoint, {**first, "barcode": "000123"}, format="json")
                self.assertEqual(response.status_code, 201, response.json())
                self.assertEqual(response.json()["barcode"], "000123")
                record_id = response.json()["id"]
                response = self.client.post(endpoint, {**second, "barcode": "000123"}, format="json")
                self.assertEqual(response.status_code, 400, response.json())
                self.assertIn("barcode", response.json())
                response = self.client.get(endpoint, {"barcode": "000123"})
                self.assertEqual([row["id"] for row in response.json()["results"]], [record_id])
                self.assertEqual(self.client.get(endpoint, {"barcode": "123"}).json()["count"], 0)
                self.assertEqual(self.client.get(endpoint, {"search": "000123"}).json()["count"], 1)
                response = self.client.patch(f"{endpoint}{record_id}/", {"barcode": ""}, format="json")
                self.assertEqual(response.status_code, 200, response.json())
                response = self.client.post(endpoint, {**second, "barcode": ""}, format="json")
                self.assertEqual(response.status_code, 201, response.json())
                self.assertIsNone(self.client.get(f"{endpoint}{record_id}/").json()["barcode"])
                self.assertIsNone(self.client.get(f"{endpoint}{response.json()['id']}/").json()["barcode"])

    def test_untracked_is_default_and_flag_can_change_before_history(self):
        response = self.client.post("/api/products/", {"sku": "NEW", "name": "New"}, format="json")
        self.assertFalse(response.json()["lot_tracked"])
        url = f"/api/products/{response.json()['id']}/"
        self.assertTrue(self.client.patch(url, {"lot_tracked": True}, format="json").json()["lot_tracked"])
        self.assertFalse(self.client.patch(url, {"lot_tracked": False}, format="json").json()["lot_tracked"])
        self.assertEqual(self.client.get("/api/products/", {"lot_tracked": "false"}).json()["count"], 2)
        self.assertEqual(self.client.get("/api/products/", {"lot_tracked": "invalid"}).status_code, 400)

    def test_flag_cannot_change_after_history_even_with_zero_stock(self):
        self.move(movement_type="inbound", to_location=self.receiving.pk, quantity=1)
        self.move(movement_type="outbound", from_location=self.receiving.pk, quantity=1)
        response = self.client.patch(
            f"/api/products/{self.product.pk}/", {"lot_tracked": True}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn("lot_tracked", response.json())
        self.product.lot_tracked = True
        with self.assertRaises(ModelValidationError):
            self.product.save()


class LotInventoryTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.product.lot_tracked = True
        self.product.save()
        self.lot_a = Lot.objects.create(
            product=self.product, lot_number="LOT-A", expiry_date="2027-01-31",
        )
        self.lot_b = Lot.objects.create(product=self.product, lot_number="LOT-B", expiry_date="2027-12-31")

    def inbound(self, lot, quantity=10):
        response = self.move(
            movement_type="inbound", to_location=self.receiving.pk, quantity=quantity, lot=lot.pk,
        )
        self.assertEqual(response.status_code, 201, response.json())
        return response

    def test_lot_expiry_required_and_number_unique_per_product(self):
        response = self.client.post(
            "/api/lots/", {"product": self.product.pk, "lot_number": "000123", "expiry_date": "2027-12-31"}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(response.json()["lot_number"], "000123")
        self.assertNotIn("batch", response.json())
        self.assertNotIn("code", response.json())
        self.assertEqual(response.json()["expiry_date"], "2027-12-31")
        self.assertEqual(self.client.post(
            "/api/lots/", {"product": self.product.pk, "lot_number": "000123", "expiry_date": "2027-12-31"}, format="json",
        ).status_code, 400)
        other = Product.objects.create(sku="TRACKED-2", name="Other", lot_tracked=True)
        self.assertEqual(self.client.post(
            "/api/lots/", {"product": other.pk, "lot_number": "000123", "expiry_date": "2027-12-31"}, format="json",
        ).status_code, 201)
        self.assertEqual(self.client.post(
            "/api/lots/", {"product": other.pk, "lot_number": "INVALID", "expiry_date": "2027-02-30"},
            format="json",
        ).status_code, 400)

    def test_new_lots_require_expiry_in_model_api_and_admin(self):
        for value in ("omitted", None, ""):
            payload = {"product": self.product.pk, "lot_number": "NEW"}
            if value != "omitted":
                payload["expiry_date"] = value
            response = self.client.post("/api/lots/", payload, format="json")
            self.assertEqual(response.status_code, 400)
            self.assertIn("expiry_date", response.json())
        with self.assertRaises(ModelValidationError):
            Lot.objects.create(product=self.product, lot_number="UNDATED")
        user = get_user_model().objects.create_superuser(username="expiry-admin")
        self.client.force_login(user)
        response = self.client.post(reverse("admin:inventory_lot_add"), {
            "product": self.product.pk, "lot_number": "UNDATED", "expiry_date": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("expiry_date", response.context["adminform"].form.errors)
        self.assertFalse(Lot.objects.filter(lot_number="UNDATED").exists())

    def test_existing_undated_lots_remain_usable_and_unchanged(self):
        Lot.objects.filter(pk=self.lot_b.pk).update(expiry_date=None)
        self.lot_b.refresh_from_db()
        self.inbound(self.lot_b)
        response = self.client.patch(f"/api/lots/{self.lot_b.pk}/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertIsNone(response.json()["expiry_date"])

    def test_lot_number_is_required_and_cannot_be_blank(self):
        for value in (None, ""):
            payload = {"product": self.product.pk}
            if value is not None:
                payload["lot_number"] = value
            response = self.client.post("/api/lots/", payload, format="json")
            self.assertEqual(response.status_code, 400, response.json())
            self.assertIn("lot_number", response.json())

    def test_untracked_skus_reject_lots_and_tracked_skus_require_one(self):
        ordinary = Product.objects.create(sku="UNTRACKED", name="Ordinary")
        for payload in (
            {"product": self.product.pk},
            {"product": self.product.pk, "lot": None},
            {"product": ordinary.pk, "lot": self.lot_a.pk},
        ):
            response = self.move(
                movement_type="inbound", to_location=self.receiving.pk, quantity=1, **payload,
            )
            self.assertEqual(response.status_code, 400, response.json())
            self.assertIn("lot", response.json())
        other = Product.objects.create(sku="TRACKED-2", name="Other", lot_tracked=True)
        response = self.move(
            movement_type="inbound", to_location=self.receiving.pk, quantity=1,
            product=other.pk, lot=self.lot_a.pk,
        )
        self.assertEqual(response.status_code, 400, response.json())
        self.assertEqual(self.client.post(
            "/api/lots/", {"product": ordinary.pk, "lot_number": "INVALID"}, format="json",
        ).status_code, 400)
        self.assertEqual(StockItem.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_service_enforces_tracking_rules_and_preserves_single_error_shape(self):
        with self.assertRaises(ValidationError) as raised:
            record_movement(
                product=self.product, movement_type="inbound",
                to_location=self.receiving, quantity=1,
            )
        self.assertIn("lot", raised.exception.detail)
        self.assertEqual(StockItem.objects.count(), 0)
        Lot.objects.filter(pk=self.lot_b.pk).delete()
        with self.assertRaises(ValidationError) as raised:
            record_movement(
                product=self.product, lot=self.lot_b, movement_type="inbound",
                to_location=self.receiving, quantity=1,
            )
        self.assertIn("lot", raised.exception.detail)
        self.assertEqual(StockItem.objects.count(), 0)

    def test_lot_metadata_and_product_are_immutable_once_used(self):
        url = f"/api/lots/{self.lot_a.pk}/"
        response = self.client.patch(url, {"lot_number": "CORRECTED"}, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["lot_number"], "CORRECTED")
        self.inbound(self.lot_a)
        for changes in ({"lot_number": "NEW"}, {"expiry_date": None}):
            with self.subTest(changes=changes):
                self.assertEqual(self.client.patch(url, changes, format="json").status_code, 400)
        other = Product.objects.create(sku="TRACKED-2", name="Other", lot_tracked=True)
        self.assertEqual(self.client.patch(
            f"/api/lots/{self.lot_b.pk}/", {"product": other.pk}, format="json",
        ).status_code, 400)
        self.assertEqual(self.client.delete(url).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/lots/{self.lot_b.pk}/").status_code, 204)
        self.assertEqual(self.client.patch(
            f"/api/products/{self.product.pk}/", {"lot_tracked": False}, format="json",
        ).status_code, 400)

    def test_flag_is_blocked_by_unused_lot_and_existing_stock(self):
        response = self.client.patch(
            f"/api/products/{self.product.pk}/", {"lot_tracked": False}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.json())
        ordinary = Product.objects.create(sku="ORDINARY", name="Ordinary")
        StockItem.objects.create(product=ordinary, location=self.receiving, quantity=1)
        response = self.client.patch(
            f"/api/products/{ordinary.pk}/", {"lot_tracked": True}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.json())

    def test_admin_lot_validation_and_used_metadata_readonly(self):
        user = get_user_model().objects.create_superuser(username="lot-admin")
        self.client.force_login(user)
        url = reverse("admin:inventory_lot_add")
        response = self.client.post(url, {"product": "", "lot_number": "INVALID"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["adminform"].form.errors)
        self.inbound(self.lot_a)
        response = self.client.get(reverse("admin:inventory_lot_change", args=[self.lot_a.pk]))
        self.assertEqual(response.status_code, 200)
        for field in ("product", "lot_number", "expiry_date"):
            self.assertIn(field, response.context["adminform"].readonly_fields)
        self.assertEqual(self.client.get(reverse("admin:inventory_lot_changelist")).status_code, 200)

    def test_stock_movements_and_reservations_are_isolated_by_lot(self):
        self.inbound(self.lot_a, 10)
        self.inbound(self.lot_b, 20)
        stock_a = StockItem.objects.get(lot=self.lot_a)
        stock_b = StockItem.objects.get(lot=self.lot_b)
        reservation = reserve_stock(stock_item=stock_a, quantity=8, reference="ORDER")
        self.assertEqual(stock_a.available_quantity, 2)
        self.assertEqual(stock_b.available_quantity, 20)
        response = self.move(
            movement_type="outbound", from_location=self.receiving.pk, quantity=3, lot=self.lot_a.pk,
        )
        self.assertEqual(response.status_code, 400, response.json())
        response = self.move(
            movement_type="transfer", from_location=self.receiving.pk,
            to_location=self.storage.pk, quantity=4, lot=self.lot_b.pk,
        )
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(StockItem.objects.get(location=self.storage, lot=self.lot_b).quantity, 4)
        fulfilled = close_reservation(reservation, fulfill=True)
        self.assertEqual(fulfilled.movement.lot_id, self.lot_a.pk)
        stock_a.refresh_from_db()
        self.assertEqual(stock_a.quantity, 2)
        self.assertEqual(stock_a.reserved_quantity, 0)
        data = self.client.get(f"/api/products/{self.product.pk}/").json()
        self.assertEqual(data["total_quantity"], 22)
        self.assertEqual(data["total_available"], 22)
        call_command("verify_stock", stdout=StringIO())

    def test_batch_same_product_location_different_lots_and_failure_rollback(self):
        lines = [
            {
                "movement_type": "inbound", "product": self.product.pk,
                "to_location": self.receiving.pk, "lot": lot.pk, "quantity": quantity,
            }
            for lot, quantity in ((self.lot_a, 2), (self.lot_b, 10))
        ]
        response = self.client.post("/api/movements/batch/", lines, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual([line["lot"] for line in response.json()], [self.lot_a.pk, self.lot_b.pk])
        before = list(StockItem.objects.order_by("pk").values())
        response = self.client.post("/api/movements/batch/", [
            lines[1],
            {
                "movement_type": "outbound", "product": self.product.pk,
                "from_location": self.receiving.pk, "lot": self.lot_a.pk, "quantity": 3,
            },
        ], format="json")
        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn("quantity", response.json()[1])
        self.assertEqual(list(StockItem.objects.order_by("pk").values()), before)
        self.assertEqual(StockMovement.objects.count(), 2)
        invalid = {**lines[0], "lot": None}
        response = self.client.post("/api/movements/batch/", [lines[0], invalid], format="json")
        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn("lot", response.json()[1])

    def test_api_exposes_lot_number_expiry_and_exact_filters(self):
        response = self.inbound(self.lot_a)
        self.inbound(self.lot_b)
        self.assertEqual(response.json()["lot_number"], "LOT-A")
        self.assertNotIn("lot_code", response.json())
        self.assertNotIn("batch", response.json())
        self.assertEqual(response.json()["expiry_date"], "2027-01-31")
        stock = StockItem.objects.get(lot=self.lot_a)
        reservation = reserve_stock(stock_item=stock, quantity=1, reference="ORDER")
        for endpoint in ("/api/stock/", "/api/movements/", "/api/reservations/"):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint, {"lot": self.lot_a.pk})
                self.assertEqual(response.status_code, 200, response.json())
                self.assertEqual(response.json()["count"], 1)
                row = response.json()["results"][0]
                self.assertEqual(row["lot"], self.lot_a.pk)
                self.assertEqual(row["lot_number"], "LOT-A")
                self.assertNotIn("lot_code", row)
                self.assertNotIn("batch", row)
                self.assertEqual(row["expiry_date"], "2027-01-31")
                self.assertEqual(self.client.get(endpoint, {"lot": "bad"}).status_code, 400)
                self.assertEqual(self.client.get(endpoint, {"search": "LOT-A"}).json()["count"], 1)
        response = self.client.get("/api/lots/", {"product": self.product.pk, "lot_number": "LOT-A"})
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(self.client.get("/api/lots/", {"lot_number": "LOT"}).json()["count"], 0)
        self.assertEqual(self.client.get("/api/lots/", {"search": "LOT-B"}).json()["count"], 1)
        self.assertEqual(self.client.get(
            f"/api/reservations/{reservation.pk}/",
        ).json()["lot"], self.lot_a.pk)

    def test_expired_lots_remain_explicitly_usable_without_fefo_policy(self):
        expired = Lot.objects.create(product=self.product, lot_number="OLD", expiry_date="2000-01-01")
        self.inbound(expired)
        response = self.move(
            movement_type="outbound", from_location=self.receiving.pk, lot=expired.pk, quantity=1,
        )
        self.assertEqual(response.status_code, 201, response.json())

    def test_reconciliation_checks_each_lot_not_just_combined_balance(self):
        self.inbound(self.lot_a, 10)
        self.inbound(self.lot_b, 20)
        StockItem.objects.filter(lot=self.lot_a).update(quantity=20)
        StockItem.objects.filter(lot=self.lot_b).update(quantity=10)
        output = StringIO()
        with self.assertRaisesMessage(CommandError, "Found 2 stock mismatch(es)"):
            call_command("verify_stock", stdout=output)
        self.assertIn(f"lot_id={self.lot_a.pk}", output.getvalue())
        call_command("verify_stock", rebuild=True, stdout=StringIO())
        self.assertEqual(StockItem.objects.get(lot=self.lot_a).quantity, 10)
        self.assertEqual(StockItem.objects.get(lot=self.lot_b).quantity, 20)
        StockItem.objects.filter(lot=self.lot_b).delete()
        call_command("verify_stock", rebuild=True, stdout=StringIO())
        self.assertEqual(StockItem.objects.get(lot=self.lot_b).quantity, 20)
        reservation = reserve_stock(
            stock_item=StockItem.objects.get(lot=self.lot_a), quantity=8, reference="ORDER",
        )
        StockMovement.objects.filter(lot=self.lot_a).update(quantity=5)
        StockMovement.objects.filter(lot=self.lot_b).update(quantity=25)
        with self.assertRaisesMessage(CommandError, "Active reservations exceed ledger stock"):
            call_command("verify_stock", rebuild=True, stdout=StringIO())
        self.assertEqual(Reservation.objects.get(pk=reservation.pk).status, "active")

    def test_database_uniqueness_includes_null_untracked_and_distinct_lots(self):
        self.inbound(self.lot_a)
        self.inbound(self.lot_b)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockItem.objects.create(product=self.product, location=self.receiving, lot=self.lot_a)
        ordinary = Product.objects.create(sku="ORDINARY", name="Ordinary")
        StockItem.objects.create(product=ordinary, location=self.receiving)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockItem.objects.create(product=ordinary, location=self.receiving)

    def test_summary_sums_lots_and_reservations_without_changing_on_hand_semantics(self):
        self.inbound(self.lot_a, 2)
        self.inbound(self.lot_b, 3)
        reserve_stock(stock_item=StockItem.objects.get(lot=self.lot_a), quantity=2, reference="ORDER")
        summary = self.client.get("/api/reports/summary/").json()
        self.assertEqual(summary["total_units"], 5)
        self.assertEqual(summary["low_stock"][0]["total_quantity"], 5)
        self.assertEqual(summary["warehouse_breakdown"][0]["total_units"], 5)
        self.assertEqual(summary["warehouse_breakdown"][0]["low_stock"][0]["total_quantity"], 5)


class LotConcurrencyTests(TransactionTestCase):
    def test_simultaneous_receipts_for_different_lots_do_not_merge_stock(self):
        warehouse = Warehouse.objects.create(code="LOT", name="Lots")
        location = Location.objects.create(warehouse=warehouse, code="A")
        product = Product.objects.create(sku="LOT", name="Tracked", lot_tracked=True)
        lots = [
            Lot.objects.create(product=product, lot_number=number, expiry_date="2027-12-31")
            for number in ("A", "B")
        ]
        barrier = Barrier(2)

        def receipt(lot):
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '3s'")
                barrier.wait(timeout=5)
                return APIClient().post("/api/movements/", {
                    "movement_type": "inbound", "product": product.pk, "lot": lot.pk,
                    "to_location": location.pk, "quantity": 4,
                }, format="json")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(receipt, lot) for lot in lots]
            responses = [future.result(timeout=10) for future in futures]
        self.assertEqual([response.status_code for response in responses], [201, 201])
        self.assertEqual(StockItem.objects.count(), 2)
        self.assertEqual(
            set(StockItem.objects.values_list("lot_id", "quantity")),
            {(lot.pk, 4) for lot in lots},
        )
        call_command("verify_stock", stdout=StringIO())

    def test_initial_movement_and_tracking_change_cannot_create_invalid_inventory(self):
        warehouse = Warehouse.objects.create(code="RACE", name="Race")
        location = Location.objects.create(warehouse=warehouse, code="A")
        product = Product.objects.create(sku="RACE", name="Untracked")
        barrier = Barrier(2)

        def request(change_tracking):
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '3s'")
                barrier.wait(timeout=5)
                client = APIClient()
                if change_tracking:
                    return client.patch(
                        f"/api/products/{product.pk}/", {"lot_tracked": True}, format="json",
                    )
                return client.post("/api/movements/", {
                    "movement_type": "inbound", "product": product.pk,
                    "to_location": location.pk, "quantity": 1,
                }, format="json")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(request, change) for change in (False, True)]
            movement_response, product_response = [future.result(timeout=10) for future in futures]
        product.refresh_from_db()
        if product.lot_tracked:
            self.assertEqual(product_response.status_code, 200, product_response.json())
            self.assertEqual(movement_response.status_code, 400, movement_response.json())
            self.assertEqual(StockItem.objects.count(), 0)
            self.assertEqual(StockMovement.objects.count(), 0)
        else:
            self.assertEqual(product_response.status_code, 400, product_response.json())
            self.assertEqual(movement_response.status_code, 201, movement_response.json())
            self.assertEqual(StockItem.objects.get().quantity, 1)
