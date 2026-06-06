"""Phase-0 foundation + Phase-1 GL tests: money, guards, entities, numbering, and
the double-entry ledger (chart of accounts, posting, reversal, trial balance)."""
from __future__ import annotations

import datetime
from decimal import Decimal

from django.test import TestCase

from vs_finance.constants import (
    AccountType,
    DocType,
    DocumentStatus,
    NormalBalance,
    PeriodStatus,
    PLATFORM_ENTITY_CODE,
)
from vs_finance.exceptions import (
    InactiveAccountError,
    PeriodClosedError,
    PostingError,
    UnbalancedJournalError,
)
from vs_finance.constants import (
    FinanceAuditAction,
    FinanceAuditStatus,
    InvoicePaymentStatus,
)
from vs_finance.constants import (
    AssetStatus,
    BankLineStatus,
    BudgetStatus,
    PayrollRunStatus,
)
from vs_finance.exceptions import (
    BankReconciliationError,
    BudgetError,
    DepreciationError,
    ExpenseClaimError,
    PayrollError,
    PeriodCloseError,
)
from vs_finance.models import (
    Account,
    AccountBalance,
    BankAccount,
    BankStatementLine,
    Budget,
    Customer,
    DepreciationSchedule,
    ExpenseClaim,
    ExpenseClaimLine,
    FinanceAuditLog,
    FiscalPeriod,
    FiscalYear,
    FixedAsset,
    Invoice,
    InvoiceLine,
    JournalEntry,
    JournalLine,
    LedgerEntity,
    Payment,
    PayrollLine,
    PayrollRun,
    TaxCode,
)
from vs_finance.money import format_naira, to_kobo, to_naira
from vs_finance.numbering import next_document_number
from vs_finance.posting import ensure_balanced, ensure_period_open, post_journal, reverse_journal
from vs_finance.receivables import post_invoice, post_payment
from vs_finance.reports import ar_aging, budget_vs_actual, reconcile_ar, trial_balance
from vs_finance.banking import (
    auto_reconcile,
    import_statement_lines,
    match_line,
    post_bank_adjustment,
)
from vs_finance.expenses import post_expense_claim, settle_expense_claim
from vs_finance.payroll import pay_payroll, post_payroll
from vs_finance.budgets import add_budget_line, approve_budget
from vs_finance.assets import acquire_asset, build_depreciation_schedule, post_depreciation
from vs_finance.close import (
    close_checklist,
    close_period,
    lock_period,
    reopen_period,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_schools.models import Branch, School


class MoneyTests(TestCase):
    def test_to_kobo_from_string_is_exact(self):
        self.assertEqual(to_kobo("1250.50"), 125050)

    def test_to_kobo_handles_float_boundary_without_drift(self):
        # The classic 0.1 + 0.2 trap: must land on 30 kobo, not 29 or 30.0000001.
        self.assertEqual(to_kobo(Decimal("0.1") + Decimal("0.2")), 30)
        self.assertEqual(to_kobo(0.1 + 0.2), 30)

    def test_round_trip(self):
        self.assertEqual(to_naira(125050), Decimal("1250.50"))
        self.assertEqual(to_kobo(to_naira(99)), 99)

    def test_half_up_rounding(self):
        self.assertEqual(to_kobo("0.005"), 1)  # rounds up at the half

    def test_format(self):
        self.assertEqual(format_naira(125050), "₦1,250.50")

    def test_to_naira_rejects_non_int(self):
        with self.assertRaises(TypeError):
            to_naira(12.5)  # type: ignore[arg-type]


class PostingGuardTests(TestCase):
    class _Period:
        def __init__(self, status):
            self.status = status

        def __str__(self):
            return f"2026-01 [{self.status}]"

    def test_open_period_allows_posting(self):
        ensure_period_open(self._Period(PeriodStatus.OPEN))  # no raise

    def test_closed_and_locked_block_posting(self):
        for status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
            with self.assertRaises(PeriodClosedError):
                ensure_period_open(self._Period(status))

    def test_soft_closed_blocked_by_default_allowed_when_privileged(self):
        with self.assertRaises(PeriodClosedError):
            ensure_period_open(self._Period(PeriodStatus.SOFT_CLOSED))
        ensure_period_open(self._Period(PeriodStatus.SOFT_CLOSED), allow_restricted=True)

    def test_missing_period_fails_closed(self):
        with self.assertRaises(PeriodClosedError):
            ensure_period_open(None)

    def test_balanced_check(self):
        ensure_balanced(125050, 125050)  # no raise
        with self.assertRaises(UnbalancedJournalError):
            ensure_balanced(125050, 125000)


class LedgerEntityTests(TestCase):
    def test_platform_entity_seeded_with_no_school(self):
        # The 0002 data migration seeds Codex's platform books; assert its shape.
        codex = LedgerEntity.objects.platform()
        self.assertIsNotNone(codex)
        self.assertEqual(codex.code, PLATFORM_ENTITY_CODE)
        self.assertTrue(codex.is_platform)
        self.assertIsNone(codex.source_school_id)
        # base_currency is now a real Currency FK (still stored as the "NGN" code).
        self.assertEqual(codex.base_currency_id, "NGN")
        self.assertEqual(codex.base_currency.symbol, "₦")

    def test_one_school_can_own_multiple_entities(self):
        school = School.objects.create(name="Greenfield", slug="greenfield")
        a = LedgerEntity.objects.create(
            name="Greenfield (Platform-managed)", code="GREEN1",
            kind=LedgerEntity.Kind.TENANT, source_school=school,
        )
        b = LedgerEntity.objects.create(
            name="Greenfield (Own books)", code="GREEN2",
            kind=LedgerEntity.Kind.TENANT, source_school=school,
        )
        self.assertEqual(
            set(LedgerEntity.objects.for_school(school).values_list("code", flat=True)),
            {"GREEN1", "GREEN2"},
        )
        self.assertNotEqual(a.code, b.code)


class NumberingTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Test Org", slug="test-org")
        self.branch = Branch.objects.create(school=self.school, name="HQ", _type="Main")
        self.entity = LedgerEntity.objects.create(
            name="Test Org Books", code="LEKKI",
            kind=LedgerEntity.Kind.TENANT, source_school=self.school,
        )
        # Use the platform entity seeded by migration 0002 (code CODEX).
        self.platform = LedgerEntity.objects.platform()

    def test_format_and_increment_with_branch(self):
        n1 = next_document_number(
            entity=self.entity, branch=self.branch, doc_type=DocType.INVOICE, fiscal_year=2026,
        )
        n2 = next_document_number(
            entity=self.entity, branch=self.branch, doc_type=DocType.INVOICE, fiscal_year=2026,
        )
        self.assertEqual(n1, f"CFX-LEKKI-B{self.branch.code:02d}-INV-2026-00001")
        self.assertEqual(n2, f"CFX-LEKKI-B{self.branch.code:02d}-INV-2026-00002")

    def test_entity_level_doc_omits_branch_segment(self):
        n = next_document_number(
            entity=self.platform, branch=None, doc_type=DocType.PAYMENT, fiscal_year=2026,
        )
        self.assertEqual(n, "CFX-CODEX-PAY-2026-00001")

    def test_scopes_are_independent(self):
        inv = next_document_number(
            entity=self.entity, branch=self.branch, doc_type=DocType.INVOICE, fiscal_year=2026,
        )
        po = next_document_number(
            entity=self.entity, branch=self.branch, doc_type=DocType.PURCHASE_ORDER, fiscal_year=2026,
        )
        self.assertTrue(inv.endswith("INV-2026-00001"))
        self.assertTrue(po.endswith("PO-2026-00001"))

    def test_two_entities_keep_independent_series(self):
        a = next_document_number(
            entity=self.entity, branch=None, doc_type=DocType.JOURNAL, fiscal_year=2026,
        )
        b = next_document_number(
            entity=self.platform, branch=None, doc_type=DocType.JOURNAL, fiscal_year=2026,
        )
        self.assertEqual(a, "CFX-LEKKI-JNL-2026-00001")
        self.assertEqual(b, "CFX-CODEX-JNL-2026-00001")


class _GLFixtureMixin:
    """Builds an entity with a seeded chart, a fiscal year and one open period."""

    def build_ledger(self, *, period_status=PeriodStatus.OPEN):
        seed_currencies()
        entity = LedgerEntity.objects.create(
            name="Test Books", code="TBOOK", kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(entity)
        year = FiscalYear.objects.create(
            entity=entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        period = FiscalPeriod.objects.create(
            entity=entity, fiscal_year=year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=period_status,
        )
        return entity, period

    def make_entry(self, entity, period, pairs, *, date=datetime.date(2026, 1, 15)):
        """pairs: list of (account_code, debit_kobo, credit_kobo)."""
        entry = JournalEntry.objects.create(
            entity=entity, date=date, period=period, narration="test",
        )
        for i, (code, dr, cr) in enumerate(pairs, start=1):
            acc = Account.objects.get(entity=entity, code=code)
            JournalLine.objects.create(
                entry=entry, account=acc, debit=dr, credit=cr, line_no=i,
            )
        return entry


class ChartOfAccountsTests(_GLFixtureMixin, TestCase):
    def test_seed_creates_five_roots_and_links_parents(self):
        entity, _ = self.build_ledger()
        roots = Account.objects.filter(entity=entity, parent__isnull=True)
        self.assertEqual(
            set(roots.values_list("account_type", flat=True)),
            {AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY,
             AccountType.INCOME, AccountType.EXPENSE},
        )
        cash = Account.objects.get(entity=entity, code="1100")
        self.assertEqual(cash.parent.code, "1000")

    def test_normal_balance_derived_and_contra_flips(self):
        entity, _ = self.build_ledger()
        cash = Account.objects.get(entity=entity, code="1100")        # asset
        self.assertEqual(cash.normal_balance, NormalBalance.DEBIT)
        accum_dep = Account.objects.get(entity=entity, code="1900")   # contra asset
        self.assertEqual(accum_dep.normal_balance, NormalBalance.CREDIT)
        revenue = Account.objects.get(entity=entity, code="4100")     # income
        self.assertEqual(revenue.normal_balance, NormalBalance.CREDIT)

    def test_seed_is_idempotent(self):
        entity, _ = self.build_ledger()
        before = Account.objects.filter(entity=entity).count()
        seed_chart_of_accounts(entity)
        self.assertEqual(Account.objects.filter(entity=entity).count(), before)


class PostingTests(_GLFixtureMixin, TestCase):
    def test_balanced_post_updates_balances_and_stamps_posted(self):
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 50000, 0), ("4100", 0, 50000)])
        post_journal(entry)

        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.POSTED)
        self.assertIsNotNone(entry.posted_at)
        self.assertTrue(entry.document_number.startswith("CFX-TBOOK-JNL-"))

        cash_bal = AccountBalance.objects.get(
            account__code="1100", period=period,
        )
        self.assertEqual(cash_bal.debit_total, 50000)
        rev_bal = AccountBalance.objects.get(account__code="4100", period=period)
        self.assertEqual(rev_bal.credit_total, 50000)

    def test_unbalanced_entry_is_rejected(self):
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 50000, 0), ("4100", 0, 40000)])
        with self.assertRaises(UnbalancedJournalError):
            post_journal(entry)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.DRAFT)
        self.assertFalse(AccountBalance.objects.filter(period=period).exists())

    def test_closed_period_blocks_posting(self):
        entity, period = self.build_ledger(period_status=PeriodStatus.CLOSED)
        entry = self.make_entry(entity, period, [("1100", 10000, 0), ("4100", 0, 10000)])
        with self.assertRaises(PeriodClosedError):
            post_journal(entry)

    def test_inactive_account_blocks_posting(self):
        entity, period = self.build_ledger()
        Account.objects.filter(entity=entity, code="4100").update(is_active=False)
        entry = self.make_entry(entity, period, [("1100", 10000, 0), ("4100", 0, 10000)])
        with self.assertRaises(InactiveAccountError):
            post_journal(entry)

    def test_cannot_double_post(self):
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 10000, 0), ("4100", 0, 10000)])
        post_journal(entry)
        with self.assertRaises(PostingError):
            post_journal(entry)

    def test_reversal_nets_balances_to_zero(self):
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 30000, 0), ("4100", 0, 30000)])
        post_journal(entry)

        reversal = reverse_journal(entry)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.REVERSED)
        self.assertEqual(reversal.reverses_id, entry.id)
        self.assertEqual(reversal.status, DocumentStatus.POSTED)

        cash_bal = AccountBalance.objects.get(account__code="1100", period=period)
        self.assertEqual(cash_bal.debit_total, 30000)
        self.assertEqual(cash_bal.credit_total, 30000)  # reversal credited it back
        self.assertEqual(cash_bal.net_kobo, 0)

    def test_cannot_reverse_twice(self):
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 30000, 0), ("4100", 0, 30000)])
        post_journal(entry)
        reverse_journal(entry)
        with self.assertRaises(PostingError):
            reverse_journal(entry)


