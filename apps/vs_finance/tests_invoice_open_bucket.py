"""The open-invoice list a payment picker offers: everything still owed, in reach.

Tola keeps Lekki's books. The Okafors are filed under Ikeja and owe Lekki for
this term, so their Lekki invoice is one she can collect. Asked for the open
invoices, she gets every posted Lekki or school-wide invoice with money still
owed, overdue or not, and nothing paid, drafted or Ikeja's.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient

from .constants import DocumentStatus, InvoicePaymentStatus
from .models import Invoice
from .tests_branch_scope import _FinanceBranchFixture


class OpenInvoiceBucketTests(_FinanceBranchFixture):
    def test_open_is_every_invoice_still_owed_in_reach(self):
        okafor = self.customer(self.books, "COKAF", self.ikeja)

        def invoice(branch, status, paid, due=datetime.date(2026, 1, 25)):
            row = self.invoice(self.books, okafor, branch)
            Invoice.objects.filter(pk=row.pk).update(
                status=status, payment_status=paid, due_date=due)
            return row.pk

        expected = {
            invoice(self.lekki, DocumentStatus.POSTED, InvoicePaymentStatus.UNPAID),
            invoice(self.lekki, DocumentStatus.POSTED, InvoicePaymentStatus.PARTIAL),
            invoice(None, DocumentStatus.POSTED, InvoicePaymentStatus.UNPAID,
                    due=datetime.date(2099, 1, 1)),
        }
        invoice(self.lekki, DocumentStatus.POSTED, InvoicePaymentStatus.PAID)
        invoice(self.lekki, DocumentStatus.DRAFT, InvoicePaymentStatus.UNPAID)
        invoice(self.ikeja, DocumentStatus.POSTED, InvoicePaymentStatus.UNPAID)

        tola = TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, "tola-open@corona.test"), "finance.invoice.view",
            tenant=self.tenant, role_key="tola-open", branch=self.lekki))
        response = tola.get(f"/v1/finance/invoices/?entity={self.books.code}&bucket=open")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual({row["id"] for row in response.data["data"]}, expected)
