"""One rule for which journal lines are in the ledger, and every reader keeping it.

Reversing a journal marks the original REVERSED and posts a mirror entry. Both are
in the ledger, as ``AccountBalance`` holds them, so they cancel. Greenfield Academy
runs June payroll with PAYE of 400,000 kobo, reverses it the next day and re-runs it
at 420,000. A reader that counted POSTED lines only would drop the first run, keep
its reversal, and say 20,000 is owed. Each reader here posts an entry, reverses it
in the same period and must net it to zero: the tax worksheet, the analytics slice,
the cash-flow statement, the bank and petty-cash registers, and the bank
reconciliation, where a reversal pair must not show as two unmatched items. The
spend dashboard's "paid" claims figure must also drop a reimbursement once it is
reversed.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from vs_finance.banking import (
    _unmatched_gl_lines,
    gl_account_balance,
    import_statement_lines,
    match_line,
)
from vs_finance.branch_ledger import ledger_lines
from vs_finance.constants import TaxObligationType
from vs_finance.models import Account, JournalEntry, JournalLine, PettyCashFund, TaxObligation
from vs_finance.posting import post_journal, reverse_journal
from vs_finance.reports import analytics_slice, cash_flow_statement
from vs_finance.tax_filing import _account_movement, prepare_filing

from .tests import _Phase4FixtureMixin

JUNE_START = datetime.date(2026, 6, 1)
JUNE_END = datetime.date(2026, 6, 30)


class _ReversalFixture(_Phase4FixtureMixin, TestCase):
    """Books with a year of open periods, and a helper to post a journal.

    The books are built once per class. The mixin's builders are instance
    methods, so ``setUpTestData`` calls them on a throwaway instance.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.entity, _, cls.periods = cls().build_books()

    def post(self, pairs, *, date=datetime.date(2026, 1, 15), dimensions=None, source="MANUAL"):
        """Post ``pairs`` of ``(code, debit, credit)``; ``dimensions`` tags every line.

        ``source`` names the document the journal stands in for when it touches an
        account a sub-ledger keeps (a bank ledger, a tax payable), which a ``MANUAL``
        journal may not.
        """
        period = self.periods[date.month - 1]
        entry = JournalEntry.objects.create(
            entity=self.entity, date=date, period=period, narration="test", source=source,
        )
        for i, (code, dr, cr) in enumerate(pairs, start=1):
            JournalLine.objects.create(
                entry=entry, account=Account.objects.get(entity=self.entity, code=code),
                debit=dr, credit=cr, line_no=i, dimensions=dimensions or {},
            )
        post_journal(entry)
        entry.refresh_from_db()
        return entry

    def account(self, code):
        return Account.objects.get(entity=self.entity, code=code)


class LedgerLinesRuleTests(_ReversalFixture):
    """The shared rule keeps both halves of a reversal and nothing unposted."""

    def test_a_reversed_entry_and_its_reversal_are_both_in_the_ledger(self):
        entry = self.post([("1100", 50_000, 0), ("4100", 0, 50_000)])
        reversal = reverse_journal(entry)
        self.post([("1100", 7_000, 0), ("4100", 0, 7_000)])
        draft = JournalEntry.objects.create(
            entity=self.entity, date=datetime.date(2026, 1, 20), period=self.periods[0],
        )
        JournalLine.objects.create(entry=draft, account=self.account("1100"), debit=9, credit=0)

        entries = set(ledger_lines(self.entity).values_list("entry_id", flat=True))
        self.assertIn(entry.id, entries)
        self.assertIn(reversal.id, entries)
        self.assertNotIn(draft.id, entries)