class TrialBalanceTests(_GLFixtureMixin, TestCase):
    def test_trial_balance_balances(self):
        entity, period = self.build_ledger()
        # Two transactions: cash sale, and a salary payment.
        post_journal(self.make_entry(entity, period, [("1100", 100000, 0), ("4100", 0, 100000)]))
        post_journal(self.make_entry(entity, period, [("5200", 25000, 0), ("1100", 0, 25000)]))

        tb = trial_balance(entity)
        self.assertTrue(tb.is_balanced)
        self.assertEqual(tb.difference, 0)
        self.assertEqual(tb.total_debit, tb.total_credit)
        # Cash net debit 75,000; revenue credit 100,000; salary debit 25,000.
        cash_row = next(r for r in tb.rows if r.code == "1100")
        self.assertEqual(cash_row.debit, 75000)
        self.assertEqual(cash_row.credit, 0)

    def test_empty_ledger_trivially_balances(self):
        entity, _ = self.build_ledger()
        tb = trial_balance(entity)
        self.assertTrue(tb.is_balanced)
        self.assertEqual(tb.rows, [])


class FinanceAuditTests(_GLFixtureMixin, TestCase):
    def test_post_writes_authoritative_audit_row(self):
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 40000, 0), ("4100", 0, 40000)])
        post_journal(entry)

        log = FinanceAuditLog.objects.get(
            action=FinanceAuditAction.JOURNAL_POSTED, target_id=str(entry.pk),
        )
        self.assertEqual(log.status, FinanceAuditStatus.SUCCESS)
        self.assertEqual(log.entity_id, entity.id)
        self.assertEqual(log.document_number, entry.document_number)
        self.assertEqual(log.metadata.get("debit"), 40000)

    def test_reversal_writes_reversed_audit_row(self):
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 40000, 0), ("4100", 0, 40000)])
        post_journal(entry)
        reversal = reverse_journal(entry)

        self.assertTrue(
            FinanceAuditLog.objects.filter(
                action=FinanceAuditAction.JOURNAL_REVERSED, target_id=str(entry.pk),
            ).exists()
        )
        # The reversing entry's own post is audited too.
        self.assertTrue(
            FinanceAuditLog.objects.filter(
                action=FinanceAuditAction.JOURNAL_POSTED, target_id=str(reversal.pk),
            ).exists()
        )

    def test_rejected_post_records_failure_durably(self):
        # An unbalanced post rolls back, but the rejection audit must survive.
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 40000, 0), ("4100", 0, 30000)])
        with self.assertRaises(UnbalancedJournalError):
            post_journal(entry)

        log = FinanceAuditLog.objects.get(
            action=FinanceAuditAction.JOURNAL_POST_REJECTED, target_id=str(entry.pk),
        )
        self.assertEqual(log.status, FinanceAuditStatus.FAILED)
        self.assertEqual(log.metadata.get("error_code"), "JOURNAL_UNBALANCED")
        # And nothing posted: no balances, entry still draft.
        self.assertFalse(AccountBalance.objects.filter(period=period).exists())

    def test_audit_log_is_append_only(self):
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 1000, 0), ("4100", 0, 1000)])
        post_journal(entry)
        log = FinanceAuditLog.objects.filter(target_id=str(entry.pk)).first()
        log.message = "tampered"
        with self.assertRaises(ValueError):
            log.save()
        with self.assertRaises(ValueError):
            log.delete()


