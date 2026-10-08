"""A deferred income release offers its undo only when the undo would go through.

Corona runs Ikeja and Lekki. The Eze family is billed at Ikeja and the Bellos at
Lekki for the second term, so January 2027 releases a share at each branch. Ikeja
then closes January on its own while Lekki is still finishing. The undo reverses
every branch's release for the month at once and a closed branch's release is
sealed with its month, so the undo is refused: neither row may offer it, and
each row says where its branch's month stands.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient

from .tests_accruals import _AccrualFixture
from .tests_branch_scope import _FinanceBranchFixture

D = datetime.date
KEYS = ("finance.deferredincome.view", "finance.deferredincome.reverse")


class ReleaseRowsFollowBranchCloseTests(_AccrualFixture):

    @classmethod
    def setUpTestData(cls):
        from vs_finance.branch_close import close_branch_period
        from vs_finance.deferred_income import release_deferred_income

        super().setUpTestData()
        cls.okafor = _FinanceBranchFixture.grant(
            _FinanceBranchFixture.user_for(cls.tenant, "okafor@release-close.test"), *KEYS,
            tenant=cls.tenant, role_key="release-close-hq")
        cls.lekki_bursar = _FinanceBranchFixture.grant(
            _FinanceBranchFixture.user_for(cls.tenant, "lekki@release-close.test"), *KEYS,
            tenant=cls.tenant, role_key="release-close-lekki", branch=cls.lekki)
        cls.harbour_bursar = _FinanceBranchFixture.grant(
            _FinanceBranchFixture.user_for(cls.solo_tenant, "bursar@release-close.test"), *KEYS,
            tenant=cls.solo_tenant, role_key="release-close-harbour")

        cls.bill_for(cls.eze)
        cls.bill_for(cls.bello)
        cls.bill_for(cls.ada)
        release_deferred_income(cls.books, up_to=D(2027, 1, 31))
        release_deferred_income(cls.solo_books, up_to=D(2027, 1, 31))
        cls.january = cls.period_of(cls.books, 2027, 1)
        close_branch_period(cls.books, cls.january, cls.ikeja, force=True,
                            reason="Ikeja finished its January books")

    @classmethod
    def bill_for(cls, customer):
        from vs_finance.models import Account, Invoice, InvoiceLine
        from vs_finance.receivables import post_invoice

        books = customer.entity
        invoice = Invoice.objects.create(
            entity=books, customer=customer, branch=customer.branch,
            invoice_date=D(2026, 12, 10), due_date=D(2026, 12, 25),
        )
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=150_000,
            revenue_account=Account.objects.get(entity=books, code="4100"),
            service_start=D(2027, 1, 6), service_end=D(2027, 4, 4),
        )
        post_invoice(invoice)

    @classmethod
    def period_of(cls, books, year, month):
        from vs_finance.models import FiscalPeriod

        return FiscalPeriod.objects.get(
            entity=books, start_date=D(year, month, 1), is_closing=False)

    def rows(self, user, books=None):
        books = books or self.books
        response = TenantAPIClient(user=user).get(
            f"/v1/finance/deferred-income/releases/?month=2027-01&entity={books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["branch_name"]: row for row in response.data["data"]}

    def test_the_closed_branchs_release_cannot_be_undone_and_says_why(self):
        ikeja = self.rows(self.okafor)["Ikeja Branch"]

        self.assertEqual(ikeja["period_status"], "OPEN")
        self.assertEqual(ikeja["branch_period_status"], "CLOSED")
        self.assertFalse(ikeja["can_reverse"])
        self.assertEqual(
            ikeja["reverse_blocked_reason"],
            "Ikeja Branch has closed January 2027; its release cannot be undone while that "
            "month is closed.",
        )

    def test_the_open_branchs_release_cannot_be_undone_while_the_month_undo_is_sealed(self):
        lekki = self.rows(self.okafor)["Lekki Branch"]

        self.assertEqual(lekki["branch_period_status"], "OPEN")
        self.assertFalse(lekki["can_reverse"])
        self.assertIn("Ikeja Branch", lekki["reverse_blocked_reason"])
        self.assertIn("closed January 2027,", lekki["reverse_blocked_reason"])
        self.assertNotIn("2027-01", lekki["reverse_blocked_reason"])

    def test_a_lekki_reader_sees_only_lekkis_row_with_the_same_answer(self):
        rows = self.rows(self.lekki_bursar)

        self.assertEqual(list(rows), ["Lekki Branch"])
        self.assertFalse(rows["Lekki Branch"]["can_reverse"])

    def test_the_undo_is_still_refused_and_reverses_nothing(self):
        from vs_finance.models import DeferredIncomeRelease

        response = TenantAPIClient(user=self.okafor).post(
            f"/v1/finance/deferred-income/reverse/?entity={self.books.code}",
            {"period": self.january.pk}, format="json")

        self.assertGreaterEqual(response.status_code, 400, response.data)
        self.assertFalse(DeferredIncomeRelease.objects.filter(
            entity=self.books, reversed_at__isnull=False).exists())

    def test_once_ikeja_reopens_both_rows_offer_the_undo(self):
        from vs_finance.branch_close import reopen_branch_period

        reopen_branch_period(self.books, self.january, self.ikeja, reason="Late receipt")
        rows = self.rows(self.okafor)

        self.assertEqual({name: row["can_reverse"] for name, row in rows.items()},
                         {"Ikeja Branch": True, "Lekki Branch": True})
        self.assertEqual(rows["Ikeja Branch"]["branch_period_status"], "OPEN")
        self.assertIsNone(rows["Lekki Branch"]["reverse_blocked_reason"])

    def test_a_one_branch_school_offers_the_undo_while_its_month_is_open(self):
        rows = self.rows(self.harbour_bursar, self.solo_books)

        self.assertEqual(list(rows), ["Main Branch"])
        self.assertTrue(rows["Main Branch"]["can_reverse"])
        self.assertEqual(rows["Main Branch"]["branch_period_status"], "OPEN")

    def test_another_schools_bursar_reads_none_of_corona_releases(self):
        response = TenantAPIClient(user=self.harbour_bursar).get(
            f"/v1/finance/deferred-income/releases/?entity={self.books.code}")

        self.assertIn(response.status_code, (403, 404))