class TaxWorksheetReversalTests(_ReversalFixture):
    """A reversed PAYE run leaves the worksheet owing only the corrected run."""

    def paye_obligation(self):
        obligation, _ = TaxObligation.objects.update_or_create(
            entity=self.entity, code="PAYE",
            defaults={
                "name": "Pay As You Earn",
                "obligation_type": TaxObligationType.PAYE,
                "liability_account": self.account("2310"),
                "recoverable_account": None,
                "authority_name": "State IRS",
            },
        )
        return obligation

    def run_payroll(self, paye, *, date):
        return self.post(
            [("5300", paye * 5, 0), ("2310", 0, paye), ("1100", 0, paye * 4)], date=date,
            source="PAYROLL",
        )

    def test_a_run_and_its_reversal_net_to_zero_movement(self):
        run = self.run_payroll(400_000, date=datetime.date(2026, 6, 27))
        reverse_journal(run, date=datetime.date(2026, 6, 28))

        self.assertEqual(
            _account_movement(
                self.entity, self.account("2310"),
                period_start=JUNE_START, period_end=JUNE_END,
            ),
            (400_000, 400_000),
        )

    def test_a_reversed_and_rerun_payroll_owes_the_corrected_paye(self):
        wrong = self.run_payroll(400_000, date=datetime.date(2026, 6, 27))
        reverse_journal(wrong, date=datetime.date(2026, 6, 28))
        self.run_payroll(420_000, date=datetime.date(2026, 6, 28))

        filing = prepare_filing(
            self.paye_obligation(), period_start=JUNE_START, period_end=JUNE_END,
        )
        self.assertEqual(filing.gross_liability, 420_000)
        self.assertEqual(filing.amount_due, 420_000)


class AnalyticsSliceReversalTests(_ReversalFixture):
    """A reversed tagged spend drops out of its bucket instead of turning negative."""

    def test_a_reversed_spend_nets_to_zero_in_its_bucket(self):
        tag = {"FUND": "GRANT-A"}
        wrong = self.post([("5500", 5_000, 0), ("1100", 0, 5_000)], dimensions=tag)
        reverse_journal(wrong)
        self.post([("5500", 3_000, 0), ("1100", 0, 3_000)], dimensions=tag)

        report = analytics_slice(
            self.entity, axis="FUND", period=self.periods[0], account_type="EXPENSE",
        )
        self.assertEqual(report.total_net, 3_000)
        self.assertEqual([(r.bucket, r.code, r.net) for r in report.rows],
                         [("GRANT-A", "5500", 3_000)])


class CashFlowReversalTests(_ReversalFixture):
    """The classified legs read the same ledger as the opening and closing cash."""

    def test_a_reversed_receipt_nets_out_and_the_statement_reconciles(self):
        self.post([("1100", 80_000, 0), ("4100", 0, 80_000)], date=datetime.date(2026, 1, 5))
        wrong = self.post([("1100", 50_000, 0), ("4100", 0, 50_000)])
        reverse_journal(wrong)

        cf = cash_flow_statement(self.entity, period=self.periods[0])
        self.assertEqual(cf.closing_cash, 80_000)
        self.assertEqual(cf.net_change, 80_000)
        self.assertTrue(cf.is_reconciled)
        self.assertEqual(
            [(ln.code, ln.amount) for ln in cf.activity_lines["operating"]],
            [("4100", 80_000)],
        )


class RegisterReversalTests(_ReversalFixture):
    """The bank and petty-cash registers walk back to a zero opening balance."""

    @staticmethod
    def opening(rows, balance_key, movement):
        """The balance before the oldest row, walked back from that row."""
        oldest = rows[-1]
        return oldest[balance_key] - movement(oldest)

    def test_the_bank_register_nets_a_reversed_receipt(self):
        from vs_finance.views_ops.banking import BankAccountDetailView

        bank = self.make_bank(self.entity)
        self.post([("1100", 80_000, 0), ("4100", 0, 80_000)], date=datetime.date(2026, 1, 5),
                  source="BANK")
        reverse_journal(self.post([("1100", 50_000, 0), ("4100", 0, 50_000)], source="BANK"))

        rows = BankAccountDetailView()._transactions(
            bank, book_balance=gl_account_balance(bank.gl_account))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["running_balance"], 80_000)
        self.assertEqual(
            self.opening(rows, "running_balance", lambda r: r["debit"] - r["credit"]), 0)

    def test_the_petty_cash_register_nets_a_reversed_top_up(self):
        from vs_finance.views_ops.pettycash import PettyCashFundDetailView

        fund = PettyCashFund.objects.create(
            entity=self.entity, name="Front Desk", custodian_name="Tunde Custodian",
            gl_account=self.account("1110"), float_amount=5_000,
        )
        reverse_journal(self.post([("1110", 5_000, 0), ("1100", 0, 5_000)], source="BANK"))

        rows = PettyCashFundDetailView()._register(fund)
        self.assertEqual([(r["in"], r["out"]) for r in rows], [(0, 5_000), (5_000, 0)])
        self.assertEqual(self.opening(rows, "balance", lambda r: r["in"] - r["out"]), 0)


