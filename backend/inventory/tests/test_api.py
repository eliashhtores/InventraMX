from datetime import datetime, timezone
from unittest import mock

from django.db import OperationalError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone as django_timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from inventory.models import Location, Product, Reservation, StockItem, StockMovement, Warehouse
from inventory.services import record_movement, reserve_stock


class ApiTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.warehouse = Warehouse.objects.create(code="MX1", name="Monterrey")
        self.receiving = Location.objects.create(
            warehouse=self.warehouse, code="RCV-01", location_type=Location.Type.RECEIVING
        )
        self.storage = Location.objects.create(warehouse=self.warehouse, code="A-01-01")
        self.product = Product.objects.create(sku="SKU-1", name="Widget", reorder_level=5)

    def move(self, **payload):
        payload.setdefault("product", self.product.id)
        return self.client.post("/api/movements/", payload, format="json")

    def qty(self, location):
        item = StockItem.objects.filter(product=self.product, location=location).first()
        return item.quantity if item else 0


class CrudTests(ApiTestCase):
    def test_create_and_list_warehouses(self):
        response = self.client.post("/api/warehouses/", {"code": "MX2", "name": "Guadalajara"}, format="json")
        self.assertEqual(response.status_code, 201)
        response = self.client.get("/api/warehouses/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 2)

    def test_duplicate_location_code_in_warehouse_rejected(self):
        response = self.client.post(
            "/api/locations/", {"warehouse": self.warehouse.id, "code": "A-01-01"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_filter_locations_by_warehouse(self):
        other = Warehouse.objects.create(code="MX2", name="Other")
        Location.objects.create(warehouse=other, code="B-01")
        response = self.client.get(f"/api/locations/?warehouse={other.id}")
        self.assertEqual([loc["code"] for loc in response.json()["results"]], ["B-01"])
        self.assertEqual(self.client.get("/api/locations/?warehouse=abc").status_code, 400)

    def test_location_warehouse_cannot_be_changed(self):
        other = Warehouse.objects.create(code="MX2", name="Other")
        self.move(movement_type="inbound", to_location=self.storage.id, quantity=1)
        response = self.client.patch(
            f"/api/locations/{self.storage.id}/", {"warehouse": other.id}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.storage.refresh_from_db()
        self.assertEqual(self.storage.warehouse, self.warehouse)

    def test_invalid_choice_filters_rejected(self):
        self.assertEqual(self.client.get("/api/locations/?location_type=xyz").status_code, 400)
        self.assertEqual(self.client.get("/api/movements/?movement_type=xyz").status_code, 400)
        self.assertEqual(self.client.get("/api/locations/?location_type=receiving").json()["count"], 1)
        self.assertEqual(self.client.get("/api/movements/?movement_type=inbound").status_code, 200)

    def test_delete_referenced_warehouse_returns_conflict(self):
        response = self.client.delete(f"/api/warehouses/{self.warehouse.id}/")
        self.assertEqual(response.status_code, 409)

    def test_product_search_and_total_quantity(self):
        self.move(movement_type="inbound", to_location=self.storage.id, quantity=7)
        response = self.client.get("/api/products/?search=widget")
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["total_quantity"], 7)


class MovementTests(ApiTestCase):
    def test_inbound_increases_stock(self):
        response = self.move(movement_type="inbound", to_location=self.receiving.id, quantity=10)
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(self.qty(self.receiving), 10)

    def test_transfer_moves_stock(self):
        self.move(movement_type="inbound", to_location=self.receiving.id, quantity=10)
        response = self.move(
            movement_type="transfer", from_location=self.receiving.id, to_location=self.storage.id, quantity=4
        )
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(self.qty(self.receiving), 6)
        self.assertEqual(self.qty(self.storage), 4)

    def test_outbound_cannot_exceed_stock(self):
        self.move(movement_type="inbound", to_location=self.storage.id, quantity=3)
        response = self.move(movement_type="outbound", from_location=self.storage.id, quantity=5)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.qty(self.storage), 3)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_outbound_decreases_stock(self):
        self.move(movement_type="inbound", to_location=self.storage.id, quantity=3)
        response = self.move(movement_type="outbound", from_location=self.storage.id, quantity=3)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.qty(self.storage), 0)

    def test_adjustment_requires_exactly_one_location(self):
        response = self.move(movement_type="adjustment", quantity=1)
        self.assertEqual(response.status_code, 400)
        response = self.move(movement_type="adjustment", to_location=self.storage.id, quantity=2)
        self.assertEqual(response.status_code, 201)
        response = self.move(movement_type="adjustment", from_location=self.storage.id, quantity=1)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.qty(self.storage), 1)

    def test_invalid_location_combinations_rejected(self):
        self.assertEqual(self.move(movement_type="inbound", quantity=1).status_code, 400)
        self.assertEqual(
            self.move(movement_type="transfer", from_location=self.storage.id, to_location=self.storage.id,
                      quantity=1).status_code,
            400,
        )
        self.assertEqual(self.move(movement_type="inbound", to_location=self.storage.id, quantity=0).status_code, 400)

    def test_movements_reject_inactive_warehouses(self):
        self.move(movement_type="inbound", to_location=self.storage.id, quantity=3)
        self.warehouse.is_active = False
        self.warehouse.save(update_fields=["is_active"])

        inbound = self.move(movement_type="inbound", to_location=self.storage.id, quantity=1)
        self.assertEqual(inbound.status_code, 400)
        self.assertEqual(inbound.json()["to_location"], ["El almacén está inactivo."])

        outbound = self.move(movement_type="outbound", from_location=self.storage.id, quantity=1)
        self.assertEqual(outbound.status_code, 400)
        self.assertEqual(outbound.json()["from_location"], ["El almacén está inactivo."])
        self.assertEqual(self.qty(self.storage), 3)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_movements_are_immutable(self):
        movement_id = self.move(movement_type="inbound", to_location=self.storage.id, quantity=1).json()["id"]
        self.assertEqual(self.client.delete(f"/api/movements/{movement_id}/").status_code, 405)
        self.assertEqual(
            self.client.patch(f"/api/movements/{movement_id}/", {"quantity": 9}, format="json").status_code, 405
        )

    def test_backdated_movement_preserves_audit_timestamp_and_updates_stock(self):
        started = django_timezone.now()
        occurred_at = "2026-01-02T10:30:00-06:00"
        response = self.move(
            movement_type="inbound", to_location=self.receiving.id, quantity=10,
            occurred_at=occurred_at, created_at="2000-01-01T00:00:00Z",
        )
        self.assertEqual(response.status_code, 201, response.json())
        movement = StockMovement.objects.get(pk=response.json()["id"])
        self.assertEqual(movement.occurred_at, parse_datetime(occurred_at))
        self.assertGreaterEqual(movement.created_at, started)
        self.assertLessEqual(movement.created_at, django_timezone.now())
        self.assertEqual(parse_datetime(response.json()["occurred_at"]), movement.occurred_at)
        self.assertEqual(parse_datetime(response.json()["created_at"]), movement.created_at)
        self.assertEqual(self.qty(self.receiving), 10)

        detail = self.client.get(f"/api/movements/{movement.pk}/")
        self.assertEqual(detail.json()["occurred_at"], response.json()["occurred_at"])
        listing = self.client.get("/api/movements/").json()["results"][0]
        self.assertEqual(listing["occurred_at"], response.json()["occurred_at"])
        self.assertEqual(
            self.client.patch(
                f"/api/movements/{movement.pk}/", {"occurred_at": "2026-01-01T00:00:00Z"},
                format="json",
            ).status_code,
            405,
        )
        movement.refresh_from_db()
        self.assertEqual(movement.occurred_at, parse_datetime(occurred_at))

    def test_occurrence_time_defaults_to_now_for_api_and_service(self):
        started = django_timezone.now()
        response = self.move(movement_type="inbound", to_location=self.receiving.id, quantity=1)
        self.assertEqual(response.status_code, 201, response.json())
        occurred_at = parse_datetime(response.json()["occurred_at"])
        self.assertGreaterEqual(occurred_at, started)
        self.assertLessEqual(occurred_at, django_timezone.now())

        movement = record_movement(
            movement_type=StockMovement.Type.INBOUND, product=self.product,
            to_location=self.storage, quantity=2,
        )
        movement.refresh_from_db()
        self.assertGreaterEqual(movement.occurred_at, started)
        self.assertLessEqual(movement.occurred_at, movement.created_at)

    def test_invalid_occurrence_times_do_not_change_stock(self):
        for value in (None, "", "not-a-date", "2026-02-30T12:00:00Z"):
            with self.subTest(value=value):
                response = self.move(
                    movement_type="inbound", to_location=self.receiving.id, quantity=10,
                    occurred_at=value,
                )
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("occurred_at", response.json())
        self.assertEqual(StockMovement.objects.count(), 0)
        self.assertEqual(StockItem.objects.count(), 0)

    def test_backdating_does_not_bypass_insufficient_stock(self):
        response = self.move(
            movement_type="outbound", from_location=self.receiving.id, quantity=10,
            occurred_at="2026-01-02T10:00:00Z",
        )
        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn("quantity", response.json())
        self.assertEqual(StockMovement.objects.count(), 0)
        self.assertEqual(StockItem.objects.count(), 0)


class MovementQueryTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.other_warehouse = Warehouse.objects.create(code="MX2", name="Other")
        self.other_location = Location.objects.create(warehouse=self.other_warehouse, code="B-01")
        self.other_product = Product.objects.create(sku="SKU-2", name="Other product")
        payloads = [
            {
                "movement_type": "inbound", "to_location": self.receiving.id,
                "quantity": 10, "reference": "PO-100",
            },
            {
                "movement_type": "transfer", "from_location": self.receiving.id,
                "to_location": self.other_location.id, "quantity": 3, "reference": "po-100-transfer",
            },
            {
                "movement_type": "outbound", "from_location": self.other_location.id,
                "quantity": 1, "reference": "SHIP-1",
            },
            {
                "movement_type": "transfer", "from_location": self.receiving.id,
                "to_location": self.storage.id, "quantity": 2, "reference": "LOCAL",
            },
            {
                "movement_type": "inbound", "product": self.other_product.id,
                "to_location": self.other_location.id, "quantity": 4, "reference": "OTHER",
            },
        ]
        self.ids = []
        for payload, day in zip(payloads, [1, 2, 3, 2, 4]):
            response = self.move(**payload)
            self.assertEqual(response.status_code, 201, response.json())
            movement_id = response.json()["id"]
            self.ids.append(movement_id)
            StockMovement.objects.filter(pk=movement_id).update(
                created_at=datetime(2026, 11, day, 12, tzinfo=timezone.utc)
            )

    def assert_movements(self, params, expected):
        response = self.client.get("/api/movements/", params)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["count"], len(expected))
        self.assertEqual([row["id"] for row in response.json()["results"]], expected)

    def test_default_ordering_and_existing_filters(self):
        inbound, transfer, outbound, local, other = self.ids
        self.assert_movements({}, [other, outbound, local, transfer, inbound])
        self.assert_movements({"product": self.product.id}, [outbound, local, transfer, inbound])
        self.assert_movements({"movement_type": "transfer"}, [local, transfer])
        self.assert_movements({"search": "po-100"}, [transfer, inbound])

    def test_inclusive_datetime_bounds_and_timezone_offsets(self):
        inbound, transfer, outbound, local, other = self.ids
        self.assert_movements(
            {"created_at_after": "2026-11-02T12:00:00Z"},
            [other, outbound, local, transfer],
        )
        self.assert_movements(
            {"created_at_before": "2026-11-02T12:00:00Z"},
            [local, transfer, inbound],
        )
        self.assert_movements(
            {
                "created_at_after": "2026-11-02T06:00:00-06:00",
                "created_at_before": "2026-11-02T14:00:00+02:00",
            },
            [local, transfer],
        )

    def test_warehouse_matches_either_side_without_duplicate_transfers(self):
        inbound, transfer, outbound, local, other = self.ids
        self.assert_movements({"warehouse": self.warehouse.id}, [local, transfer, inbound])
        self.assert_movements({"warehouse": self.other_warehouse.id}, [other, outbound, transfer])
        self.assert_movements({"warehouse": 999999}, [])

    def test_occurrence_filters_and_ordering_are_independent_of_audit_time(self):
        inbound, transfer, outbound, local, other = self.ids
        for movement_id, day in zip(self.ids, [5, 2, 3, 2, 1]):
            StockMovement.objects.filter(pk=movement_id).update(
                occurred_at=datetime(2026, 10, day, 12, tzinfo=timezone.utc)
            )
        self.assert_movements(
            {"occurred_at_after": "2026-10-02T06:00:00-06:00"},
            [outbound, local, transfer, inbound],
        )
        self.assert_movements(
            {"occurred_at_before": "2026-10-02T12:00:00Z"},
            [other, local, transfer],
        )
        self.assert_movements(
            {
                "occurred_at_after": "2026-10-02T12:00:00Z",
                "occurred_at_before": "2026-10-02T12:00:00Z",
                "created_at_after": "2026-11-02T12:00:00Z",
                "product": self.product.id,
            },
            [local, transfer],
        )
        self.assert_movements({"ordering": "occurred_at"}, [other, transfer, local, outbound, inbound])
        self.assert_movements({"ordering": "-occurred_at"}, [inbound, outbound, local, transfer, other])
        self.assert_movements({}, [other, outbound, local, transfer, inbound])

    def test_location_matches_either_side(self):
        inbound, transfer, outbound, local, other = self.ids
        self.assert_movements({"location": self.receiving.id}, [local, transfer, inbound])
        self.assert_movements({"location": self.other_location.id}, [other, outbound, transfer])
        self.assert_movements({"location": self.storage.id}, [local])
        self.assert_movements({"location": 999999}, [])

    def test_reference_is_case_insensitive_substring_and_filters_combine(self):
        inbound, transfer, outbound, local, other = self.ids
        self.assert_movements({"reference": "o-100"}, [transfer, inbound])
        self.assert_movements(
            {
                "product": self.product.id,
                "movement_type": "transfer",
                "warehouse": self.warehouse.id,
                "location": self.other_location.id,
                "reference": "PO-100",
                "created_at_after": "2026-11-02T12:00:00Z",
                "created_at_before": "2026-11-02T12:00:00Z",
                "search": "SKU-1",
                "ordering": "created_at",
            },
            [transfer],
        )
        self.assert_movements({"reference": "missing"}, [])

    def test_ordering_supports_allowlisted_fields_in_both_directions(self):
        inbound, transfer, outbound, local, other = self.ids
        cases = [
            ("created_at", [inbound, transfer, local, outbound, other]),
            ("-created_at", [other, outbound, local, transfer, inbound]),
            ("id", self.ids),
            ("-id", list(reversed(self.ids))),
            ("quantity", [outbound, local, transfer, other, inbound]),
            ("-quantity", [inbound, other, transfer, local, outbound]),
            ("created_at,-id", [inbound, local, transfer, outbound, other]),
            ("-created_at, id", [other, outbound, transfer, local, inbound]),
        ]
        for ordering, expected in cases:
            with self.subTest(ordering=ordering):
                self.assert_movements({"ordering": ordering}, expected)

    def test_ordering_tie_breaker_is_stable_across_pages(self):
        results = []
        for offset in range(len(self.ids)):
            response = self.client.get(
                "/api/movements/", {"ordering": "created_at", "limit": 1, "offset": offset}
            )
            self.assertEqual(response.status_code, 200, response.json())
            results.extend(row["id"] for row in response.json()["results"])
        inbound, transfer, outbound, local, other = self.ids
        self.assertEqual(results, [inbound, transfer, local, outbound, other])

    def test_invalid_filter_values_return_field_errors(self):
        cases = [
            {"created_at_after": "not-a-date"},
            {"created_at_before": "2026-02-30T12:00:00Z"},
            {"created_at_after": "9999-12-31T23:59:59-23:59"},
            {"occurred_at_after": "not-a-date"},
            {"occurred_at_before": "2026-02-30T12:00:00Z"},
            {
                "occurred_at_after": "2026-11-03T00:00:00Z",
                "occurred_at_before": "2026-11-02T00:00:00Z",
            },
            {
                "created_at_after": "2026-11-03T00:00:00Z",
                "created_at_before": "2026-11-02T00:00:00Z",
            },
        ]
        for field in ("warehouse", "location"):
            cases.extend({field: value} for value in ("abc", "-1", "0", "1.5", str(2**63)))
        cases.extend(
            {"ordering": value}
            for value in ("product", "reference", "?", "--id", "created_at,", "quantity,__class__")
        )
        for params in cases:
            with self.subTest(params=params):
                response = self.client.get("/api/movements/", params)
                self.assertEqual(response.status_code, 400, response.content)
                field = next(iter(params)).replace("_after", "_before") if len(params) == 2 else next(iter(params))
                self.assertIn(field, response.json())

    def test_empty_filters_are_ignored(self):
        inbound, transfer, outbound, local, other = self.ids
        self.assert_movements(
            {
                "created_at_after": "", "created_at_before": "", "warehouse": "",
                "location": "", "reference": "", "ordering": "",
                "occurred_at_after": "", "occurred_at_before": "",
            },
            [other, outbound, local, transfer, inbound],
        )