class _ARFixtureMixin(_GLFixtureMixin):
    """A ledger plus a customer wired to the AR control account and a VAT tax code."""

    def build_ar(self, *, period_status=PeriodStatus.OPEN):
        entity, period = self.build_ledger(period_status=period_status)
        ar_control = Account.objects.get(entity=entity, code="1200")   # Accounts Receivable
        vat_output = Account.objects.get(entity=entity, code="2200")   # Output VAT
        customer = Customer.objects.create(
            entity=entity, code="CUST1", name="Acme Ltd",
            receivable_account=ar_control,
        )
        vat = TaxCode.objects.create(
            entity=entity, code="VAT", name="VAT 7.5%", rate_bps=750,
            collected_account=vat_output,
        )
        return entity, period, customer, vat

    def make_invoice(self, entity, customer, *, lines, date=datetime.date(2026, 1, 10),
                     due=datetime.date(2026, 1, 25)):
        """lines: list of (revenue_code, quantity, unit_price_kobo, tax_code_or_None)."""
        inv = Invoice.objects.create(
            entity=entity, customer=customer, invoice_date=date, due_date=due,
        )
        for i, (code, qty, price, tax) in enumerate(lines, start=1):
            InvoiceLine.objects.create(
                invoice=inv, revenue_account=Account.objects.get(entity=entity, code=code),
                quantity=qty, unit_price=price, tax_code=tax, line_no=i,
            )
        return inv


