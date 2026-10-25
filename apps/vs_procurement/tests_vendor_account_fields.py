"""The three vendor accounting defaults have independent field switches."""

from vs_rbac.tests.helpers import set_field_access

from .tests_vendor_field_access import _VendorAccessFixture


ACCOUNT_FIELDS = {
    "payable_account": ("payable_account_id", "payable_code"),
    "default_expense_account": ("default_expense_account_id", "default_expense_code"),
    "default_wht_tax_code": ("default_wht_tax_code_id", "default_wht_tax_code_value"),
}


class VendorAccountingFieldAccessTests(_VendorAccessFixture):
    def test_each_default_is_open_then_hidden_independently(self):
        for user, role, books in (
            (self.officer_user, self.officer_role, self.multi),
            (self.flat_user, self.flat_role, self.flat),
        ):
            for field, names in ACCOUNT_FIELDS.items():
                with self.subTest(tenant=books.entity.code, field=field):
                    detail_path = f"vendors/{books.vendor.pk}/"
                    shown = self.call(user, detail_path, books=books)
                    self.assertEqual(shown.status_code, 200)
                    for name in names:
                        self.assertIn(name, shown.data["data"])
                    set_field_access(
                        role, f"procurement.vendor.{field}", read=False, write=False,
                    )
                    hidden = self.call(user, detail_path, books=books)
                    for name in names:
                        self.assertNotIn(name, hidden.data["data"])
                    for other, other_names in ACCOUNT_FIELDS.items():
                        if other != field:
                            for name in other_names:
                                self.assertIn(name, hidden.data["data"])
                    set_field_access(
                        role, f"procurement.vendor.{field}", read=True, write=True,
                    )

    def test_read_only_default_refuses_alternate_write_paths(self):
        for field in ACCOUNT_FIELDS:
            with self.subTest(field=field):
                set_field_access(
                    self.officer_role, f"procurement.vendor.{field}",
                    read=True, write=False,
                )
                patch = self.call(
                    self.officer_user, f"vendors/{self.multi.vendor.pk}/", "patch",
                    {field: "5300"},
                )
                self.assertEqual(patch.status_code, 403)
                create = self.call(self.officer_user, "vendors/", "post", {
                    "code": f"NEW{field[:4].upper()}", "name": "New Vendor", field: "5300",
                })
                self.assertEqual(create.status_code, 403)
                set_field_access(
                    self.officer_role, f"procurement.vendor.{field}",
                    read=True, write=True,
                )
