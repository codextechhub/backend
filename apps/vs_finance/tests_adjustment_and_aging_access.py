"""The AR aging report and the refunds and write-offs list, by key and by branch.

Corona runs Ikeja, Lekki and Yaba. The aging report is built from invoices, so a
bursar pinned to Ikeja who may run it sees Ikeja's customers and the school-wide
ones, never Lekki's debtors. The refunds and write-offs list serves two jobs: a
credit controller who handles write-offs holds only the write-off key and must
be able to open it, seeing write-offs and not the refunds paid back to parents;
a cashier who handles refunds sees the reverse.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.models import Refund, WriteOffRequest

from .tests_branch_scope import _FinanceBranchFixture


class _AccessFixture(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        e = self.books
        self.ikeja_customer = self.customer(e, "CIKJ", self.ikeja)
        self.lekki_customer = self.customer(e, "CLEK", self.lekki)
        self.shared_customer = self.customer(e, "CALL", None)
        self.ikeja_invoice = self.posted(self.invoice(e, self.ikeja_customer, self.ikeja))
        self.lekki_invoice = self.posted(self.invoice(e, self.lekki_customer, self.lekki))
        self.shared_invoice = self.posted(self.invoice(e, self.shared_customer, None))

    def posted(self, invoice):
        """Post ``invoice`` for real, its line pointed at a postable income account."""
        from vs_finance.models import Account, InvoiceLine
        from vs_finance.receivables import post_invoice

        InvoiceLine.objects.filter(invoice=invoice).update(
            revenue_account=Account.objects.get(entity=invoice.entity, code="4100"),
        )
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def client_holding(self, email, *keys, branch=None):
        user = self.grant(
            self.user_for(self.tenant, email), *keys,
            tenant=self.tenant, role_key=f"role-{email}", branch=branch,
        )
        return TenantAPIClient(user=user)


class ARAgingBranchTests(_AccessFixture):
    """The aging report answers under the reader's branches, export included."""

    def aging(self, client, suffix=""):
        return client.get(f"/v1/finance/reports/ar-aging/?entity={self.books.code}{suffix}")

    def test_a_branch_reader_sees_their_branch_and_school_wide_customers_only(self):
        client = self.client_holding("aging-ikeja@corona.test", "finance.report.view", branch=self.ikeja)
        data = self.aging(client).data["data"]

        codes = {row["code"] for row in data["rows"]}
        self.assertEqual(codes, {"CIKJ", "CALL"})
        self.assertTrue(data["narrowed"])
        self.assertEqual(data["total_net"]["kobo"], 2 * 100_000)

    def test_the_export_is_the_same_narrowed_report(self):
        client = self.client_holding("aging-export@corona.test", "finance.report.view", branch=self.ikeja)
        response = self.aging(client, "&export=csv")

        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content) if response.streaming else response.content
        self.assertIn(b"CIKJ", body)
        self.assertNotIn(b"CLEK", body)

    def test_a_school_wide_reader_sees_every_branch(self):
        client = self.client_holding("aging-hq@corona.test", "finance.report.view")
        data = self.aging(client).data["data"]

        self.assertEqual({row["code"] for row in data["rows"]}, {"CIKJ", "CLEK", "CALL"})
        self.assertFalse(data["narrowed"])


class AdjustmentListAccessTests(_AccessFixture):
    """Refund rows need the refund key; write-off rows need the write-off key."""

    def setUp(self):
        super().setUp()
        e = self.books
        today = datetime.date(2026, 1, 20)
        self.ikeja_refund = Refund.objects.create(
            entity=e, customer=self.ikeja_customer, branch=self.ikeja, refund_date=today, amount=5_000,
        )
        self.ikeja_writeoff = WriteOffRequest.objects.create(
            entity=e, invoice=self.ikeja_invoice, branch=self.ikeja, amount=10_000,
        )
        self.lekki_writeoff = WriteOffRequest.objects.create(
            entity=e, invoice=self.lekki_invoice, branch=self.lekki, amount=20_000,
        )

    def adjustments(self, client):
        return client.get(f"/v1/finance/ar-adjustments/?entity={self.books.code}")

    def test_a_reader_with_neither_key_is_refused(self):
        client = self.client_holding("no-adjust@corona.test", "finance.invoice.view")
        self.assertEqual(self.adjustments(client).status_code, 403)

    def test_a_write_off_holder_opens_the_list_and_sees_write_offs_only(self):
        client = self.client_holding("credit-control@corona.test", "finance.writeoff.view")
        response = self.adjustments(client)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual({row["kind"] for row in body["data"]}, {"WRITEOFF"})
        self.assertEqual(body["kinds"], ["WRITEOFF"])
        self.assertIsNone(body["kpis"]["refundable_credit"])
        self.assertIsNotNone(body["kpis"]["written_off_ytd"])

    def test_a_refund_holder_sees_refunds_only(self):
        client = self.client_holding("cashier@corona.test", "finance.refund.view")
        body = self.adjustments(client).json()

        self.assertEqual({row["kind"] for row in body["data"]}, {"REFUND"})
        self.assertEqual(body["kinds"], ["REFUND"])
        self.assertIsNone(body["kpis"]["written_off_ytd"])

    def test_a_branch_write_off_holder_never_sees_another_branchs_write_offs(self):
        client = self.client_holding(
            "credit-ikeja@corona.test", "finance.writeoff.view", branch=self.ikeja,
        )
        body = self.adjustments(client).json()

        ids = {row["write_off_id"] for row in body["data"]}
        self.assertIn(self.ikeja_writeoff.id, ids)
        self.assertNotIn(self.lekki_writeoff.id, ids)
