"""Branch financial statements, built from the journals in a reader's reach.

Corona runs Ikeja, Lekki and Yaba. Ikeja invoices one parent 100,000 kobo and
banks 60,000 of it; Lekki invoices a parent three times and banks 30,000; one
invoice is school-wide. The
Ikeja bursar's statements must show Ikeja's revenue and cash plus the school-wide
invoice, never Lekki's, and each statement must still hold together: the trial
balance balances, the balance sheet satisfies its equation, the cash flow
reconciles and the equity statement agrees with the balance sheet. A reader whose
reach covers every branch must get exactly the whole-school figures.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.branch_ledger import BranchLedger, ledger_balances
from vs_finance.models import Account, InvoiceLine, Payment
from vs_finance.reports import (
    balance_sheet,
    cash_flow_statement,
    income_statement,
    statement_of_changes_in_equity,
    trial_balance,
)
from vs_rbac.scoping import BranchScope

from .tests_branch_scope import _FinanceBranchFixture

INVOICE = 100_000


class _LedgerFixture(_FinanceBranchFixture):
    def setUp(self):
        from vs_finance.receivables import post_invoice, post_payment

        super().setUp()
        e = self.books
        income = Account.objects.get(entity=e, code="4100")

        def posted_invoice(customer, branch):
            invoice = self.invoice(e, customer, branch)
            InvoiceLine.objects.filter(invoice=invoice).update(revenue_account=income)
            post_invoice(invoice)
            invoice.refresh_from_db()
            return invoice

        ikeja_parent = self.customer(e, "CIKJ", self.ikeja)
        self.ikeja_invoice = posted_invoice(ikeja_parent, self.ikeja)
        lekki_parent = self.customer(e, "CLEK", self.lekki)
        lekki_invoices = [posted_invoice(lekki_parent, self.lekki) for _ in range(3)]
        posted_invoice(self.customer(e, "CALL", None), None)

        receipt = Payment.objects.create(
            entity=e, customer=ikeja_parent, branch=self.ikeja,
            payment_date=datetime.date(2026, 1, 15), amount=60_000,
            deposit_account=Account.objects.get(entity=e, code="1100"),
        )
        post_payment(receipt, allocations=[(self.ikeja_invoice, 60_000)])
        # Lekki banks 30,000, which must never appear in Ikeja's cash.
        lekki_receipt = Payment.objects.create(
            entity=e, customer=lekki_parent, branch=self.lekki,
            payment_date=datetime.date(2026, 1, 16), amount=30_000,
            deposit_account=Account.objects.get(entity=e, code="1100"),
        )
        post_payment(lekki_receipt, allocations=[(lekki_invoices[0], 30_000)])

        self.ikeja_scope = BranchScope(frozenset({self.ikeja.id}), include_shared=True)
        self.every_branch = BranchScope(
            frozenset({self.ikeja.id, self.lekki.id, self.yaba.id}), include_shared=True,
        )


class BranchStatementTests(_LedgerFixture):
    """The statements a branch reader gets, and that they still hold together."""

    def test_income_is_the_branchs_and_the_school_wide_revenue_only(self):
        pnl = income_statement(self.books, scope=self.ikeja_scope)
        self.assertEqual(pnl.total_income, 2 * INVOICE)  # Ikeja's and the school-wide one.
        self.assertEqual(income_statement(self.books).total_income, 5 * INVOICE)

    def test_the_branch_trial_balance_balances(self):
        tb = trial_balance(self.books, scope=self.ikeja_scope)
        self.assertTrue(tb.is_balanced)
        self.assertGreater(tb.total_debit, 0)

    def test_the_branch_balance_sheet_satisfies_its_equation(self):
        bs = balance_sheet(self.books, as_of=datetime.date(2026, 1, 31), scope=self.ikeja_scope)
        self.assertTrue(bs.is_balanced, bs.difference)

    def test_the_branch_cash_flow_reconciles_to_its_own_cash(self):
        cf = cash_flow_statement(self.books, scope=self.ikeja_scope)
        self.assertTrue(cf.is_reconciled)
        self.assertEqual(cf.closing_cash, 60_000)
        self.assertEqual(cash_flow_statement(self.books).closing_cash, 90_000)

    def test_the_branch_equity_statement_agrees_with_its_balance_sheet(self):
        soce = statement_of_changes_in_equity(self.books, scope=self.ikeja_scope)
        self.assertTrue(soce.is_reconciled)

    def test_a_reach_over_every_branch_gives_the_whole_school_figures(self):
        whole = trial_balance(self.books)
        every = trial_balance(self.books, scope=self.every_branch)
        self.assertEqual(
            [(r.code, r.debit, r.credit) for r in every.rows],
            [(r.code, r.debit, r.credit) for r in whole.rows],
        )

    def test_an_unnarrowed_reader_keeps_the_stored_balances(self):
        self.assertNotIsInstance(ledger_balances(self.books), BranchLedger)

    def test_an_unknown_filter_fails_loudly_rather_than_reading_the_whole_entity(self):
        with self.assertRaises(ValueError):
            BranchLedger(self.books, self.ikeja_scope).filter(created_at__gte=datetime.date(2026, 1, 1))


class BranchReportEndpointTests(_LedgerFixture):
    """The report endpoints narrow for a branch-bound reader and say so."""

    def client_holding(self, email, *keys, branch=None):
        user = self.grant(
            self.user_for(self.tenant, email), *keys,
            tenant=self.tenant, role_key=f"role-{email}", branch=branch,
        )
        return TenantAPIClient(user=user)

    def get(self, client, path, suffix=""):
        return client.get(f"/v1/finance/reports/{path}/?entity={self.books.code}{suffix}")

    def test_a_branch_bursar_gets_their_branchs_income_statement_without_the_school_budget(self):
        client = self.client_holding("is-ikeja@corona.test", "finance.report.view", branch=self.ikeja)
        data = self.get(client, "income-statement").data["data"]

        self.assertTrue(data["narrowed"])
        self.assertFalse(data["has_budget"])

    def test_every_statement_endpoint_says_it_is_narrowed(self):
        client = self.client_holding("all-ikeja@corona.test", "finance.report.view", branch=self.ikeja)
        for path in ("trial-balance", "income-statement", "balance-sheet", "cash-flow",
                     "changes-in-equity", "ar-reconciliation"):
            with self.subTest(path=path):
                response = self.get(client, path)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertTrue(response.data["data"]["narrowed"])

    def test_a_narrowed_export_says_whose_figures_it_holds(self):
        client = self.client_holding("export-ikeja@corona.test", "finance.report.view", branch=self.ikeja)
        response = self.get(client, "trial-balance", "&export=csv")

        body = b"".join(response.streaming_content) if response.streaming else response.content
        self.assertIn(b"branches and school-wide entries only", body)

    def test_the_statutory_pack_is_the_schools_filing_and_refused_to_a_branch(self):
        branch = self.client_holding("pack-ikeja@corona.test", "finance.report.view", branch=self.ikeja)
        self.assertEqual(self.get(branch, "statutory-pack").status_code, 403)

        school = self.client_holding("pack-hq@corona.test", "finance.report.view")
        self.assertEqual(self.get(school, "statutory-pack").status_code, 200)

    def test_a_school_wide_reader_is_not_narrowed(self):
        client = self.client_holding("is-hq@corona.test", "finance.report.view")
        data = self.get(client, "trial-balance").data["data"]
        self.assertFalse(data["narrowed"])

    def test_the_branch_ar_reconciliation_compares_like_with_like(self):
        client = self.client_holding("rec-ikeja@corona.test", "finance.report.view", branch=self.ikeja)
        data = self.get(client, "ar-reconciliation").data["data"]
        self.assertTrue(data["is_reconciled"], data)
