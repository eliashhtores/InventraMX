from unittest import mock

from django.db import OperationalError
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import Location, Product, StockItem, StockMovement, Warehouse


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
        self.assertEqual(inbound.json()["to_location"], ["Warehouse is inactive."])

        outbound = self.move(movement_type="outbound", from_location=self.storage.id, quantity=1)
        self.assertEqual(outbound.status_code, 400)
        self.assertEqual(outbound.json()["from_location"], ["Warehouse is inactive."])
        self.assertEqual(self.qty(self.storage), 3)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_movements_are_immutable(self):
        movement_id = self.move(movement_type="inbound", to_location=self.storage.id, quantity=1).json()["id"]
        self.assertEqual(self.client.delete(f"/api/movements/{movement_id}/").status_code, 405)
        self.assertEqual(
            self.client.patch(f"/api/movements/{movement_id}/", {"quantity": 9}, format="json").status_code, 405
        )


class ReportTests(ApiTestCase):
    def test_summary_report(self):
        self.move(movement_type="inbound", to_location=self.storage.id, quantity=3)
        data = self.client.get("/api/reports/summary/").json()
        self.assertEqual(data["total_units"], 3)
        self.assertEqual(data["movements"]["inbound"], 1)
        self.assertEqual([p["sku"] for p in data["low_stock"]], ["SKU-1"])

    def test_low_stock_ignores_products_without_reorder_level(self):
        Product.objects.create(sku="SKU-0", name="No alert", reorder_level=0)
        data = self.client.get("/api/reports/summary/").json()
        self.assertEqual([p["sku"] for p in data["low_stock"]], ["SKU-1"])

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
