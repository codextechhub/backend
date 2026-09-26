"""The finance overview's windows and operational cards.

Corona is a school running Ikeja, Lekki and Yaba. Its First Term runs from
5 January to 31 March 2026, and one fee structure (``T1``) bills it. In January
Ikeja's Tunde is billed First Term fees; Lekki's Aisha still owes last term's
balance on an arrears invoice. Both pay in January.

"This term" counts by the fees billed for the term: Tunde's payment is First
Term money and Aisha's is not, even though both arrived in the same month. "This
month" counts by date and includes both. The books of a company that is not a
school offer no term at all.

The operational cards follow the reader: the per-branch comparison and the bank
accounts are for readers who see the whole school, and a bursar at Ikeja never
sees Lekki's payment plans among the ones running behind.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.dashboard import DashboardReader, finance_dashboard
from vs_finance.models import Account, FiscalPeriod, InvoiceLine, Payment

from .tests_branch_scope import _FinanceBranchFixture


class _OverviewFixture(_FinanceBranchFixture):
    def setUp(self):
        from schools.core.fal.models import FeeStructureTermLink
        from schools.vs_academics.models import AcademicSession, AcademicTerm
        from vs_finance.models import FeeStructure

        super().setUp()
        e = self.books
        self.period = FiscalPeriod.objects.get(entity=e, period_no=1)
        session = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2025/2026", status="ACTIVE",
            start_date=datetime.date(2025, 9, 1), end_date=datetime.date(2026, 7, 31),
        )
        term = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=session, name="First Term", order_index=1,
            start_date=datetime.date(2026, 1, 5), end_date=datetime.date(2026, 3, 31),
        )
        structure = FeeStructure.objects.create(entity=e, code="T1", name="First Term fees")
        FeeStructureTermLink.objects.create(fee_structure=structure, session=session, term=term)

        self.tunde = self.customer(e, "CIKJ", self.ikeja)
        self.aisha = self.customer(e, "CLEK", self.lekki)
        self.term_invoice = self.posted(self.invoice(e, self.tunde, self.ikeja), reference="FEE:T1")
        self.arrears = self.posted(self.invoice(e, self.aisha, self.lekki), reference="ARREARS-2025T3")
        self.pay(self.tunde, self.ikeja, self.term_invoice, 60_000, "BANK_TRANSFER")
        self.pay(self.aisha, self.lekki, self.arrears, 40_000, "CASH")

    def posted(self, invoice, *, reference):
        from vs_finance.receivables import post_invoice

        invoice.reference = reference
        invoice.save(update_fields=["reference"])
        InvoiceLine.objects.filter(invoice=invoice).update(
            revenue_account=Account.objects.get(entity=invoice.entity, code="4100"),
        )
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def pay(self, customer, branch, invoice, amount, method):
        from vs_finance.receivables import post_payment

        payment = Payment.objects.create(
            entity=self.books, customer=customer, branch=branch, method=method,
            payment_date=datetime.date(2026, 1, 15), amount=amount,
            deposit_account=Account.objects.get(entity=self.books, code="1100"),
        )
        post_payment(payment, allocations=[(invoice, amount)])
        return payment

    def dashboard(self, window=None, reader=None):
        from vs_finance.dashboard import EVERY_BLOCK

        return finance_dashboard(self.books, period=self.period, window=window, reader=reader or EVERY_BLOCK)

    def reader(self, *keys, branch=None):
        user = self.grant(
            self.user_for(self.tenant, f"{'-'.join(keys)}-{getattr(branch, 'id', 'all')}@corona.test"),
            *keys, tenant=self.tenant, role_key=f"role-{len(keys)}-{getattr(branch, 'id', 'all')}",
            branch=branch,
        )
        return DashboardReader.for_user(user, self.tenant)


class WindowTests(_OverviewFixture):
    """Which windows the books offer, and what each one counts."""

    def test_a_schools_books_default_to_the_current_term(self):
        d = self.dashboard()
        self.assertEqual(d["books"], "school")
        self.assertEqual([w["key"] for w in d["windows"]], ["term", "month", "year"])
        self.assertEqual(d["window"]["key"], "term")
        self.assertEqual(d["window"]["name"], "First Term 2025/2026")
        self.assertEqual(d["window"]["basis"], "billed_for")

    def test_the_term_counts_the_terms_fees_not_arrears_paid_during_it(self):
        term = self.dashboard("term")["collections"]
        self.assertEqual(term["billed"]["kobo"], 100_000)
        self.assertEqual(term["collected"]["kobo"], 60_000)  # Aisha's arrears are last term's.
        self.assertEqual(term["rate_pct"], 60.0)

    def test_the_month_counts_everything_invoiced_and_received_in_it(self):
        month = self.dashboard("month")["collections"]
        self.assertEqual(month["billed"]["kobo"], 200_000)
        self.assertEqual(month["collected"]["kobo"], 100_000)
        self.assertEqual(self.dashboard("month")["window"]["basis"], "dates")

    def test_an_unknown_window_reads_the_default(self):
        self.assertEqual(self.dashboard("fortnight")["window"]["key"], "term")

    def test_books_that_are_not_a_schools_offer_calendar_windows_only(self):
        from vs_tenants.models import Tenant

        Tenant.objects.filter(pk=self.tenant.pk).update(kind=Tenant.Kind.ORGANIZATION)
        self.books.refresh_from_db()
        d = self.dashboard()
        self.assertEqual(d["books"], "general")
        self.assertEqual([w["key"] for w in d["windows"]], ["month", "quarter", "year"])

    def test_receipts_are_grouped_by_how_they_were_paid(self):
        items = {i["key"]: i["amount"]["kobo"] for i in self.dashboard("month")["channels"]["items"]}
        self.assertEqual(items, {"BANK_TRANSFER": 60_000, "CASH": 40_000})
        # The term reads only what settled the term's fees.
        term = {i["key"] for i in self.dashboard("term")["channels"]["items"]}
        self.assertEqual(term, {"BANK_TRANSFER"})


class BranchCardTests(_OverviewFixture):
    """The per-branch comparison is for a reader who sees the whole school."""

    def test_the_whole_school_sees_every_branch(self):
        rows = {r["name"]: r for r in self.dashboard("month")["branches"]}
        self.assertEqual(rows["Ikeja Branch"]["collected"]["kobo"], 60_000)
        self.assertEqual(rows["Lekki Branch"]["collected"]["kobo"], 40_000)
        self.assertIn("Yaba Branch", rows)

    def test_a_branch_reader_gets_no_comparison(self):
        reader = self.reader("finance.invoice.view", "finance.payment.view", branch=self.ikeja)
        self.assertIsNone(self.dashboard("month", reader)["branches"])

    def test_a_branch_readers_collections_cover_their_branch_only(self):
        reader = self.reader("finance.invoice.view", "finance.payment.view", branch=self.ikeja)
        month = self.dashboard("month", reader)["collections"]
        self.assertEqual(month["collected"]["kobo"], 60_000)


class OperationalCardTests(_OverviewFixture):
    """What needs doing, each item for the readers who hold its key."""

    def test_overdue_payers_are_grouped_by_payer(self):
        self.posted(self.invoice(self.books, self.aisha, self.lekki), reference="ARREARS-2025T2")
        payers = self.dashboard("month")["top_payers"]
        aisha = next(p for p in payers if p["code"] == "CLEK")
        self.assertEqual(aisha["invoices"], 2)
        self.assertEqual(aisha["amount"]["kobo"], 60_000 + 100_000)

    def test_the_receivables_summary_counts_payers_not_invoices(self):
        self.posted(self.invoice(self.books, self.aisha, self.lekki), reference="ARREARS-2025T2")
        summary = self.dashboard("month")["receivables_summary"]
        # Tunde owes 40,000 and Aisha 60,000 + 100,000, all due 25 January.
        self.assertEqual(summary["overdue_payers"], 2)
        self.assertEqual(summary["overdue_amount"]["kobo"], 200_000)
        self.assertEqual(summary["oldest_days_overdue"], 6)
        self.assertEqual(summary["owing_payers"], 2)
        ikeja = self.reader("finance.invoice.view", branch=self.ikeja)
        self.assertEqual(self.dashboard("month", ikeja)["receivables_summary"]["overdue_payers"], 1)

    def test_payment_plans_behind_follow_the_readers_branches(self):
        from vs_finance.installments import activate_payment_plan, build_installments
        from vs_finance.models import PaymentPlan

        lekki_invoice = self.posted(self.invoice(self.books, self.aisha, self.lekki), reference="FEE:T1")
        plan = PaymentPlan.objects.create(
            entity=self.books, customer=self.aisha, invoice=lekki_invoice, branch=self.lekki,
            start_date=datetime.date(2026, 1, 12), frequency="MONTHLY", installment_count=2,
            total_amount=100_000,
        )
        build_installments(plan)
        activate_payment_plan(plan)

        school = self.reader("finance.paymentplan.view")
        keys = [i["key"] for i in self.dashboard(reader=school)["attention"]]
        self.assertIn("plans_behind", keys)
        ikeja = self.reader("finance.paymentplan.view", branch=self.ikeja)
        keys = [i["key"] for i in self.dashboard(reader=ikeja)["attention"]]
        self.assertNotIn("plans_behind", keys)

    def test_bank_accounts_need_the_key_and_the_whole_school(self):
        from vs_finance.models import BankAccount

        BankAccount.objects.create(
            entity=self.books, name="Operations", gl_account=Account.objects.get(entity=self.books, code="1100"),
        )
        self.assertIsNone(self.dashboard(reader=self.reader("finance.report.view"))["bank_accounts"])
        school = self.dashboard(reader=self.reader("finance.bankaccount.view"))["bank_accounts"]
        self.assertEqual(school[0]["balance"]["kobo"], 100_000)
        self.assertEqual(school[0]["spark"][-1], 100_000)
        self.assertNotIn("gl_account_id", school[0])
        ikeja = self.dashboard(reader=self.reader("finance.bankaccount.view", branch=self.ikeja))
        self.assertIsNone(ikeja["bank_accounts"])

    def test_a_reader_with_no_operational_keys_gets_no_items(self):
        d = self.dashboard(reader=self.reader("finance.invoice.view"))
        self.assertEqual(d["attention"], [])
        self.assertEqual(d["upcoming"], [])


class OverviewEndpointTests(_OverviewFixture):
    def test_the_endpoint_reads_the_window_asked_for(self):
        user = self.grant(
            self.user_for(self.tenant, "window@corona.test"), "finance.invoice.view",
            tenant=self.tenant, role_key="role-window",
        )
        response = TenantAPIClient(user=user).get(
            f"/v1/finance/reports/dashboard/?entity={self.books.code}&window=month&period=1",
        )
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(response.json()["data"]["window"]["key"], "month")
