"""Invoice fields follow their switches on the list, detail and create paths."""

from django.test import SimpleTestCase

from core.test_utils import TenantAPIClient
from vs_exports.catalogue import get_dataset
from vs_finance.models import FinanceAuditLog, Invoice
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .tests_bank_document_rework import _ReworkFixture


class FinanceExportFieldAccessTests(SimpleTestCase):
    def test_invoice_settlement_columns_need_total_and_settlement(self):
        dataset = get_dataset("finance.customer_invoices")
        columns = dataset.describe(readable=lambda key: key != "finance.invoice.total")
        self.assertNotIn("amount_paid", {field["id"] for field in columns["fields"]})
        columns = dataset.describe(readable=lambda key: key != "finance.invoice.settlement")
        self.assertNotIn("amount_credited", {field["id"] for field in columns["fields"]})

    def test_journal_filters_cannot_read_hidden_lines_or_narration(self):
        dataset = get_dataset("finance.gl_postings")
        readable = lambda key: key not in {
            "finance.journal.lines", "finance.journal.narration",
        }
        columns = dataset.describe(readable=readable)
        self.assertNotIn("debit", {field["id"] for field in columns["fields"]})
        account_filter = next(f for f in dataset.filters if f.id == "account")
        search_filter = next(f for f in dataset.filters if f.id == "search")
        self.assertFalse(dataset.filter_readable(account_filter, readable))
        self.assertNotIn("entry__narration", {
            path for path, _ in dataset.readable_searches(search_filter, readable)
        })


class InvoiceFieldAccessTests(_ReworkFixture):
    """One billed customer in Ikeja and a school with one branch."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("finance.invoice")
        cls.customer_row = cls.customer(cls.books, "FLS-INV", cls.ikeja)
        cls.invoice_row = cls.invoice(cls.books, cls.customer_row, cls.ikeja)
        cls.reader = cls.grant(
            cls.user_for(cls.tenant, "invoice-reader@field.test"),
            "finance.invoice.view", "finance.invoice.create", "finance.audit.view",
            tenant=cls.tenant, role_key="invoice-reader",
        )
        cls.hidden = cls.grant(
            cls.user_for(cls.tenant, "invoice-hidden@field.test"),
            "finance.invoice.view", "finance.invoice.create", "finance.audit.view",
            tenant=cls.tenant, role_key="invoice-hidden",
        )
        hidden_role = TenantRoleTemplate.objects.get(tenant=cls.tenant, key="invoice-hidden")
        set_field_access(hidden_role, "finance.invoice.unit_price", read=False, write=False)
        set_field_access(hidden_role, "finance.invoice.total", read=False, write=False)
        set_field_access(hidden_role, "finance.invoice.reference", read=False, write=False)
        cls.read_only = cls.grant(
            cls.user_for(cls.tenant, "invoice-readonly@field.test"),
            "finance.invoice.view", "finance.invoice.create",
            tenant=cls.tenant, role_key="invoice-readonly",
        )
        readonly_role = TenantRoleTemplate.objects.get(
            tenant=cls.tenant, key="invoice-readonly")
        set_field_access(readonly_role, "finance.invoice.unit_price",
                         read=True, write=False)

    def detail(self, user):
        response = TenantAPIClient(user=user).get(
            self.url(f"invoices/{self.invoice_row.pk}/"))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_default_access_keeps_invoice_fields_visible(self):
        detail = self.detail(self.reader)

        self.assertIn("total", detail["invoice"])
        self.assertIn("unit_price", detail["lines"][0])
        self.assertIn("reference", detail["invoice"])

    def test_hidden_unit_price_and_total_leave_no_line_restating_value(self):
        detail = self.detail(self.hidden)

        self.assertNotIn("unit_price", detail["lines"][0])
        self.assertNotIn("line_total", detail["lines"][0])
        self.assertNotIn("tax_amount", detail["lines"][0])
        self.assertNotIn("total", detail["invoice"])
        self.assertNotIn("total", detail["summary"])
        self.assertNotIn("reference", detail["invoice"])

    def test_summary_does_not_restate_a_hidden_invoice_total(self):
        response = TenantAPIClient(user=self.hidden).get(self.url("invoices/summary/"))
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertNotIn("total_invoiced", data["kpis"])
        self.assertNotIn("overdue_balance", data["kpis"])
        self.assertNotIn("collection_rate", data["kpis"])
        self.assertNotIn("total", data["totals"])
        self.assertNotIn("outstanding", data["totals"])
        self.assertNotIn("invoiced", data["monthly"][0])

    def test_audit_snapshot_does_not_restate_a_hidden_invoice_total(self):
        log = FinanceAuditLog.objects.create(
            entity=self.books, branch=self.ikeja, action="INVOICE_POSTED",
            target_type="Invoice", target_id=str(self.invoice_row.pk),
            document_number=self.invoice_row.document_number,
            message="Invoice posted.", before={"total": 1_000, "reference": "TERM-1"},
            after={"total": 2_000, "reference": "TERM-2"},
        )
        response = TenantAPIClient(user=self.hidden).get(
            self.url(f"audit-logs/{log.pk}/"))
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertNotIn("total", data["before"])
        self.assertNotIn("total", data["after"])
        self.assertNotIn("reference", data["before"])

    def test_create_refuses_a_unit_price_the_role_may_not_write(self):
        before = Invoice.objects.filter(entity=self.books).count()
        response = TenantAPIClient(user=self.read_only).post(
            self.url("invoices/"), {
                "customer": self.customer_row.pk,
                "invoice_date": "2026-01-15", "post": False,
                "lines": [{"revenue_account": "4000", "unit_price": 1_000}],
            }, format="json")

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertEqual(Invoice.objects.filter(entity=self.books).count(), before)
