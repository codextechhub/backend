"""Procurement settings and their audit history honor the same field switches."""

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .tests_vendor_field_access import _VendorAccessFixture


class ProcurementSettingsFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.settings_keys = install_declared_fields("procurement.settings")
        for user, tenant in (
            (cls.officer_user, cls.multi_tenant),
            (cls.flat_user, cls.flat_tenant),
        ):
            for key in ("procurement.settings.view", "procurement.settings.update"):
                cls.grant(user, key, tenant=tenant, role_key="officer", branch=None)

    def test_default_read_and_hidden_setting_in_get_patch_and_history(self):
        for user, role, books in (
            (self.officer_user, self.officer_role, self.multi),
            (self.flat_user, self.flat_role, self.flat),
        ):
            with self.subTest(tenant=books.entity.code):
                changed = self.call(user, "settings/", "patch", {
                    "default_payment_terms": "NET_60",
                    "price_tolerance_bps": 250,
                }, books=books)
                self.assertEqual(changed.status_code, 200)
                self.assertEqual(changed.data["data"]["settings"]["price_tolerance_bps"], 250)
                set_field_access(
                    role, "procurement.settings.price_tolerance_bps",
                    read=False, write=False,
                )
                read = self.call(user, "settings/", books=books)
                self.assertEqual(read.status_code, 200)
                self.assertNotIn("price_tolerance_bps", read.data["data"]["settings"])
                self.assertIn("default_payment_terms", read.data["data"]["settings"])
                entry = read.data["data"]["history"][0]
                self.assertNotIn("price_tolerance_bps", entry["before"])
                self.assertNotIn("price_tolerance_bps", entry["after"])
                self.assertNotIn("price_tolerance_bps", {change["field"] for change in entry["changes"]})
                alternate = self.call(user, "settings/history/", books=books)
                self.assertEqual(alternate.status_code, 200)
                self.assertNotIn("price_tolerance_bps", alternate.data["data"]["history"][0]["after"])

    def test_read_only_setting_refuses_patch_and_hidden_history_disappears(self):
        set_field_access(
            self.officer_role, "procurement.settings.default_payment_terms",
            read=True, write=False,
        )
        denied = self.call(self.officer_user, "settings/", "patch", {
            "default_payment_terms": "NET_60",
        })
        self.assertEqual(denied.status_code, 403)
        set_field_access(
            self.officer_role, "procurement.settings.history", read=False, write=False,
        )
        read = self.call(self.officer_user, "settings/")
        self.assertEqual(read.status_code, 200)
        self.assertNotIn("history", read.data["data"])
        alternate = self.call(self.officer_user, "settings/history/")
        self.assertEqual(alternate.status_code, 200)
        self.assertNotIn("history", alternate.data["data"])