class InvoicePostingTests(_ARFixtureMixin, TestCase):
    def test_invoice_posts_balanced_ar_journal_with_tax(self):
        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(
            entity, customer, lines=[("4100", 1, 100000, vat)],  # ₦1,000 + 7.5% VAT
        )
        post_invoice(inv)
        inv.refresh_from_db()

        self.assertEqual(inv.status, "POSTED")
        self.assertEqual(inv.subtotal, 100000)
        self.assertEqual(inv.tax_total, 7500)
        self.assertEqual(inv.total, 107500)
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.UNPAID)
        self.assertTrue(inv.document_number.startswith("CFX-TBOOK-INV-"))

        # Journal: Dr AR 107,500 ; Cr Revenue 100,000 ; Cr VAT 7,500.
        debit, credit = inv.journal.totals()
        self.assertEqual(debit, credit)
        self.assertEqual(debit, 107500)
        ar_bal = AccountBalance.objects.get(account__code="1200", period=period)
        self.assertEqual(ar_bal.debit_total, 107500)

    def test_invoice_in_closed_period_is_rejected(self):
        entity, period, customer, vat = self.build_ar(period_status=PeriodStatus.CLOSED)
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        with self.assertRaises(PeriodClosedError):
            post_invoice(inv)
        inv.refresh_from_db()
        self.assertEqual(inv.status, "DRAFT")
        # Rejection durably audited.
        self.assertTrue(
            FinanceAuditLog.objects.filter(
                action=FinanceAuditAction.INVOICE_POSTED,
                status=FinanceAuditStatus.FAILED, target_id=str(inv.pk),
            ).exists()
        )


