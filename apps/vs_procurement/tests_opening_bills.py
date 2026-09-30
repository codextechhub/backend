"""Carrying in the supplier bills still unpaid when the books began.

Corona goes live in January 2026 owing Chuks Stationery for two bills: one raised
on 1 November 2025 and due on 30 November, and one raised on 1 December and due on
31 December. Mrs Okafor imports them as two opening bills, not one lump sum, so on
31 January the first reads 62 days overdue and the second 31, exactly as the
originals would. They credit AP and debit retained earnings, keep the AP
reconciliation passing, and are paid like any bill.
"""
from __future__ import annotations

import itertools

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus, InvoicePaymentStatus, JournalSource

from .corrections import post_vendor_credit_note
from .models import VendorInvoice
from .reports import ap_aging, spend_analysis, vendor_performance
from .tests_ap_corrections import JAN, _APCorrectionsFixture

_officers = itertools.count(1)


class OpeningBillTests(_APCorrectionsFixture):
    KEY = "procurement.vendor_invoice.import_opening"

    def officer(self):
        user = self.user_for(self.tenant, f"opening-{next(_officers)}@corona.test")
        self.grant(user, self.KEY, tenant=self.tenant,
                   role_key=f"opening-{next(_officers)}", branch=self.ikeja)
        return TenantAPIClient(user=user)

    def url(self):
        return f"/v1/procurement/vendor-invoices/opening/?entity={self.books.code}"

    def row(self, raised, due, amount, reference):
        return {
            "vendor": self.vendor.code, "invoice_date": raised.isoformat(),
            "due_date": due.isoformat(), "amount": amount, "vendor_reference": reference,
        }

    def carry_in(self):
        response = self.officer().post(self.url(), {"bills": [
            self.row(JAN(2025, 11, 1), JAN(2025, 11, 30), 420_000_00, "CS-1101"),
            self.row(JAN(2025, 12, 1), JAN(2025, 12, 31), 1_100_000_00, "CS-1201"),
        ]}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return list(VendorInvoice.objects.filter(is_opening=True).order_by("invoice_date"))

    def test_opening_bills_age_by_their_own_dates(self):
        november, december = self.carry_in()

        for bill in (november, december):
            self.assertEqual((bill.status, bill.branch_id), (DocumentStatus.POSTED, self.ikeja.pk))
            self.assertEqual(bill.journal.source, JournalSource.OPENING)
            self.assertEqual(bill.journal.date, JAN(2026, 1, 1))
        self.assertEqual(self.journal_lines(november.journal), {
            "3200": (420_000_00, 0), "2100": (0, 420_000_00),
        })
        aging = ap_aging(self.books, as_of=JAN(2026, 1, 31))
        row = aging.rows[0]
        self.assertEqual(row.buckets["61-90"], 420_000_00)
        self.assertEqual(row.buckets["31-60"], 1_100_000_00)
        self.assertAPReconciled()

    def test_an_opening_bill_is_paid_like_any_bill(self):
        november, _december = self.carry_in()

        self.pay(november, 420_000_00, day=20)

        november.refresh_from_db()
        self.assertEqual(november.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(self.balance("2100"), 1_100_000_00)
        self.assertAPReconciled()

    def test_a_bill_dated_after_the_books_went_live_is_refused(self):
        self.non_po_bill(10_000, day=10, tax=False)

        refused = self.officer().post(self.url(), {"bills": [
            self.row(JAN(2026, 1, 15), JAN(2026, 1, 31), 50_000, "CS-0115"),
        ]}, format="json")
        accepted = self.officer().post(self.url(), {"bills": [
            self.row(JAN(2025, 12, 1), JAN(2025, 12, 31), 50_000, "CS-1202"),
        ]}, format="json")

        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("went live", str(refused.data))
        self.assertEqual(accepted.status_code, 201, accepted.data)

    def test_the_import_is_all_or_nothing(self):
        response = self.officer().post(self.url(), {"bills": [
            self.row(JAN(2025, 11, 1), JAN(2025, 11, 30), 420_000_00, "CS-1101"),
            self.row(JAN(2025, 12, 1), JAN(2025, 12, 31), 0, "CS-1201"),
        ]}, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(VendorInvoice.objects.filter(is_opening=True).exists())

    def test_import_needs_its_own_key(self):
        user = self.user_for(self.tenant, "viewer@corona.test")
        self.grant(user, "procurement.vendor_invoice.create", tenant=self.tenant,
                   role_key="bill-clerk", branch=self.ikeja)

        response = TenantAPIClient(user=user).post(self.url(), {"bills": [
            self.row(JAN(2025, 11, 1), JAN(2025, 11, 30), 420_000_00, "CS-1101"),
        ]}, format="json")

        self.assertEqual(response.status_code, 403)


class SpendNetOfCreditTests(_APCorrectionsFixture):
    """Spend reads what was really bought: credit notes out, opening bills left out."""

    def test_spend_and_billing_are_net_of_credit_notes_and_leave_opening_bills_out(self):
        bill = self.non_po_bill(100_000, tax=False)
        post_vendor_credit_note(self.credit_note(bill, amount=30_000))
        VendorInvoice.objects.create(
            entity=self.books, vendor=self.vendor, branch=self.ikeja,
            invoice_date=JAN(2026, 1, 3), status=DocumentStatus.POSTED,
            is_opening=True, subtotal=999_000, total=999_000,
        )

        spend = spend_analysis(self.books)
        performance = vendor_performance(self.books)

        self.assertEqual((spend.total_gross, spend.invoice_count), (70_000, 1))
        self.assertEqual(performance.rows[0].total_billed, 70_000)
