"""Vendor active state stays behind its own read and write switch."""

from vs_rbac.tests.helpers import set_field_access

from .tests_vendor_field_access import _VendorAccessFixture


class VendorActiveFieldAccessTests(_VendorAccessFixture):
    def test_default_read_and_hidden_alternate_paths(self):
        for user, role, books in (
            (self.officer_user, self.officer_role, self.multi),
            (self.flat_user, self.flat_role, self.flat),
        ):
            with self.subTest(tenant=books.entity.code):
                detail_path = f"vendors/{books.vendor.pk}/"
                shown = self.call(user, detail_path, books=books)
                self.assertEqual(shown.status_code, 200)
                self.assertTrue(shown.data["data"]["is_active"])
                set_field_access(
                    role, "procurement.vendor.is_active", read=False, write=False,
                )
                detail = self.call(user, detail_path, books=books)
                listed = self.call(user, "vendors/", books=books)
                summary = self.call(user, "vendors/summary/", books=books)
                self.assertNotIn("is_active", detail.data["data"])
                self.assertNotIn("is_active", listed.data["data"][0])
                self.assertNotIn("active", summary.data["data"])
                self.assertNotIn("inactive", summary.data["data"])
                self.assertEqual(
                    self.call(user, "vendors/?is_active=true", books=books).status_code,
                    403,
                )

    def test_read_only_state_refuses_patch(self):
        set_field_access(
            self.officer_role, "procurement.vendor.is_active", read=True, write=False,
        )
        response = self.call(
            self.officer_user, f"vendors/{self.multi.vendor.pk}/", "patch",
            {"is_active": False},
        )
        self.assertEqual(response.status_code, 403)