class PaymentAllocationTests(_ARFixtureMixin, TestCase):
    def test_partial_then_full_payment_moves_status_and_aging(self):
        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        post_invoice(inv)  # total ₦1,000 = 100,000 kobo

        bank = Account.objects.get(entity=entity, code="1100")
        pay1 = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 12),
            amount=40000, deposit_account=bank,
        )
        post_payment(pay1)  # auto-allocates oldest-first
        inv.refresh_from_db()
        self.assertEqual(inv.amount_paid, 40000)
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PARTIAL)
        self.assertEqual(inv.balance_due, 60000)

        pay2 = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 20),
            amount=60000, deposit_account=bank,
        )
        post_payment(pay2)
        inv.refresh_from_db()
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(inv.balance_due, 0)

        # Bank debited twice; AR credited twice → AR control nets to zero here.
        bank_bal = AccountBalance.objects.get(account__code="1100", period=period)
        self.assertEqual(bank_bal.debit_total, 100000)

    def test_overpayment_leaves_unallocated_credit(self):
        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(inv)  # 50,000

        bank = Account.objects.get(entity=entity, code="1100")
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=70000, deposit_account=bank,
        )
        post_payment(pay)
        pay.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(pay.allocated_amount, 50000)
        self.assertEqual(pay.unallocated_amount, 20000)


class ARReconciliationTests(_ARFixtureMixin, TestCase):
    def test_aging_buckets_and_control_reconciles(self):
        entity, period, customer, vat = self.build_ar()
        # One invoice due 2026-01-25, viewed as of 2026-03-01 → ~35 days overdue.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        post_invoice(inv)

        report = ar_aging(entity, as_of=datetime.date(2026, 3, 1))
        row = report.rows[0]
        self.assertEqual(row.outstanding, 100000)
        self.assertEqual(row.buckets["31-60"], 100000)
        self.assertEqual(report.total_net, 100000)

        # Sub-ledger (customer balances) must equal the AR control GL balance.
        rec = reconcile_ar(entity, as_of=datetime.date(2026, 3, 1))
        self.assertTrue(rec.is_reconciled)
        self.assertEqual(rec.control_total, 100000)

    def test_reconciles_after_partial_payment(self):
        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        post_invoice(inv)
        bank = Account.objects.get(entity=entity, code="1100")
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 18),
            amount=30000, deposit_account=bank,
        )
        post_payment(pay)

        rec = reconcile_ar(entity)
        self.assertTrue(rec.is_reconciled)
        self.assertEqual(rec.subledger_total, 70000)
        self.assertEqual(rec.control_total, 70000)


# =========================================================================== #
# Phase 4 — banking, expenses, payroll, budget, fixed assets, period close     #
# =========================================================================== #


class _Phase4FixtureMixin(_GLFixtureMixin):
    """A ledger with a full year of monthly periods and a bank account on 1100."""

    def build_books(self, *, period_status=PeriodStatus.OPEN):
        seed_currencies()
        entity = LedgerEntity.objects.create(
            name="Test Books", code="TBOOK", kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(entity)
        year = FiscalYear.objects.create(
            entity=entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        periods = []
        for m in range(1, 13):
            start = datetime.date(2026, m, 1)
            end = (datetime.date(2026, m + 1, 1) if m < 12 else datetime.date(2027, 1, 1))
            end = end - datetime.timedelta(days=1)
            periods.append(FiscalPeriod.objects.create(
                entity=entity, fiscal_year=year, period_no=m,
                name=f"2026-{m:02d}", start_date=start, end_date=end,
                status=period_status,
            ))
        return entity, year, periods

    def make_bank(self, entity, *, gl_code="1100"):
        return BankAccount.objects.create(
            entity=entity, name="GTBank Operations",
            gl_account=Account.objects.get(entity=entity, code=gl_code),
        )


class BankReconciliationTests(_Phase4FixtureMixin, TestCase):
    def test_import_is_idempotent_on_external_id(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        rows = [
            {"txn_date": datetime.date(2026, 1, 5), "amount": 50000, "external_id": "A1"},
            {"txn_date": datetime.date(2026, 1, 6), "amount": -2000, "external_id": "A2"},
        ]
        created = import_statement_lines(bank, rows)
        self.assertEqual(len(created), 2)
        # Re-import the same export: nothing new.
        again = import_statement_lines(bank, rows)
        self.assertEqual(again, [])
        self.assertEqual(BankStatementLine.objects.filter(bank_account=bank).count(), 2)

    def test_auto_reconcile_matches_by_amount_and_date(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        # A cash inflow of +50,000 posted on 2026-01-15.
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],
            date=datetime.date(2026, 1, 15),
        ))
        import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 16), "amount": 50000, "external_id": "S1"},
            {"txn_date": datetime.date(2026, 1, 16), "amount": 99999, "external_id": "S2"},
        ])
        matched = auto_reconcile(bank, tolerance_days=4)
        self.assertEqual(len(matched), 1)
        s1 = BankStatementLine.objects.get(external_id="S1")
        self.assertEqual(s1.status, BankLineStatus.MATCHED)
        self.assertIsNotNone(s1.matched_line)
        # The unmatched, amount-mismatched line is left for a human.
        s2 = BankStatementLine.objects.get(external_id="S2")
        self.assertEqual(s2.status, BankLineStatus.UNMATCHED)

    def test_manual_match_rejects_amount_mismatch(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        entry = self.make_entry(
            entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)],
        )
        post_journal(entry)
        gl_line = entry.lines.get(account__code="1100")
        line = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 15), "amount": 31000},
        ])[0]
        with self.assertRaises(BankReconciliationError):
            match_line(line, gl_line)
        # Correct amount matches cleanly.
        line.amount = 30000
        line.save(update_fields=["amount"])
        match_line(line, gl_line)
        line.refresh_from_db()
        self.assertEqual(line.status, BankLineStatus.MATCHED)

    def test_post_bank_adjustment_books_charge_and_matches(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        line = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 20), "amount": -1500,
             "description": "Monthly fee"},
        ])[0]
        entry = post_bank_adjustment(line)
        line.refresh_from_db()
        self.assertEqual(line.status, BankLineStatus.MATCHED)
        self.assertEqual(line.adjusting_journal_id, entry.id)
        # Outflow: Dr 5500 Bank Charges, Cr 1100 cash.
        charge = entry.lines.get(account__code="5500")
        cash = entry.lines.get(account__code="1100")
        self.assertEqual(charge.debit, 1500)
        self.assertEqual(cash.credit, 1500)

    def test_adjustment_rejects_already_matched_line(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        line = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 20), "amount": -1500},
        ])[0]
        post_bank_adjustment(line)
        with self.assertRaises(BankReconciliationError):
            post_bank_adjustment(line)


