"""Field Access on the rest of Finance settings: mappings, documents, calendar, payroll, retention.

Corona's auditor role may not read the receivables account mapping, the days
until an invoice is due, the pension rate or how long records are kept; its
bursar role may read them and not change them. Each section leaves a hidden
value out of what it serves and its history, and refuses a write that would
change a value the caller may not write before anything is saved, while a
value sent back exactly as stored is not a change.
"""
from __future__ import annotations

from vs_finance.models import FinanceDocumentSettings, FinancePayrollSettings

from .tests_accruals_field_access import SETTINGS, _AccrualsFieldAccess

K = f"{SETTINGS}."


class SettingsSectionsFieldAccessTests(_AccrualsFieldAccess):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        hidden = [K + "mapping_accounts_receivable", K + "default_invoice_due_days",
                  K + "employee_pension_rate_bps", K + "retention_years",
                  K + "next_year_lead_days"]
        cls.open = cls.staff(cls.tenant, "ss-open")
        cls.auditor = cls.staff(cls.tenant, "ss-auditor", hide=hidden)
        cls.bursar = cls.staff(cls.tenant, "ss-bursar", read_only=hidden)

    def get(self, user, path):
        return self.api(user).get(self.url(path)).data["data"]

    def patch(self, user, path, body):
        return self.api(user).patch(self.url(path), body, format="json")

    # -- account mappings ---------------------------------------------------- #

    def test_a_hidden_mapping_leaves_the_list_and_a_read_only_one_is_named(self):
        hidden = {row["key"] for row in self.get(self.auditor, "settings/account-mappings/")["mappings"]}
        self.assertNotIn("ACCOUNTS_RECEIVABLE", hidden)
        self.assertIn("CASH_BANK", hidden)
        data = self.get(self.bursar, "settings/account-mappings/")
        self.assertIn("ACCOUNTS_RECEIVABLE", {row["key"] for row in data["mappings"]})
        self.assertEqual(data["read_only_keys"], ["ACCOUNTS_RECEIVABLE"])

    def test_a_read_only_mapping_refuses_another_account_and_takes_its_own(self):
        rows = {row["key"]: row for row in self.get(self.bursar, "settings/account-mappings/")["mappings"]}
        own = rows["ACCOUNTS_RECEIVABLE"]["account"]["id"]
        self.assertEqual(self.patch(self.bursar, "settings/account-mappings/", {
            "mappings": {"ACCOUNTS_RECEIVABLE": own}}).status_code, 200)
        refused = self.patch(self.bursar, "settings/account-mappings/", {
            "mappings": {"ACCOUNTS_RECEIVABLE": rows["CASH_BANK"]["account"]["id"]}})
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(self.code(refused), "field_write_denied")

    # -- documents, calendar, payroll ---------------------------------------- #

    def test_hidden_section_values_leave_each_screen(self):
        for path, name in (("settings/documents/", "default_invoice_due_days"),
                           ("settings/calendar/", "next_year_lead_days"),
                           ("settings/payroll/", "employee_pension_rate_bps")):
            with self.subTest(path=path):
                data = self.get(self.auditor, path)
                self.assertNotIn(name, data["settings"])
                self.assertNotIn(name, data["consumers"])
                self.assertIn(name, self.get(self.open, path)["settings"])
                self.assertIn(name, self.get(self.bursar, path)["settings"]["_read_only_fields"])

    def test_a_read_only_value_refuses_a_change_before_anything_is_saved(self):
        refused = self.patch(self.bursar, "settings/documents/", {
            "default_invoice_due_days": 45, "auto_post_manual_invoices": True})
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertFalse(FinanceDocumentSettings.objects.filter(entity=self.books).exists())
        refused = self.patch(self.bursar, "settings/payroll/", {"employee_pension_rate_bps": 900})
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertFalse(FinancePayrollSettings.objects.filter(entity=self.books).exists())

    def test_sending_a_read_only_value_back_as_stored_is_not_a_change(self):
        stored = self.get(self.bursar, "settings/documents/")["settings"]
        saved = self.patch(self.bursar, "settings/documents/", {
            "default_invoice_due_days": stored["default_invoice_due_days"],
            "auto_post_manual_invoices": not stored["auto_post_manual_invoices"],
        })
        self.assertEqual(saved.status_code, 200, saved.data)

    def test_the_history_leaves_a_hidden_change_out(self):
        self.assertEqual(self.patch(self.open, "settings/documents/", {
            "default_invoice_due_days": 45}).status_code, 200)
        self.assertEqual(len(self.get(self.open, "settings/documents/")["history"]), 1)
        self.assertEqual(self.get(self.auditor, "settings/documents/")["history"], [])
        combined = self.api(self.auditor).get(self.url("settings/history/")).data["data"]["history"]
        for entry in combined:
            self.assertNotIn("default_invoice_due_days", entry["after"])

    # -- record retention ---------------------------------------------------- #

    def test_retention_follows_its_switches(self):
        hidden = self.get(self.auditor, "settings/records/")
        self.assertNotIn("retention_years", hidden)
        self.assertNotIn("effective_retention_years", hidden)
        self.assertIn("statutory_years", hidden)
        self.assertIn("retention_years", self.get(self.bursar, "settings/records/")["_read_only_fields"])
        refused = self.patch(self.bursar, "settings/records/", {"retention_years": 12})
        self.assertEqual(refused.status_code, 403, refused.data)
