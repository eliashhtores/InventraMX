from django.conf import settings

from inventory.models import Lot, StockItem

from .test_api import ApiTestCase


class SpanishApiTests(ApiTestCase):
    def test_default_language_and_builtin_validation_are_spanish(self):
        self.assertEqual(settings.LANGUAGE_CODE, "es-mx")
        response = self.client.post("/api/products/", {}, format="json", HTTP_ACCEPT_LANGUAGE="en-US")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["sku"], ["Este campo es requerido."])
        response = self.move(movement_type="inbound", to_location=self.receiving.pk, quantity=0)
        self.assertEqual(response.status_code, 400)
        self.assertIn("mayor o igual", response.json()["quantity"][0])

    def test_builtin_date_and_uniqueness_errors_are_spanish(self):
        response = self.client.get("/api/movements/", {"occurred_at_after": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("formato", response.json()["occurred_at_after"][0].lower())
        response = self.client.post("/api/products/", {
            "sku": self.product.sku, "name": "Duplicate",
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["sku"], ["Ya existe un/a producto con este/a SKU."])

    def test_single_and_batch_stock_errors_are_spanish_and_keep_shapes(self):
        self.move(movement_type="inbound", to_location=self.receiving.pk, quantity=2)
        payload = {
            "movement_type": "outbound", "product": self.product.pk,
            "from_location": self.receiving.pk, "quantity": 3,
        }
        expected = f"Existencias insuficientes en {self.receiving}: 2 disponibles."
        response = self.client.post("/api/movements/", payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"quantity": expected})
        response = self.client.post("/api/movements/batch/", [payload], format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), [{"quantity": expected}])
        self.assertEqual(self.qty(self.receiving), 2)

    def test_reservation_errors_are_spanish(self):
        self.move(movement_type="inbound", to_location=self.receiving.pk, quantity=2)
        stock = StockItem.objects.get(product=self.product, location=self.receiving)
        payload = {"stock_item": stock.pk, "quantity": 3, "reference": "ORDER"}
        response = self.client.post("/api/reservations/", payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["quantity"], "Existencias disponibles insuficientes: 2 disponibles.")
        payload["quantity"] = 1
        reservation = self.client.post("/api/reservations/", payload, format="json").json()["id"]
        url = f"/api/reservations/{reservation}/release/"
        self.assertEqual(self.client.post(url).status_code, 200)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "Solo se pueden liberar o surtir reservas activas.")

    def test_lot_and_protected_delete_errors_are_spanish(self):
        self.product.lot_tracked = True
        self.product.save()
        response = self.move(movement_type="inbound", to_location=self.receiving.pk, quantity=1)
        self.assertEqual(response.status_code, 400)
        self.assertIn("número de lote", response.json()["lot"][0])
        lot = Lot.objects.create(product=self.product, lot_number="000123", expiry_date="2027-12-31")
        self.move(movement_type="inbound", to_location=self.receiving.pk, quantity=1, lot=lot.pk)
        response = self.client.patch(f"/api/lots/{lot.pk}/", {"lot_number": "NEW"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("No se pueden modificar", response.json()["lot_number"][0])
        response = self.client.delete(f"/api/lots/{lot.pk}/")
        self.assertEqual(response.status_code, 409)
        self.assertIn("No se puede eliminar", response.json()["detail"])

    def test_query_errors_are_spanish_and_identifiers_are_unchanged(self):
        response = self.client.get("/api/stock/", {"warehouse": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["warehouse"], "Debe ser un identificador numérico.")
        response = self.client.get("/api/movements/", {"movement_type": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Debe ser uno de", response.json()["movement_type"])
        response = self.client.get("/api/movements/", {
            "occurred_at_after": "2026-11-06T00:00:00Z",
            "occurred_at_before": "2026-11-05T00:00:00Z",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["occurred_at_before"],
            ["Debe ser igual o posterior a occurred_at_after."],
        )
