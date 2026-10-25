"""Category accounting and status values follow their own Field Access switches."""

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .tests_vendor_field_access import _VendorAccessFixture


class VendorCategoryFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.category_keys = install_declared_fields("procurement.category")
        for user, tenant in (
            (cls.officer_user, cls.multi_tenant),
            (cls.flat_user, cls.flat_tenant),
        ):
            for key in (
                "procurement.category.view",
                "procurement.category.create",
                "procurement.category.update",
            ):
                cls.grant(user, key, tenant=tenant, role_key="officer", branch=None)

    def test_default_access_and_hidden_values_on_list_and_detail(self):
        for user, role, books in (
            (self.officer_user, self.officer_role, self.multi),
            (self.flat_user, self.flat_role, self.flat),
        ):
            with self.subTest(tenant=books.entity.code):
                detail_path = f"categories/{books.category.pk}/"
                initial = self.call(user, detail_path, books=books)
                self.assertEqual(initial.status_code, 200)
                self.assertIn("is_active", initial.data["data"])
                self.assertIn("default_expense_code", initial.data["data"])

                set_field_access(role, *self.category_keys, read=False, write=False)
                detail = self.call(user, detail_path, books=books)
                listed = self.call(user, "categories/", books=books)
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(listed.status_code, 200)
                for row in (detail.data["data"], listed.data["data"][0]):
                    self.assertNotIn("is_active", row)
                    self.assertNotIn("default_expense_account_id", row)
                    self.assertNotIn("default_expense_code", row)
                self.assertEqual(
                    self.call(user, "categories/?is_active=true", books=books).status_code,
                    403,
                )

    def test_read_only_fields_refuse_create_and_update(self):
        for key in self.category_keys:
            set_field_access(self.officer_role, key, read=True, write=False)
        created = self.call(self.officer_user, "categories/", "post", {
            "code": "PAPER", "name": "Paper", "is_active": True,
        })
        self.assertEqual(created.status_code, 403)
        updated = self.call(self.officer_user, f"categories/{self.multi.category.pk}/", "patch", {
            "default_expense_account": None,
        })
        self.assertEqual(updated.status_code, 403)

    def test_foreign_category_cannot_be_read_through_id(self):
        response = self.call(self.officer_user, f"categories/{self.foreign.category.pk}/")
        self.assertEqual(response.status_code, 404)
