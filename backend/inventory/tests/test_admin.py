from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from inventory.models import Location, Product, Reservation, StockItem, StockMovement, Warehouse
from inventory.services import record_movement, reserve_stock


class AdminTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="test-password",
        )

    def test_admin_requires_login(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response.url)

    def test_admin_lists_inventory_models(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(reverse("admin:inventory_product_changelist")).status_code,
            200,
        )

    def test_non_superusers_cannot_add_or_delete_even_with_model_permissions(self):
        staff = get_user_model().objects.create_user(username="staff", is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(content_type__app_label="inventory"))
        self.client.force_login(staff)
        stock, movement, reservation = self.create_inventory()
        for model, obj in (("stockitem", stock), ("stockmovement", movement), ("reservation", reservation)):
            with self.subTest(model=model):
                self.assertEqual(self.client.get(reverse(f"admin:inventory_{model}_add")).status_code, 403)
                self.assertEqual(
                    self.client.post(reverse(f"admin:inventory_{model}_delete", args=[obj.pk]), {"post": "yes"}).status_code,
                    403,
                )

    def test_superuser_can_add_and_delete_without_assigned_permissions(self):
        self.client.force_login(self.admin_user)
        self.assertFalse(self.admin_user.user_permissions.exists())
        stock, movement, reservation = self.create_inventory()
        for model in ("stockitem", "stockmovement", "reservation"):
            self.assertEqual(self.client.get(reverse(f"admin:inventory_{model}_add")).status_code, 200)
        other_location = Location.objects.create(warehouse=stock.location.warehouse, code="A-02")
        response = self.client.post(reverse("admin:inventory_stockitem_add"), {
            "product": stock.product_id, "location": other_location.pk, "lot": "", "quantity": 7, "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        added_stock = StockItem.objects.get(location=other_location)
        response = self.client.post(reverse("admin:inventory_stockmovement_add"), {
            "product": stock.product_id, "lot": "", "movement_type": "inbound", "quantity": 3,
            "from_location": "", "to_location": other_location.pk, "reference": "ADMIN-ADD", "notes": "",
            "occurred_at_0": "2026-01-02", "occurred_at_1": "12:00:00", "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        added_movement = StockMovement.objects.get(reference="ADMIN-ADD")
        added_stock.refresh_from_db()
        self.assertEqual(added_stock.quantity, 7)
        response = self.client.post(reverse("admin:inventory_reservation_add"), {
            "stock_item": added_stock.pk, "quantity": 1, "reference": "ADMIN-HOLD",
            "status": "active", "movement": "", "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        added_reservation = Reservation.objects.get(reference="ADMIN-HOLD")
        for model, obj in (
            ("reservation", added_reservation), ("stockmovement", added_movement), ("stockitem", added_stock),
            ("reservation", reservation), ("stockmovement", movement), ("stockitem", stock),
        ):
            response = self.client.post(
                reverse(f"admin:inventory_{model}_delete", args=[obj.pk]), {"post": "yes"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertFalse(type(obj).objects.filter(pk=obj.pk).exists())

    def test_superuser_cannot_delete_stock_protected_by_reservations(self):
        self.client.force_login(self.admin_user)
        stock, _, _ = self.create_inventory()
        response = self.client.post(
            reverse("admin:inventory_stockitem_delete", args=[stock.pk]), {"post": "yes"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["protected"])
        self.assertTrue(StockItem.objects.filter(pk=stock.pk).exists())

    def create_inventory(self):
        warehouse = Warehouse.objects.create(code="ADMIN", name="Admin")
        location = Location.objects.create(warehouse=warehouse, code="A-01")
        product = Product.objects.create(sku="ADMIN-1", name="Admin product")
        movement = record_movement(
            product=product, to_location=location, movement_type="inbound", quantity=10,
        )
        stock = StockItem.objects.get(product=product)
        reservation = reserve_stock(stock_item=stock, quantity=2, reference="ORDER-1")
        return stock, movement, reservation

    def test_stock_quantity_can_be_edited_directly_without_ledger_adjustment(self):
        self.client.force_login(self.admin_user)
        stock, movement, _ = self.create_inventory()
        response = self.client.post(
            reverse("admin:inventory_stockitem_change", args=[stock.pk]),
            {"product": stock.product_id, "location": stock.location_id, "lot": "", "quantity": 15, "_save": "Save"},
        )
        self.assertEqual(response.status_code, 302)
        stock.refresh_from_db()
        movement.refresh_from_db()
        self.assertEqual(stock.quantity, 15)
        self.assertEqual(movement.quantity, 10)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_movement_can_be_edited_directly_without_recalculating_stock(self):
        self.client.force_login(self.admin_user)
        stock, movement, _ = self.create_inventory()
        created_at = movement.created_at
        response = self.client.post(
            reverse("admin:inventory_stockmovement_change", args=[movement.pk]),
            {
                "product": stock.product_id, "lot": "", "movement_type": "inbound", "quantity": 12,
                "from_location": "", "to_location": stock.location_id, "reference": "REPAIRED",
                "notes": "Manual repair", "occurred_at_0": "2026-01-02", "occurred_at_1": "12:00:00",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        movement.refresh_from_db()
        stock.refresh_from_db()
        self.assertEqual(movement.quantity, 12)
        self.assertEqual(movement.reference, "REPAIRED")
        self.assertEqual(movement.occurred_at.date().isoformat(), "2026-01-02")
        self.assertEqual(movement.created_at, created_at)
        self.assertEqual(stock.quantity, 10)

    def test_reservation_can_be_edited_directly_without_service_side_effects(self):
        self.client.force_login(self.admin_user)
        stock, _, reservation = self.create_inventory()
        url = reverse("admin:inventory_reservation_change", args=[reservation.pk])
        response = self.client.post(
            url,
            {"stock_item": stock.pk, "quantity": 20, "reference": "MANUAL", "status": "active", "movement": "", "_save": "Save"},
        )
        self.assertEqual(response.status_code, 302)
        reservation.refresh_from_db()
        stock.refresh_from_db()
        self.assertEqual(reservation.quantity, 20)
        self.assertEqual(reservation.reference, "MANUAL")
        self.assertEqual(stock.available_quantity, -10)
        response = self.client.post(
            url,
            {"stock_item": stock.pk, "quantity": 20, "reference": "MANUAL", "status": "released", "movement": "", "_save": "Save"},
        )
        self.assertEqual(response.status_code, 302)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, "released")
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_model_validation_still_applies_to_admin_edits(self):
        self.client.force_login(self.admin_user)
        stock, _, reservation = self.create_inventory()
        response = self.client.post(
            reverse("admin:inventory_reservation_change", args=[reservation.pk]),
            {"stock_item": stock.pk, "quantity": 0, "reference": "INVALID", "status": "active", "movement": "", "_save": "Save"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("quantity", response.context["adminform"].form.errors)
        reservation.refresh_from_db()
        self.assertEqual(reservation.quantity, 2)

    def test_editing_requires_each_models_change_permission(self):
        staff = get_user_model().objects.create_user(username="viewer", is_staff=True)
        stock, movement, reservation = self.create_inventory()
        self.client.force_login(staff)
        for model, obj in (("stockitem", stock), ("stockmovement", movement), ("reservation", reservation)):
            with self.subTest(model=model):
                url = reverse(f"admin:inventory_{model}_change", args=[obj.pk])
                self.assertEqual(self.client.get(url).status_code, 403)
                staff.user_permissions.add(Permission.objects.get(codename=f"view_{model}"))
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context["has_change_permission"])
                self.assertEqual(self.client.post(url, {}).status_code, 403)
                staff.user_permissions.add(Permission.objects.get(codename=f"change_{model}"))
                self.assertTrue(self.client.get(url).context["has_change_permission"])
