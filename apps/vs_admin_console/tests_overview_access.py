"""The landing screen's finance signals count the reader's branches and honour finance switches.

Corona runs Ikeja and Lekki; Single Site runs one branch; Rival Group is a third
school whose documents are never counted for anybody else. A signal restates a
figure of the dashboard (``finance.dashboard``), so a role that may not read
that card is served no signal for it, and a branch-bound reader is counted
their own branches' documents only.
"""
import datetime

from vs_finance.constants import DocumentStatus, InvoicePaymentStatus
from vs_finance.models import Customer, Invoice, LedgerEntity, Payment
from vs_procurement.models import Vendor, VendorInvoice
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    set_field_access,
)

from .tests_overview import OverviewTestBase

DASHBOARD = "finance.dashboard"
KEYS = ("finance.invoice.view", "finance.payment.view", "finance.report.view",
        "procurement.vendor_invoice.view")
_count = iter(range(1, 1000))


def _k(*names):
    return [f"{DASHBOARD}.{name}" for name in names]


class OverviewFinanceSignalAccessTests(OverviewTestBase):

    def setUp(self):
        super().setUp()
        install_declared_fields(DASHBOARD)
        self.ikeja = self.branch
        self.lekki = make_branch(self.school, name="Lekki Branch", is_main=False)
        self.solo_school = make_school(slug="ov-solo", name="Single Site")
        self.solo_branch = make_branch(self.solo_school, name="Main Branch")
        self.rival_school = make_school(slug="ov-rival", name="Rival Group")
        self.rival_branch = make_branch(self.rival_school, name="Rival Branch")
        self.books = self.entity_for(self.school, "OVCORONA")
        self.solo_books = self.entity_for(self.solo_school, "OVSOLO")
        self.rival_books = self.entity_for(self.rival_school, "OVRIVAL")
        # Ikeja: two overdue bills, one receipt with credit; Lekki: one bill, two receipts.
        self.bills(self.books, self.ikeja, 2)
        self.bills(self.books, self.lekki, 1)
        self.credits(self.books, self.ikeja, 1)
        self.credits(self.books, self.lekki, 2)
        self.vendor_bills(self.books, self.ikeja, 1)
        self.vendor_bills(self.books, self.lekki, 2)
        self.bills(self.solo_books, self.solo_branch, 1)
        self.credits(self.solo_books, self.solo_branch, 1)
        self.bills(self.rival_books, self.rival_branch, 5)
        self.credits(self.rival_books, self.rival_branch, 5)

    @staticmethod
    def entity_for(school, code):
        return LedgerEntity.objects.create(name=f"{code} Books", code=code, tenant=school.tenant)

    def customer(self, books):
        customer, _ = Customer.objects.get_or_create(entity=books, code="C1", defaults={"name": "Acme"})
        return customer

    def bills(self, books, branch, count):
        yesterday = self.today() - datetime.timedelta(days=1)
        for _ in range(count):
            Invoice.objects.create(
                entity=books, customer=self.customer(books), branch=branch,
                invoice_date=yesterday, due_date=yesterday, status=DocumentStatus.POSTED,
                payment_status=InvoicePaymentStatus.UNPAID,
            )

    def credits(self, books, branch, count):
        for _ in range(count):
            Payment.objects.create(
                entity=books, customer=self.customer(books), branch=branch,
                payment_date=datetime.date(2026, 8, 1), status=DocumentStatus.POSTED,
                amount=10_000, allocated_amount=4_000,
            )

    def vendor_bills(self, books, branch, count):
        vendor, _ = Vendor.objects.get_or_create(entity=books, code="V1", defaults={"name": "Supplies"})
        for _ in range(count):
            VendorInvoice.objects.create(
                entity=books, vendor=vendor, branch=branch,
                invoice_date=datetime.date(2026, 8, 1), status=DocumentStatus.POSTED,
                payment_status=InvoicePaymentStatus.UNPAID,
            )

    def staff(self, school, *, hide=(), branch=None):
        number = next(_count)
        user = make_school_admin(None, email=f"ovfa-{number}@school.test", tenant=school.tenant)
        role = make_role(school, name=f"Overview reader {number}")
        for key in KEYS:
            make_role_permission(role, make_permission(key))
        make_assignment(school, user, role, branch=branch)
        if hide:
            set_field_access(role, *hide, read=False, write=False)
        return user

    def signals(self, user, school):
        return self.fetch(user, tenant=school.tenant.slug).get("signals", {})

    def counts(self, user, school):
        signals = self.signals(user, school)
        return {
            name: signals[name]["count"] if name in signals else None
            for name in ("overdue_invoices", "unallocated_credit", "vendor_invoices_unpaid")
        }

    def test_a_whole_school_reader_is_counted_every_branchs_documents_and_no_other_schools(self):
        counts = self.counts(self.staff(self.school), self.school)

        self.assertEqual(counts, {
            "overdue_invoices": 3, "unallocated_credit": 3, "vendor_invoices_unpaid": 3})

    def test_a_branch_bound_reader_is_counted_their_own_branchs_documents_only(self):
        ikeja = self.counts(self.staff(self.school, branch=self.ikeja), self.school)
        lekki = self.counts(self.staff(self.school, branch=self.lekki), self.school)

        self.assertEqual(ikeja, {
            "overdue_invoices": 2, "unallocated_credit": 1, "vendor_invoices_unpaid": 1})
        self.assertEqual(lekki, {
            "overdue_invoices": 1, "unallocated_credit": 2, "vendor_invoices_unpaid": 2})

    def test_a_single_branch_school_loses_nothing_whether_or_not_the_reader_is_pinned(self):
        whole = self.counts(self.staff(self.solo_school), self.solo_school)
        pinned = self.counts(
            self.staff(self.solo_school, branch=self.solo_branch), self.solo_school)

        for counts in (whole, pinned):
            self.assertEqual(counts["overdue_invoices"], 1)
            self.assertEqual(counts["unallocated_credit"], 1)

    def test_a_hidden_who_owes_card_removes_the_overdue_signal_only(self):
        counts = self.counts(
            self.staff(self.school, hide=_k("receivables_summary")), self.school)

        self.assertIsNone(counts["overdue_invoices"])
        self.assertEqual(counts["unallocated_credit"], 3)

    def test_a_hidden_credit_card_removes_the_unallocated_credit_signal_only(self):
        counts = self.counts(self.staff(self.school, hide=_k("credit_held")), self.school)

        self.assertIsNone(counts["unallocated_credit"])
        self.assertEqual(counts["overdue_invoices"], 3)

    def test_a_hidden_runway_card_removes_the_fiscal_runway_signal(self):
        shown = self.signals(self.staff(self.school), self.school)
        hidden = self.signals(self.staff(self.school, hide=_k("fiscal_runway")), self.school)

        self.assertIn("fiscal_runway", shown)
        self.assertNotIn("fiscal_runway", hidden)
        self.assertIn("overdue_invoices", hidden)

    def test_another_schools_switch_does_not_reach_this_school(self):
        self.staff(self.solo_school, hide=_k("receivables_summary", "credit_held", "fiscal_runway"))

        counts = self.counts(self.staff(self.school), self.school)

        self.assertEqual(counts["overdue_invoices"], 3)
        self.assertEqual(counts["unallocated_credit"], 3)
