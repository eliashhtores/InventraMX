from inventory.models import Location, Product, StockItem, Warehouse
from inventory.services import reserve_stock

from .test_api import ApiTestCase


class LookupFilterTests(ApiTestCase):
    def test_warehouse_search_filters_activity_before_pagination(self):
        Warehouse.objects.bulk_create([
            Warehouse(code=f"FIND-{index:02}", name="Inactive", is_active=False)
            for index in range(25)
        ])
        active = Warehouse.objects.create(code="FIND-Z", name="Active")
        response = self.client.get("/api/warehouses/", {
            "is_active": "true", "search": "FIND", "limit": 1,
        })
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], active.pk)
        self.assertEqual(self.client.get("/api/warehouses/", {
            "is_active": "false", "search": "FIND",
        }).json()["count"], 25)
        self.assertEqual(self.client.get("/api/warehouses/").json()["count"], 27)
        self.assertEqual(self.client.get("/api/warehouses/", {"is_active": "invalid"}).status_code, 400)

    def test_locations_require_active_parent_warehouse_for_movement_lookups(self):
        inactive = Warehouse.objects.create(code="AA-INACTIVE", name="Inactive", is_active=False)
        Location.objects.bulk_create([
            Location(warehouse=inactive, code=f"LOOKUP-{index:02}")
            for index in range(25)
        ])
        selected = Location.objects.create(warehouse=self.warehouse, code="LOOKUP-Z", barcode="000123")
        Location.objects.create(warehouse=self.warehouse, code="LOOKUP-OFF", is_active=False)
        params = {
            "is_active": "true", "warehouse_is_active": "true", "search": "LOOKUP", "limit": 1,
        }
        response = self.client.get("/api/locations/", params)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], selected.pk)
        params["search"] = "000123"
        self.assertEqual(self.client.get("/api/locations/", params).json()["count"], 1)
        self.assertEqual(self.client.get("/api/locations/", {
            "warehouse_is_active": "invalid",
        }).status_code, 400)
        self.assertEqual(self.client.get("/api/locations/", {
            "warehouse_is_active": "false", "search": "LOOKUP",
        }).json()["count"], 25)

    def test_reservable_stock_excludes_unavailable_and_inactive_inventory_before_pagination(self):
        self.move(movement_type="inbound", to_location=self.receiving.pk, quantity=10)
        held = StockItem.objects.get(product=self.product, location=self.receiving)
        reserve_stock(stock_item=held, quantity=10, reference="HELD")
        available = StockItem.objects.create(product=self.product, location=self.storage, quantity=9)
        reserve_stock(stock_item=available, quantity=2, reference="PARTIAL")
        products = Product.objects.bulk_create([
            Product(sku=f"AAA-{index:02}", name="Inactive", is_active=False)
            for index in range(25)
        ])
        StockItem.objects.bulk_create([
            StockItem(product=product, location=self.storage, quantity=10) for product in products
        ])
        inactive_location = Location.objects.create(warehouse=self.warehouse, code="OFF", is_active=False)
        StockItem.objects.create(product=self.product, location=inactive_location, quantity=10)
        warehouse = Warehouse.objects.create(code="OFF", name="Inactive", is_active=False)
        location = Location.objects.create(warehouse=warehouse, code="A")
        StockItem.objects.create(product=self.product, location=location, quantity=10)

        response = self.client.get("/api/stock/", {"reservable": "true", "limit": 1})
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["count"], 1)
        row = response.json()["results"][0]
        self.assertEqual(row["id"], available.pk)
        self.assertEqual(row["available_quantity"], 7)
        self.assertEqual(self.client.get("/api/stock/", {
            "reservable": "true", "search": "missing",
        }).json()["count"], 0)
        self.assertEqual(self.client.get("/api/stock/", {
            "reservable": "true", "warehouse": warehouse.pk,
        }).json()["count"], 0)
        self.assertEqual(self.client.get("/api/stock/", {
            "reservable": "true", "search": self.product.sku,
        }).json()["count"], 1)
        self.assertEqual(self.client.get("/api/stock/", {
            "reservable": "true", "search": f"{self.product.sku} {self.warehouse.code}",
        }).json()["count"], 1)
        self.assertEqual(self.client.get("/api/stock/").json()["count"], 29)
        self.assertEqual(self.client.get("/api/stock/", {"reservable": "false"}).json()["count"], 29)
        self.assertEqual(self.client.get("/api/stock/", {"reservable": "invalid"}).status_code, 400)

    def test_product_lookup_combines_search_active_and_tracking_filters(self):
        Product.objects.create(sku="LOOKUP-OFF", name="Inactive", lot_tracked=True, is_active=False)
        Product.objects.create(sku="LOOKUP-NO", name="Ordinary")
        tracked = Product.objects.create(sku="LOOKUP-YES", name="Tracked", lot_tracked=True)
        response = self.client.get("/api/products/", {
            "is_active": "true", "lot_tracked": "true", "search": "LOOKUP", "limit": 20,
        })
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], tracked.pk)
