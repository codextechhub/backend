"""Field Access on the banking and cash defaults of the finance settings screen.

Corona's bursar may read "Apply bank transfers to open bills automatically" and
not change it; its auditor may not read it at all. The default is served to
neither in a form that lets them change it, the history and the audit trail
leave it out for the auditor, and a write that would change it is refused
before anything is saved.
"""
from __future__ import annotations

from vs_finance.models import FinanceBankingSettings

from .tests_accruals_field_access import SETTINGS, _AccrualsFieldAccess

FIELD = f"{SETTINGS}.apply_transfers_automatically"


class BankingSettingsFieldAccessTests(_AccrualsFieldAccess):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(cls.tenant, "bk-open")
        cls.no_transfers = cls.staff(cls.tenant, "bk-no-transfers", hide=[FIELD])
        cls.read_only = cls.staff(cls.tenant, "bk-read-only", read_only=[FIELD])

    path = "settings/banking/"

    def get(self, user):
        return self.api(user).get(self.url(self.path)).data["data"]

    def patch(self, user, body):
        return self.api(user).patch(self.url(self.path), body, format="json")

    def test_a_hidden_default_leaves_the_settings_and_its_consumer_entry(self):
        data = self.get(self.no_transfers)
        self.assertNotIn("apply_transfers_automatically", data["settings"])
        self.assertNotIn("apply_transfers_automatically", data["consumers"])
        self.assertIn("default_receipt_allocation_strategy", data["settings"])
        full = self.get(self.open)
        self.assertTrue(full["settings"]["apply_transfers_automatically"])
        self.assertIn("apply_transfers_automatically", full["consumers"])

    def test_a_read_only_default_is_named_so_and_refuses_a_change(self):
        settings = self.get(self.read_only)["settings"]
        self.assertIn("apply_transfers_automatically", settings["_read_only_fields"])
        refused = self.patch(self.read_only, {"apply_transfers_automatically": False})
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(self.code(refused), "field_write_denied")
        self.assertFalse(FinanceBankingSettings.objects.filter(entity=self.books).exists())

    def test_sending_it_back_as_stored_is_not_a_change(self):
        saved = self.patch(self.read_only, {
            "apply_transfers_automatically": True,
            "default_receipt_allocation_strategy": "largest",
        })
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(
            FinanceBankingSettings.objects.get(entity=self.books).default_receipt_allocation_strategy,
            "largest",
        )

    def test_a_hidden_default_cannot_be_changed_by_naming_it(self):
        refused = self.patch(self.no_transfers, {"apply_transfers_automatically": False})
        self.assertEqual(refused.status_code, 403, refused.data)

    def test_the_history_and_audit_trail_leave_a_hidden_change_out(self):
        self.assertEqual(self.patch(self.open, {"apply_transfers_automatically": False}).status_code, 200)
        self.assertEqual(len(self.get(self.open)["history"]), 1)
        self.assertEqual(self.get(self.no_transfers)["history"], [])
        rows = self.api(self.no_transfers).get(self.url("audit-logs/")).data["data"]
        for row in rows:
            if row["target_type"] == "FinanceBankingSettings":
                self.assertNotIn("apply_transfers_automatically", row["after"])