class ExpenseClaimTests(_Phase4FixtureMixin, TestCase):
    def _make_claim(self, entity, *, lines):
        claim = ExpenseClaim.objects.create(
            entity=entity, claimant_name="Jane Staff",
            claim_date=datetime.date(2026, 1, 10), title="Trip",
        )
        for i, (code, qty, price, tax) in enumerate(lines, start=1):
            ExpenseClaimLine.objects.create(
                claim=claim, expense_account=Account.objects.get(entity=entity, code=code),
                quantity=qty, unit_price=price, tax_code=tax, line_no=i,
            )
        return claim

    def test_post_raises_liability_with_input_vat(self):
        entity, _, _ = self.build_books()
        vat = TaxCode.objects.create(
            entity=entity, code="VAT", name="VAT 7.5%", rate_bps=750,
            paid_account=Account.objects.get(entity=entity, code="1300"),  # input VAT
        )
        claim = self._make_claim(entity, lines=[("5500", 1, 100000, vat)])
        post_expense_claim(claim)
        claim.refresh_from_db()
        self.assertEqual(claim.status, DocumentStatus.POSTED)
        self.assertEqual(claim.subtotal, 100000)
        self.assertEqual(claim.tax_total, 7500)
        self.assertEqual(claim.total, 107500)
        self.assertEqual(claim.payment_status, InvoicePaymentStatus.UNPAID)
        # Dr expense 100,000 + Dr input VAT 7,500 ; Cr accrued reimbursement 107,500.
        debit, credit = claim.journal.totals()
        self.assertEqual(debit, credit)
        self.assertEqual(debit, 107500)
        reimb = claim.journal.lines.get(account__code="2400")
        self.assertEqual(reimb.credit, 107500)

    def test_settle_partial_then_full(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        claim = self._make_claim(entity, lines=[("5500", 1, 100000, None)])
        post_expense_claim(claim)

        settle_expense_claim(
            claim, bank_account=bank, pay_date=datetime.date(2026, 1, 15), amount=40000,
        )
        claim.refresh_from_db()
        self.assertEqual(claim.amount_paid, 40000)
        self.assertEqual(claim.payment_status, InvoicePaymentStatus.PARTIAL)
        self.assertEqual(claim.balance_due, 60000)

        settle_expense_claim(claim, bank_account=bank, pay_date=datetime.date(2026, 1, 20))
        claim.refresh_from_db()
        self.assertEqual(claim.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(claim.balance_due, 0)

    def test_cannot_post_empty_claim(self):
        entity, _, _ = self.build_books()
        claim = ExpenseClaim.objects.create(
            entity=entity, claimant_name="Nobody",
            claim_date=datetime.date(2026, 1, 10),
        )
        with self.assertRaises(ExpenseClaimError):
            post_expense_claim(claim)


class PayrollTests(_Phase4FixtureMixin, TestCase):
    def _make_run(self, entity, *, lines):
        run = PayrollRun.objects.create(
            entity=entity, pay_date=datetime.date(2026, 1, 28), period_label="Jan 2026",
        )
        for i, (name, gross, paye, pension) in enumerate(lines, start=1):
            PayrollLine.objects.create(
                run=run, employee_name=name, gross_amount=gross,
                paye_amount=paye, pension_amount=pension, line_no=i,
            )
        return run

    def test_accrual_posts_balanced_with_statutory_liabilities(self):
        entity, _, _ = self.build_books()
        run = self._make_run(entity, lines=[
            ("Ada", 300000, 30000, 15000),   # net 255,000
            ("Bola", 200000, 20000, 10000),  # net 170,000
        ])
        post_payroll(run)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.POSTED)
        self.assertEqual(run.gross_total, 500000)
        self.assertEqual(run.paye_total, 50000)
        self.assertEqual(run.pension_total, 25000)
        self.assertEqual(run.net_total, 425000)
        # Dr 5200 gross ; Cr 2310 PAYE, 2320 pension, 2330 net.
        debit, credit = run.journal.totals()
        self.assertEqual(debit, credit)
        self.assertEqual(run.journal.lines.get(account__code="5200").debit, 500000)
        self.assertEqual(run.journal.lines.get(account__code="2330").credit, 425000)

    def test_disburse_clears_net_payable(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        run = self._make_run(entity, lines=[("Ada", 300000, 30000, 15000)])
        post_payroll(run)
        pay_payroll(run, bank_account=bank)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.PAID)
        # Dr 2330 net payable ; Cr 1100 bank.
        disb = run.disbursement_journal
        self.assertEqual(disb.lines.get(account__code="2330").debit, 255000)
        self.assertEqual(disb.lines.get(account__code="1100").credit, 255000)

    def test_negative_net_is_rejected(self):
        entity, _, _ = self.build_books()
        run = self._make_run(entity, lines=[("Greedy", 100000, 80000, 30000)])  # net -10,000
        with self.assertRaises(PayrollError):
            post_payroll(run)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.DRAFT)

    def test_cannot_pay_unposted_run(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        run = self._make_run(entity, lines=[("Ada", 300000, 30000, 15000)])
        with self.assertRaises(PayrollError):
            pay_payroll(run, bank_account=bank)


class BudgetTests(_Phase4FixtureMixin, TestCase):
    def test_approve_locks_lines_against_edits(self):
        entity, year, _ = self.build_books()
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")
        salaries = Account.objects.get(entity=entity, code="5200")
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)
        approve_budget(budget)
        budget.refresh_from_db()
        self.assertEqual(budget.status, BudgetStatus.APPROVED)
        self.assertTrue(budget.is_locked)
        with self.assertRaises(BudgetError):
            add_budget_line(budget, account=salaries, period_no=2, amount=10000)

    def test_period_no_must_be_in_range(self):
        entity, year, _ = self.build_books()
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")
        salaries = Account.objects.get(entity=entity, code="5200")
        with self.assertRaises(BudgetError):
            add_budget_line(budget, account=salaries, period_no=13, amount=10000)

    def test_budget_vs_actual_variance(self):
        entity, year, periods = self.build_books()
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")
        salaries = Account.objects.get(entity=entity, code="5200")
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)
        # Actual salary spend of 50,000 in Jan.
        post_journal(self.make_entry(
            entity, periods[0], [("5200", 50000, 0), ("1100", 0, 50000)],
            date=datetime.date(2026, 1, 15),
        ))
        report = budget_vs_actual(budget)
        row = next(r for r in report.rows if r.code == "5200")
        self.assertEqual(row.budget, 60000)
        self.assertEqual(row.actual, 50000)
        self.assertEqual(row.variance, -10000)        # under budget
        self.assertEqual(report.total_budget, 60000)
        self.assertEqual(report.total_actual, 50000)

    def test_budget_vs_actual_scoped_to_period(self):
        entity, year, periods = self.build_books()
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")
        salaries = Account.objects.get(entity=entity, code="5200")
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)
        add_budget_line(budget, account=salaries, period_no=2, amount=60000)
        post_journal(self.make_entry(
            entity, periods[1], [("5200", 70000, 0), ("1100", 0, 70000)],
            date=datetime.date(2026, 2, 15),
        ))
        feb = budget_vs_actual(budget, period_no=2)
        row = next(r for r in feb.rows if r.code == "5200")
        self.assertEqual(row.budget, 60000)
        self.assertEqual(row.actual, 70000)
        self.assertEqual(row.variance, 10000)         # over budget