class ReportTests(ApiTestCase):
    def test_summary_report(self):
        self.move(movement_type="inbound", to_location=self.storage.id, quantity=3)
        data = self.client.get("/api/reports/summary/").json()
        self.assertEqual(data["total_units"], 3)
        self.assertEqual(data["movements"]["inbound"], 1)
        self.assertEqual(data["products"], 1)
        self.assertEqual(data["low_stock_count"], 1)
        self.assertEqual([p["sku"] for p in data["low_stock"]], ["SKU-1"])

    def test_summary_product_count_and_alerts_only_include_active_products(self):
        Product.objects.create(sku="INACTIVE", name="Inactive", reorder_level=5, is_active=False)
        Product.objects.create(sku="NO-ALERT", name="No alert", reorder_level=0)
        data = self.client.get("/api/reports/summary/").json()
        self.assertEqual(data["products"], 2)
        self.assertEqual(data["low_stock_count"], 1)
        self.assertEqual([p["sku"] for p in data["low_stock"]], ["SKU-1"])

    def test_summary_with_no_active_products_has_zero_counts(self):
        Product.objects.update(is_active=False)
        data = self.client.get("/api/reports/summary/").json()
        self.assertEqual(data["products"], 0)
        self.assertEqual(data["low_stock_count"], 0)
        self.assertEqual(data["low_stock"], [])

    def test_low_stock_ignores_products_without_reorder_level(self):
        Product.objects.create(sku="SKU-0", name="No alert", reorder_level=0)
        data = self.client.get("/api/reports/summary/").json()
        self.assertEqual([p["sku"] for p in data["low_stock"]], ["SKU-1"])

    def test_warehouse_breakdown_exposes_local_shortage_hidden_by_global_stock(self):
        guadalajara = Warehouse.objects.create(code="GDL", name="Guadalajara")
        location = Location.objects.create(warehouse=guadalajara, code="A")
        self.move(movement_type="inbound", to_location=location.pk, quantity=100)

        data = self.client.get("/api/reports/summary/").json()

        self.assertEqual(data["total_units"], 100)
        self.assertEqual(data["low_stock"], [])
        self.assertEqual(data["low_stock_count"], 0)
        self.assertEqual([row["code"] for row in data["warehouse_breakdown"]], ["GDL", "MX1"])
        full, empty = data["warehouse_breakdown"]
        self.assertEqual(full, {
            "id": guadalajara.pk, "code": "GDL", "name": "Guadalajara", "is_active": True,
            "total_units": 100, "low_stock_count": 0, "low_stock": [],
        })
        self.assertEqual(empty, {
            "id": self.warehouse.pk, "code": "MX1", "name": "Monterrey", "is_active": True,
            "total_units": 0, "low_stock_count": 1,
            "low_stock": [{
                "id": self.product.pk, "sku": "SKU-1", "name": "Widget",
                "total_quantity": 0, "reorder_level": 5,
            }],
        })
        self.assertFalse(StockItem.objects.filter(location__warehouse=self.warehouse).exists())

    def test_warehouse_breakdown_aggregates_locations_and_includes_exact_threshold(self):
        self.move(movement_type="inbound", to_location=self.receiving.pk, quantity=2)
        self.move(movement_type="inbound", to_location=self.storage.pk, quantity=3)
        other = Warehouse.objects.create(code="OTHER", name="Other")
        location = Location.objects.create(warehouse=other, code="A")
        self.move(movement_type="inbound", to_location=location.pk, quantity=6)

        data = self.client.get("/api/reports/summary/").json()
        local, stocked = data["warehouse_breakdown"]
        self.assertEqual(data["low_stock"], [])
        self.assertEqual(local["total_units"], 5)
        self.assertEqual(local["low_stock_count"], 1)
        self.assertEqual(local["low_stock"][0]["total_quantity"], 5)
        self.assertEqual(stocked["total_units"], 6)
        self.assertEqual(stocked["low_stock"], [])
        self.assertEqual(sum(row["total_units"] for row in data["warehouse_breakdown"]), data["total_units"])

    def test_warehouse_alerts_use_active_products_positive_thresholds_and_on_hand(self):
        disabled = Product.objects.create(sku="DISABLED", name="No alert", reorder_level=0)
        inactive = Product.objects.create(sku="INACTIVE", name="Inactive", reorder_level=20, is_active=False)
        self.move(movement_type="inbound", to_location=self.storage.pk, quantity=10)
        stock = StockItem.objects.get(product=self.product)
        reserve_stock(stock_item=stock, quantity=10, reference="ORDER")
        # Retained stock in inactive inventory still contributes to on-hand totals.
        StockItem.objects.create(product=inactive, location=self.storage, quantity=4)
        StockItem.objects.create(product=disabled, location=self.storage, quantity=2)
        self.storage.is_active = False
        self.storage.save(update_fields=["is_active"])

        data = self.client.get("/api/reports/summary/").json()

        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(data["total_units"], 16)
        self.assertEqual(data["warehouse_breakdown"][0]["total_units"], 16)
        self.assertEqual(data["warehouse_breakdown"][0]["low_stock_count"], 0)
        self.assertEqual(data["warehouse_breakdown"][0]["low_stock"], [])

    def test_empty_inactive_warehouses_and_zero_quantity_rows_are_reported(self):
        other = Warehouse.objects.create(code="EMPTY", name="Empty", is_active=False)
        StockItem.objects.create(product=self.product, location=self.storage, quantity=0)
        data = self.client.get("/api/reports/summary/").json()
        empty, local = data["warehouse_breakdown"]
        self.assertEqual(empty["id"], other.pk)
        self.assertFalse(empty["is_active"])
        for row in (empty, local):
            self.assertEqual(row["total_units"], 0)
            self.assertEqual(row["low_stock_count"], 1)
            self.assertEqual(row["low_stock"][0]["total_quantity"], 0)

    def test_warehouse_alerts_are_ordered_limited_and_counted_before_truncation(self):
        self.product.reorder_level = 0
        self.product.save(update_fields=["reorder_level"])
        Product.objects.bulk_create([
            Product(sku=f"ALERT-{index:03}", name=f"Alert {index}", reorder_level=5)
            for index in reversed(range(52))
        ])
        product = Product.objects.get(sku="ALERT-000")
        StockItem.objects.create(product=product, location=self.storage, quantity=1)

        data = self.client.get("/api/reports/summary/").json()
        warehouse = data["warehouse_breakdown"][0]

        self.assertEqual(warehouse["low_stock_count"], 52)
        self.assertEqual(len(warehouse["low_stock"]), 50)
        self.assertEqual(
            [row["sku"] for row in warehouse["low_stock"]],
            [f"ALERT-{index:03}" for index in range(1, 51)],
        )
        self.assertEqual(len(data["low_stock"]), 50)
        self.assertEqual(data["low_stock_count"], 52)
        self.assertEqual(
            [row["sku"] for row in data["low_stock"]],
            [f"ALERT-{index:03}" for index in range(1, 51)],
        )

    def test_warehouse_breakdown_query_count_does_not_grow_per_warehouse(self):
        with CaptureQueriesContext(connection) as initial:
            response = self.client.get("/api/reports/summary/")
        self.assertEqual(response.status_code, 200)
        Warehouse.objects.bulk_create([
            Warehouse(code=f"EXTRA-{index}", name="Extra") for index in range(5)
        ])
        with CaptureQueriesContext(connection) as expanded:
            response = self.client.get("/api/reports/summary/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["warehouse_breakdown"]), 6)
        self.assertEqual(len(expanded), len(initial))

    def test_summary_without_warehouses_has_empty_breakdown(self):
        Location.objects.all().delete()
        Warehouse.objects.all().delete()
        data = self.client.get("/api/reports/summary/").json()
        self.assertEqual(data["warehouse_breakdown"], [])
        self.assertEqual(data["total_units"], 0)
        self.assertEqual(data["warehouses"], 0)

    def test_health(self):
        self.assertEqual(self.client.get("/api/health/").json(), {"status": "ok"})

    def test_health_reports_unavailable_database(self):
        with mock.patch("inventory.views.connection.cursor", side_effect=OperationalError("down")):
            response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    def test_api_returns_json_only(self):
        response = self.client.get("/api/products/", HTTP_ACCEPT="text/html")
        self.assertEqual(response["Content-Type"], "application/json")