class ClaimsPaidReversalTests(_ReversalFixture):
    """The spend dashboard's "paid" claims figure nets a reversed reimbursement."""

    def paid(self):
        from vs_finance.dashboard_blocks import Window
        from vs_finance.dashboard_spend import expense_claims

        window = Window("month", "This month", "January 2026",
                        datetime.date(2026, 1, 1), datetime.date(2026, 1, 31))
        claims = {self.account("2400").id}
        paid = expense_claims(self.entity, window, datetime.date(2026, 1, 31), {"claims": claims})["paid"]
        return paid["count"], paid["amount"]["kobo"]

    def test_a_paid_claim_whose_payment_is_reversed_shows_as_not_paid(self):
        self.post([("5300", 9_000, 0), ("2400", 0, 9_000)], date=datetime.date(2026, 1, 10))
        payment = self.post([("2400", 9_000, 0), ("1100", 0, 9_000)], date=datetime.date(2026, 1, 25))
        self.assertEqual(self.paid(), (1, 9_000))

        reverse_journal(payment)
        self.assertEqual(self.paid(), (0, 0))

    def test_a_voided_claim_is_not_counted_as_paid(self):
        reverse_journal(
            self.post([("5300", 4_000, 0), ("2400", 0, 4_000)], date=datetime.date(2026, 1, 10)))
        self.post([("5300", 9_000, 0), ("2400", 0, 9_000)], date=datetime.date(2026, 1, 10))
        self.post([("2400", 9_000, 0), ("1100", 0, 9_000)], date=datetime.date(2026, 1, 25))

        self.assertEqual(self.paid(), (1, 9_000))


class BankReconciliationReversalTests(_ReversalFixture):
    """An entry and its own reversal need no statement line while both are unpaired."""

    def cash_line(self, entry):
        return entry.lines.get(account__code="1100")

    def test_an_unpaired_reversal_pair_is_not_unmatched(self):
        bank = self.make_bank(self.entity)
        kept = self.post([("1100", 80_000, 0), ("4100", 0, 80_000)], date=datetime.date(2026, 1, 5),
                         source="BANK")
        reverse_journal(self.post([("1100", 50_000, 0), ("4100", 0, 50_000)], source="BANK"))

        self.assertEqual(
            [ln.id for ln in _unmatched_gl_lines(bank)], [self.cash_line(kept).id])

    def test_once_the_original_is_matched_its_reversal_is_a_real_item(self):
        bank = self.make_bank(self.entity)
        receipt = self.post([("1100", 50_000, 0), ("4100", 0, 50_000)], source="BANK")
        statement_line = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 15), "amount": 50_000},
        ])[1][0]
        match_line(statement_line, self.cash_line(receipt))
        reversal = reverse_journal(receipt)

        self.assertEqual(
            [ln.id for ln in _unmatched_gl_lines(bank)], [self.cash_line(reversal).id])

    def test_once_the_reversal_is_matched_the_original_can_be_matched(self):
        bank = self.make_bank(self.entity)
        receipt = self.post([("1100", 50_000, 0), ("4100", 0, 50_000)], source="BANK")
        reversal = reverse_journal(receipt)
        imported = {line.external_id: line for line in import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 16), "amount": -50_000, "external_id": "OUT"},
            {"txn_date": datetime.date(2026, 1, 15), "amount": 50_000, "external_id": "IN"},
        ])[1]}
        refund_line, receipt_line = imported["OUT"], imported["IN"]
        match_line(refund_line, self.cash_line(reversal))

        original = self.cash_line(receipt)
        self.assertEqual([ln.id for ln in _unmatched_gl_lines(bank)], [original.id])
        match_line(receipt_line, original)
        self.assertEqual(list(_unmatched_gl_lines(bank)), [])
