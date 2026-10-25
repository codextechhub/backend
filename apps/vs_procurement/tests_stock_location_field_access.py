"""Stock location details and actions follow their Field Access switches."""

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .models import StockLocation
from .tests_vendor_field_access import _VendorAccessFixture


class StockLocationFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.stock_keys = install_declared_fields("procurement.stock")
        for user, tenant in (
            (cls.officer_user, cls.multi_tenant),
            (cls.flat_user, cls.flat_tenant),
        ):
            for key in (
                "procurement.stock.view", "procurement.stock.create",
                "procurement.stock.update",
            ):
                cls.grant(user, key, tenant=tenant, role_key="officer", branch=None)
        cls.multi.store = StockLocation.objects.create(
            entity=cls.multi.entity, branch=cls.lekki, code="MAIN",
            name="Main Store", description="Textbooks", is_default=True,
        )
        cls.flat.store = StockLocation.objects.create(
            entity=cls.flat.entity, branch=cls.flat_tenant.branches.first(),
            code="MAIN", name="Main Store", description="Textbooks", is_default=True,
        )

    def test_default_read_and_hidden_values_on_list_and_detail(self):
        for user, role, books in (
            (self.officer_user, self.officer_role, self.multi),
            (self.flat_user, self.flat_role, self.flat),
        ):
            with self.subTest(tenant=books.entity.code):
                detail = self.call(user, f"stock-locations/{books.store.pk}/", books=books)
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(detail.data["data"]["description"], "Textbooks")
                for key in self.stock_keys:
                    set_field_access(role, key, read=False, write=False)
                detail = self.call(user, f"stock-locations/{books.store.pk}/", books=books)
                listed = self.call(user, "stock-locations/", books=books)
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(listed.status_code, 200)
                for row in (detail.data["data"], listed.data["data"][0]):
                    self.assertNotIn("description", row)
                    self.assertNotIn("is_default", row)
                    self.assertNotIn("is_active", row)
                self.assertEqual(
                    self.call(user, "stock-locations/?is_active=true", books=books).status_code,
                    403,
                )

    def test_read_only_fields_refuse_create_and_update(self):
        for key in self.stock_keys:
            set_field_access(self.officer_role, key, read=True, write=False)
        created = self.call(self.officer_user, "stock-locations/", "post", {
            "code": "ANNEX", "name": "Annex Store", "description": "Paper",
        })
        self.assertEqual(created.status_code, 403)
        updated = self.call(self.officer_user, f"stock-locations/{self.multi.store.pk}/", "patch", {
            "is_active": False,
        })
        self.assertEqual(updated.status_code, 403)