class FixedAssetTests(_Phase4FixtureMixin, TestCase):
    def _make_asset(self, entity, *, cost=1100000, salvage=0, life=11,
                    acq=datetime.date(2026, 1, 1)):
        return FixedAsset.objects.create(
            entity=entity, name="Server rack", acquisition_date=acq,
            cost=cost, salvage_value=salvage, useful_life_months=life,
        )

    def test_acquire_capitalises_and_builds_schedule(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        asset = self._make_asset(entity)
        acquire_asset(asset, bank_account=bank)
        asset.refresh_from_db()
        self.assertEqual(asset.asset_status, AssetStatus.ACTIVE)
        # Dr 1500 PP&E, Cr 1100 cash.
        self.assertEqual(asset.acquisition_journal.lines.get(account__code="1500").debit, 1100000)
        # Schedule sums to the depreciable base exactly.
        rows = list(asset.schedule.all())
        self.assertEqual(len(rows), 11)
        self.assertEqual(sum(r.amount for r in rows), asset.depreciable_base)

    def test_schedule_remainder_lands_on_last_period(self):
        entity, _, _ = self.build_books()
        asset = self._make_asset(entity, cost=1000000, salvage=0, life=3)
        build_depreciation_schedule(asset)
        amounts = [r.amount for r in asset.schedule.all()]
        # 1,000,000 / 3 = 333,333 r1 → last row carries the extra kobo.
        self.assertEqual(amounts, [333333, 333333, 333334])
        self.assertEqual(sum(amounts), 1000000)

    def test_post_depreciation_runs_and_completes(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)
        acquire_asset(asset, bank_account=bank)
        # Schedule charges Feb–Dec 2026 (100,000 each). Post the lot.
        posted = post_depreciation(asset, up_to_date=datetime.date(2026, 12, 31))
        asset.refresh_from_db()
        self.assertEqual(len(posted), 11)
        self.assertEqual(asset.accumulated_depreciation, 1100000)
        self.assertEqual(asset.asset_status, AssetStatus.FULLY_DEPRECIATED)
        self.assertEqual(asset.net_book_value, 0)
        # Each charge: Dr 5400 expense, Cr 1900 accumulated depreciation.
        one = posted[0].journal
        self.assertEqual(one.lines.get(account__code="5400").debit, 100000)
        self.assertEqual(one.lines.get(account__code="1900").credit, 100000)

    def test_cannot_rebuild_schedule_after_posting(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        asset = self._make_asset(entity)
        acquire_asset(asset, bank_account=bank)
        post_depreciation(asset, up_to_date=datetime.date(2026, 2, 28))
        with self.assertRaises(DepreciationError):
            build_depreciation_schedule(asset)


class PeriodCloseTests(_Phase4FixtureMixin, TestCase):
    def test_checklist_passes_on_clean_ledger(self):
        entity, _, periods = self.build_books()
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],
        ))
        checklist = close_checklist(entity, periods[0])
        self.assertTrue(checklist.passed)
        names = {i.name for i in checklist.items}
        self.assertIn("trial_balance_balanced", names)
        self.assertIn("ar_reconciled", names)
        self.assertIn("depreciation_posted", names)

    def test_close_reopen_and_lock_cycle(self):
        entity, _, periods = self.build_books()
        jan = periods[0]
        post_journal(self.make_entry(
            entity, jan, [("1100", 50000, 0), ("4100", 0, 50000)],
        ))
        period, checklist = close_period(entity, jan)
        self.assertEqual(period.status, PeriodStatus.CLOSED)
        self.assertIsNotNone(period.closed_at)

        reopen_period(entity, jan)
        jan.refresh_from_db()
        self.assertEqual(jan.status, PeriodStatus.OPEN)
        self.assertIsNone(jan.closed_at)

        close_period(entity, jan)
        lock_period(entity, jan)
        jan.refresh_from_db()
        self.assertEqual(jan.status, PeriodStatus.LOCKED)
        # A LOCKED period cannot be reopened.
        with self.assertRaises(PeriodCloseError):
            reopen_period(entity, jan)

    def test_soft_close_allows_depreciation_auto_posting(self):
        entity, _, periods = self.build_books()
        period, _ = close_period(entity, periods[0], soft=True)
        self.assertEqual(period.status, PeriodStatus.SOFT_CLOSED)

    def test_blocking_failure_requires_force(self):
        entity, _, periods = self.build_books()
        jan = periods[0]
        # Post straight into the AR control with no sub-ledger invoice → control != sub-ledger.
        ar = Account.objects.get(entity=entity, code="1200")
        Customer.objects.create(entity=entity, code="C1", name="Acme", receivable_account=ar)
        post_journal(self.make_entry(
            entity, jan, [("1200", 50000, 0), ("4100", 0, 50000)],
        ))
        with self.assertRaises(PeriodCloseError):
            close_period(entity, jan)
        jan.refresh_from_db()
        self.assertEqual(jan.status, PeriodStatus.OPEN)
        # Forcing over the failure closes it anyway.
        period, checklist = close_period(entity, jan, force=True)
        self.assertEqual(period.status, PeriodStatus.CLOSED)
        self.assertFalse(checklist.passed)

    def test_extra_checks_are_injected(self):
        entity, _, periods = self.build_books()
        calls = []

        def failing_check():
            calls.append(True)
            return ("ap_reconciled", False, "sub-ledger 100 vs control 0")

        with self.assertRaises(PeriodCloseError):
            close_period(entity, periods[0], extra_checks=[failing_check])
        self.assertTrue(calls)  # the injected check actually ran
