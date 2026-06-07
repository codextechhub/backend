"""Phase-0 foundation + Phase-1 GL tests: money, guards, entities, numbering, and
the double-entry ledger (chart of accounts, posting, reversal, trial balance)."""
from __future__ import annotations

import datetime
from decimal import Decimal

from django.db.models import Sum
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
    CreditNoteKind,
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
    PettyCashError,
    PettyCashOverdrawError,
    TaxFilingError,
)
from vs_finance.models import (
    Account,
    AccountBalance,
    BankAccount,
    BankStatementLine,
    Budget,
    Concession,
    CreditNote,
    CreditNoteLine,
    Customer,
    DepreciationSchedule,
    DunningNotice,
    DunningPolicy,
    DunningStage,
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
    PaymentPlan,
    PaymentPlanInstallment,
    PayrollLine,
    PayrollRun,
    PettyCashFund,
    PettyCashVoucher,
    PettyCashVoucherLine,
    Refund,
    TaxCode,
    TaxFiling,
    TaxObligation,
)
from vs_finance.money import format_naira, to_kobo, to_naira
from vs_finance.numbering import next_document_number
from vs_finance.posting import ensure_balanced, ensure_period_open, post_journal, reverse_journal
from vs_finance.receivables import post_invoice, post_payment
from vs_finance.credit_notes import (
    allocate_credit_note,
    post_credit_note,
    post_refund,
    write_off_invoice,
)
from vs_finance.installments import (
    activate_payment_plan,
    build_installments,
    cancel_payment_plan,
    post_concession,
    refresh_plan_progress,
    split_amount,
)
from vs_finance.dunning import (
    cancel_notice,
    ensure_default_policy,
    generate_dunning,
    mark_notice_sent,
)
from vs_finance.reports import (
    ar_aging,
    balance_sheet,
    budget_vs_actual,
    cash_flow_statement,
    customer_statement,
    income_statement,
    reconcile_ar,
    trial_balance,
)
from vs_finance.banking import (
    auto_reconcile,
    import_statement_lines,
    match_line,
    post_bank_adjustment,
)
from vs_finance.expenses import post_expense_claim, settle_expense_claim
from vs_finance.petty_cash import (
    establish_fund,
    fund_status,
    post_voucher,
    replenish_fund,
)
from vs_finance.tax_filing import (
    file_filing,
    outstanding_obligations,
    pay_filing,
    prepare_filing,
)
from vs_finance.constants import TaxFilingStatus, TaxObligationType
from vs_finance.payroll import pay_payroll, post_payroll
from vs_finance.budgets import add_budget_line, approve_budget
from vs_finance.assets import acquire_asset, build_depreciation_schedule, post_depreciation
from vs_finance.close import (
    close_checklist,
    close_period,
    lock_period,
    reopen_period,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies, seed_tax_obligations
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


class CreditNoteTests(_ARFixtureMixin, TestCase):
    def test_credit_note_posts_reverses_ar_and_applies_to_invoice(self):
        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, vat)])
        post_invoice(inv)  # total 107,500 (Dr AR)

        note = CreditNote.objects.create(
            entity=entity, customer=customer, kind=CreditNoteKind.CREDIT,
            note_date=datetime.date(2026, 1, 15), invoice=inv, reason="Returned goods",
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=Account.objects.get(entity=entity, code="4900"),
            quantity=1, unit_price=40000, tax_code=vat, line_no=1,
        )
        post_credit_note(note, auto_allocate=True)
        note.refresh_from_db()
        inv.refresh_from_db()

        # CRN total = 40,000 + 7.5% = 43,000; balanced journal that credits AR.
        self.assertEqual(note.status, "POSTED")
        self.assertEqual(note.total, 43000)
        self.assertTrue(note.document_number.startswith("CFX-TBOOK-CRN-"))
        debit, credit = note.journal.totals()
        self.assertEqual(debit, credit)
        self.assertEqual(credit, 43000)

        # Applied to the invoice as a non-cash reduction.
        self.assertEqual(inv.amount_credited, 43000)
        self.assertEqual(inv.balance_due, 107500 - 43000)
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PARTIAL)

        # AR control nets to the reduced balance.
        rec = reconcile_ar(entity, as_of=datetime.date(2026, 2, 1))
        self.assertTrue(rec.is_reconciled)
        self.assertEqual(rec.control_total, 64500)

    def test_debit_note_increases_ar_and_cannot_be_allocated(self):
        entity, period, customer, vat = self.build_ar()
        note = CreditNote.objects.create(
            entity=entity, customer=customer, kind=CreditNoteKind.DEBIT,
            note_date=datetime.date(2026, 1, 20), reason="Under-billed",
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=Account.objects.get(entity=entity, code="4100"),
            quantity=1, unit_price=25000, tax_code=None, line_no=1,
        )
        post_credit_note(note)
        note.refresh_from_db()
        self.assertEqual(note.total, 25000)
        self.assertTrue(note.document_number.startswith("CFX-TBOOK-DRN-"))
        # Dr AR (debit note raises the receivable).
        ar_bal = AccountBalance.objects.get(account__code="1200", period=period)
        self.assertEqual(ar_bal.debit_total, 25000)
        with self.assertRaises(PostingError):
            allocate_credit_note(note)

    def test_refund_pays_out_credit_balance(self):
        entity, period, customer, vat = self.build_ar()
        bank = Account.objects.get(entity=entity, code="1100")
        refund = Refund.objects.create(
            entity=entity, customer=customer, refund_date=datetime.date(2026, 1, 18),
            amount=30000, deposit_account=bank,
        )
        post_refund(refund)
        refund.refresh_from_db()
        self.assertEqual(refund.status, "POSTED")
        self.assertTrue(refund.document_number.startswith("CFX-TBOOK-RFD-"))
        debit, credit = refund.journal.totals()
        self.assertEqual(debit, credit)
        # Dr AR, Cr bank.
        ar_bal = AccountBalance.objects.get(account__code="1200", period=period)
        bank_bal = AccountBalance.objects.get(account__code="1100", period=period)
        self.assertEqual(ar_bal.debit_total, 30000)
        self.assertEqual(bank_bal.credit_total, 30000)

    def test_write_off_clears_balance_as_bad_debt(self):
        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        post_invoice(inv)  # 100,000 outstanding

        write_off_invoice(inv, write_off_date=datetime.date(2026, 1, 28))
        inv.refresh_from_db()
        self.assertEqual(inv.amount_credited, 100000)
        self.assertEqual(inv.balance_due, 0)
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PAID)
        # Dr bad-debt expense (5300), Cr AR.
        exp_bal = AccountBalance.objects.get(account__code="5300", period=period)
        self.assertEqual(exp_bal.debit_total, 100000)
        self.assertTrue(
            FinanceAuditLog.objects.filter(
                action=FinanceAuditAction.INVOICE_WRITTEN_OFF, target_id=str(inv.pk),
            ).exists()
        )

    def test_write_off_rejected_when_nothing_outstanding(self):
        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(inv)
        bank = Account.objects.get(entity=entity, code="1100")
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=50000, deposit_account=bank,
        )
        post_payment(pay)
        inv.refresh_from_db()
        with self.assertRaises(PostingError):
            write_off_invoice(inv)


class ConcessionTests(_ARFixtureMixin, TestCase):
    def test_discount_reduces_invoice_and_posts_to_allowances(self):
        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        post_invoice(inv)  # 100,000 outstanding

        concession = Concession.objects.create(
            entity=entity, customer=customer, invoice=inv, kind="DISCOUNT",
            concession_date=datetime.date(2026, 1, 16), amount=20000,
            reason="Early-settlement discount",
        )
        post_concession(concession)
        concession.refresh_from_db()
        inv.refresh_from_db()

        self.assertEqual(concession.status, "POSTED")
        self.assertTrue(concession.document_number.startswith("CFX-TBOOK-CNC-"))
        # Dr 4910 Discounts & Concessions, Cr AR — balanced.
        debit, credit = concession.journal.totals()
        self.assertEqual(debit, credit)
        self.assertEqual(debit, 20000)
        disc_bal = AccountBalance.objects.get(account__code="4910", period=period)
        self.assertEqual(disc_bal.debit_total, 20000)

        # Invoice reduced via the non-cash credit path.
        self.assertEqual(inv.amount_credited, 20000)
        self.assertEqual(inv.balance_due, 80000)
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PARTIAL)
        self.assertTrue(
            FinanceAuditLog.objects.filter(
                action=FinanceAuditAction.CONCESSION_POSTED, target_id=str(concession.pk),
            ).exists()
        )

    def test_concession_rejected_when_amount_exceeds_balance(self):
        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(inv)
        concession = Concession.objects.create(
            entity=entity, customer=customer, invoice=inv, kind="WAIVER",
            concession_date=datetime.date(2026, 1, 16), amount=60000,
        )
        with self.assertRaises(PostingError):
            post_concession(concession)
        inv.refresh_from_db()
        self.assertEqual(inv.amount_credited, 0)


class PaymentPlanTests(_ARFixtureMixin, TestCase):
    def test_split_amount_is_integer_exact(self):
        parts = split_amount(100000, 3)
        self.assertEqual(parts, [33333, 33333, 33334])
        self.assertEqual(sum(parts), 100000)

    def test_plan_builds_dated_installments_and_tracks_settlement(self):
        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        post_invoice(inv)  # 100,000 outstanding

        plan = PaymentPlan.objects.create(
            entity=entity, customer=customer, invoice=inv,
            start_date=datetime.date(2026, 1, 10), frequency="MONTHLY",
            installment_count=4, total_amount=inv.balance_due,
        )
        build_installments(plan)
        self.assertTrue(plan.document_number.startswith("CFX-TBOOK-PPL-"))
        installs = list(plan.installments.order_by("seq_no"))
        self.assertEqual([i.amount for i in installs], [25000, 25000, 25000, 25000])
        self.assertEqual(
            [i.due_date for i in installs],
            [datetime.date(2026, 1, 10), datetime.date(2026, 2, 10),
             datetime.date(2026, 3, 10), datetime.date(2026, 4, 10)],
        )

        activate_payment_plan(plan)
        plan.refresh_from_db()
        self.assertEqual(plan.plan_status, "ACTIVE")

        # A ₦500 part-payment settles the first two installments oldest-first.
        bank = Account.objects.get(entity=entity, code="1100")
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 12),
            amount=50000, deposit_account=bank,
        )
        post_payment(pay)
        refresh_plan_progress(plan)
        plan.refresh_from_db()
        statuses = [i.status for i in plan.installments.order_by("seq_no")]
        self.assertEqual(statuses, ["PAID", "PAID", "PENDING", "PENDING"])
        self.assertEqual(plan.settled_total, 50000)
        self.assertEqual(plan.plan_status, "ACTIVE")

        # Settle the rest → plan completes.
        pay2 = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 22),
            amount=50000, deposit_account=bank,
        )
        post_payment(pay2)
        refresh_plan_progress(plan)
        plan.refresh_from_db()
        self.assertEqual(plan.plan_status, "COMPLETED")
        self.assertTrue(
            all(i.status == "PAID" for i in plan.installments.all())
        )

    def test_build_rejects_mismatched_explicit_amounts(self):
        entity, period, customer, vat = self.build_ar()
        plan = PaymentPlan.objects.create(
            entity=entity, customer=customer,
            start_date=datetime.date(2026, 1, 10), frequency="WEEKLY",
            installment_count=2, total_amount=100000,
        )
        with self.assertRaises(PostingError):
            build_installments(plan, amounts=[40000, 40000])  # sums to 80,000 ≠ 100,000

    def test_activate_requires_a_built_schedule(self):
        entity, period, customer, vat = self.build_ar()
        plan = PaymentPlan.objects.create(
            entity=entity, customer=customer,
            start_date=datetime.date(2026, 1, 10), frequency="MONTHLY",
            installment_count=3, total_amount=90000,
        )
        with self.assertRaises(PostingError):
            activate_payment_plan(plan)

    def test_cancel_marks_plan_cancelled(self):
        entity, period, customer, vat = self.build_ar()
        plan = PaymentPlan.objects.create(
            entity=entity, customer=customer,
            start_date=datetime.date(2026, 1, 10), frequency="MONTHLY",
            installment_count=2, total_amount=80000,
        )
        build_installments(plan)
        activate_payment_plan(plan)
        cancel_payment_plan(plan)
        plan.refresh_from_db()
        self.assertEqual(plan.plan_status, "CANCELLED")


class CustomerStatementTests(_ARFixtureMixin, TestCase):
    def test_statement_runs_balance_and_buckets_open_invoices(self):
        entity, period, customer, vat = self.build_ar()

        # Two invoices; one part-paid, one discounted via a concession.
        inv1 = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)],
                                 date=datetime.date(2026, 1, 5))
        post_invoice(inv1)  # +100,000
        inv2 = self.make_invoice(entity, customer, lines=[("4100", 1, 60000, None)],
                                 date=datetime.date(2026, 1, 18))
        post_invoice(inv2)  # +60,000

        bank = Account.objects.get(entity=entity, code="1100")
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 12),
            amount=40000, deposit_account=bank,
        )
        post_payment(pay)  # -40,000 against inv1

        concession = Concession.objects.create(
            entity=entity, customer=customer, invoice=inv1, kind="DISCOUNT",
            concession_date=datetime.date(2026, 1, 20), amount=10000,
        )
        post_concession(concession)  # -10,000 against inv1

        stmt = customer_statement(customer, end_date=datetime.date(2026, 1, 31))

        # Opening (no start_date) is zero; running movements net to the live balance.
        self.assertEqual(stmt.opening_balance, 0)
        self.assertEqual(stmt.total_debits, 160000)   # 100,000 + 60,000
        self.assertEqual(stmt.total_credits, 50000)   # 40,000 receipt + 10,000 discount
        self.assertEqual(stmt.closing_balance, 110000)
        # Entries are ordered and carry a running balance ending at the close.
        self.assertEqual([e.doc_type for e in stmt.entries],
                         ["Invoice", "Receipt", "Invoice", "Discount"])
        self.assertEqual(stmt.entries[-1].balance, 110000)
        # Aging sums the two still-open invoices' live balances.
        self.assertEqual(sum(stmt.aging.values()), 110000)

    def test_start_date_folds_prior_movements_into_opening_balance(self):
        entity, period, customer, vat = self.build_ar()
        early = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)],
                                  date=datetime.date(2026, 1, 3))
        post_invoice(early)
        later = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)],
                                  date=datetime.date(2026, 1, 20))
        post_invoice(later)

        stmt = customer_statement(
            customer, start_date=datetime.date(2026, 1, 10),
            end_date=datetime.date(2026, 1, 31),
        )
        # The 3 Jan invoice predates the window → opening balance, not an entry.
        self.assertEqual(stmt.opening_balance, 100000)
        self.assertEqual([e.document_number for e in stmt.entries],
                         [later.document_number])
        self.assertEqual(stmt.closing_balance, 150000)


class DunningTests(_ARFixtureMixin, TestCase):
    def test_ensure_default_policy_is_idempotent_with_a_ladder(self):
        entity, period, customer, vat = self.build_ar()
        p1 = ensure_default_policy(entity)
        p2 = ensure_default_policy(entity)
        self.assertEqual(p1.pk, p2.pk)
        self.assertTrue(p1.is_default)
        self.assertEqual(p1.stages.count(), 3)
        self.assertEqual(
            [s.min_days_overdue for s in p1.stages.order_by("level")], [1, 14, 30],
        )

    def test_generate_raises_notice_at_highest_qualifying_stage(self):
        entity, period, customer, vat = self.build_ar()
        ensure_default_policy(entity)
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)],
                                due=datetime.date(2026, 1, 25))
        post_invoice(inv)  # 100,000 outstanding, due 25 Jan

        notices = generate_dunning(entity, as_of=datetime.date(2026, 3, 1))  # 35 days late
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertEqual(notice.level, 3)            # Final notice (min 30 days)
        self.assertEqual(notice.notice_status, "PENDING")
        self.assertEqual(notice.amount_due, 100000)
        self.assertEqual(notice.days_overdue, 35)
        self.assertTrue(notice.document_number.startswith("CFX-TBOOK-DUN-"))
        self.assertTrue(
            FinanceAuditLog.objects.filter(action="DUNNING_RUN_GENERATED").exists()
        )

    def test_generate_is_idempotent_per_invoice_level(self):
        entity, period, customer, vat = self.build_ar()
        ensure_default_policy(entity)
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 80000, None)],
                                due=datetime.date(2026, 1, 25))
        post_invoice(inv)

        first = generate_dunning(entity, as_of=datetime.date(2026, 2, 20))  # ~26 days → level 2
        second = generate_dunning(entity, as_of=datetime.date(2026, 2, 20))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].level, 2)
        self.assertEqual(len(second), 0)  # same level already issued → no duplicate
        self.assertEqual(DunningNotice.objects.filter(invoice=inv).count(), 1)

    def test_not_yet_due_invoice_is_skipped(self):
        entity, period, customer, vat = self.build_ar()
        ensure_default_policy(entity)
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)],
                                due=datetime.date(2026, 1, 25))
        post_invoice(inv)
        notices = generate_dunning(entity, as_of=datetime.date(2026, 1, 20))  # before due
        self.assertEqual(notices, [])

    def test_settled_invoice_marks_notice_resolved_and_no_new_one(self):
        entity, period, customer, vat = self.build_ar()
        ensure_default_policy(entity)
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)],
                                due=datetime.date(2026, 1, 25))
        post_invoice(inv)
        notice = generate_dunning(entity, as_of=datetime.date(2026, 3, 1))[0]

        # Customer pays in full; the next run resolves the open notice, raises nothing new.
        bank = Account.objects.get(entity=entity, code="1100")
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 28),
            amount=100000, deposit_account=bank,
        )
        post_payment(pay)
        again = generate_dunning(entity, as_of=datetime.date(2026, 3, 2))
        notice.refresh_from_db()
        self.assertEqual(again, [])
        self.assertEqual(notice.notice_status, "RESOLVED")

    def test_mark_sent_then_cancel_lifecycle(self):
        entity, period, customer, vat = self.build_ar()
        ensure_default_policy(entity)
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 60000, None)],
                                due=datetime.date(2026, 1, 25))
        post_invoice(inv)
        notice = generate_dunning(entity, as_of=datetime.date(2026, 3, 1))[0]

        mark_notice_sent(notice)
        notice.refresh_from_db()
        self.assertEqual(notice.notice_status, "SENT")
        self.assertIsNotNone(notice.sent_at)

        cancel_notice(notice, reason="Customer disputed")
        notice.refresh_from_db()
        self.assertEqual(notice.notice_status, "CANCELLED")


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


class PettyCashTests(_Phase4FixtureMixin, TestCase):
    def _make_fund(self, entity, *, name="Front Desk", float_amount=5000000, gl_code="1110"):
        return PettyCashFund.objects.create(
            entity=entity, name=name, custodian_name="Tunde Custodian",
            gl_account=Account.objects.get(entity=entity, code=gl_code),
            float_amount=float_amount,
        )

    def _make_voucher(self, fund, *, lines, voucher_date=datetime.date(2026, 1, 12)):
        voucher = PettyCashVoucher.objects.create(
            entity=fund.entity, fund=fund, voucher_date=voucher_date,
            payee="Corner Shop",
        )
        for i, (code, qty, price, tax) in enumerate(lines, start=1):
            PettyCashVoucherLine.objects.create(
                voucher=voucher,
                expense_account=Account.objects.get(entity=fund.entity, code=code),
                quantity=qty, unit_price=price, tax_code=tax, line_no=i,
            )
        return voucher

    def test_establish_moves_cash_from_bank_to_tin(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = self._make_fund(entity, float_amount=5000000)
        entry = establish_fund(
            fund, bank_account=bank, amount=5000000, date=datetime.date(2026, 1, 1),
        )
        fund.refresh_from_db()
        self.assertEqual(fund.current_balance, 5000000)
        # Dr 1110 petty cash 5,000,000 ; Cr 1100 bank 5,000,000.
        debit, credit = entry.totals()
        self.assertEqual(debit, credit)
        self.assertEqual(entry.lines.get(account__code="1110").debit, 5000000)
        self.assertEqual(entry.lines.get(account__code="1100").credit, 5000000)

    def test_establish_rejects_non_positive(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = self._make_fund(entity)
        with self.assertRaises(PettyCashError):
            establish_fund(fund, bank_account=bank, amount=0, date=datetime.date(2026, 1, 1))

    def test_voucher_posts_expense_and_lowers_balance(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = self._make_fund(entity, float_amount=5000000)
        establish_fund(fund, bank_account=bank, amount=5000000, date=datetime.date(2026, 1, 1))
        vat = TaxCode.objects.create(
            entity=entity, code="VAT", name="VAT 7.5%", rate_bps=750,
            paid_account=Account.objects.get(entity=entity, code="1300"),
        )
        voucher = self._make_voucher(fund, lines=[("5500", 1, 100000, vat)])
        post_voucher(voucher)
        voucher.refresh_from_db()
        fund.refresh_from_db()
        self.assertEqual(voucher.status, DocumentStatus.POSTED)
        self.assertEqual(voucher.subtotal, 100000)
        self.assertEqual(voucher.tax_total, 7500)
        self.assertEqual(voucher.total, 107500)
        # Dr expense 100,000 + Dr input VAT 7,500 ; Cr petty cash 107,500.
        debit, credit = voucher.journal.totals()
        self.assertEqual(debit, credit)
        self.assertEqual(voucher.journal.lines.get(account__code="1110").credit, 107500)
        self.assertEqual(fund.current_balance, 5000000 - 107500)

    def test_voucher_overdraw_is_blocked_and_audited(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = self._make_fund(entity, float_amount=50000)
        establish_fund(fund, bank_account=bank, amount=50000, date=datetime.date(2026, 1, 1))
        voucher = self._make_voucher(fund, lines=[("5500", 1, 80000, None)])
        with self.assertRaises(PettyCashOverdrawError):
            post_voucher(voucher)
        voucher.refresh_from_db()
        fund.refresh_from_db()
        self.assertEqual(voucher.status, DocumentStatus.DRAFT)
        self.assertEqual(fund.current_balance, 50000)
        self.assertTrue(
            FinanceAuditLog.objects.filter(
                entity=entity,
                action=FinanceAuditAction.PETTY_CASH_VOUCHER_REJECTED,
                status=FinanceAuditStatus.FAILED,
            ).exists()
        )

    def test_replenish_restores_float_by_default(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = self._make_fund(entity, float_amount=5000000)
        establish_fund(fund, bank_account=bank, amount=5000000, date=datetime.date(2026, 1, 1))
        voucher = self._make_voucher(fund, lines=[("5500", 1, 1200000, None)])
        post_voucher(voucher)
        fund.refresh_from_db()
        self.assertEqual(fund.current_balance, 5000000 - 1200000)

        entry = replenish_fund(fund, bank_account=bank, date=datetime.date(2026, 1, 31))
        fund.refresh_from_db()
        self.assertEqual(fund.current_balance, 5000000)  # restored to float
        self.assertEqual(fund.last_replenished_at, datetime.date(2026, 1, 31))
        self.assertEqual(entry.lines.get(account__code="1110").debit, 1200000)
        self.assertEqual(entry.lines.get(account__code="1100").credit, 1200000)

    def test_replenish_with_nothing_to_top_up_is_rejected(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = self._make_fund(entity, float_amount=5000000)
        establish_fund(fund, bank_account=bank, amount=5000000, date=datetime.date(2026, 1, 1))
        with self.assertRaises(PettyCashError):
            replenish_fund(fund, bank_account=bank, date=datetime.date(2026, 1, 31))

    def test_fund_status_flags_low_balance(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = self._make_fund(entity, float_amount=1000000)
        establish_fund(fund, bank_account=bank, amount=1000000, date=datetime.date(2026, 1, 1))
        # Spend down to 20% of float — below the default 25% threshold.
        voucher = self._make_voucher(fund, lines=[("5500", 1, 800000, None)])
        post_voucher(voucher)
        rows = fund_status(entity)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_balance"], 200000)
        self.assertEqual(rows[0]["shortfall"], 800000)
        self.assertTrue(rows[0]["needs_replenish"])


class TaxFilingTests(_Phase4FixtureMixin, TestCase):
    def _vat_obligation(self, entity):
        # The fixture seeds a VAT obligation already; reuse it idempotently.
        ob, _ = TaxObligation.objects.update_or_create(
            entity=entity, code="VAT",
            defaults={
                "name": "Value Added Tax",
                "obligation_type": TaxObligationType.VAT,
                "liability_account": Account.objects.get(entity=entity, code="2200"),
                "recoverable_account": Account.objects.get(entity=entity, code="1300"),
                "authority_name": "FIRS",
            },
        )
        return ob

    def _wht_obligation(self, entity):
        ob, _ = TaxObligation.objects.update_or_create(
            entity=entity, code="WHT",
            defaults={
                "name": "Withholding Tax",
                "obligation_type": TaxObligationType.WHT,
                "liability_account": Account.objects.get(entity=entity, code="2300"),
                "recoverable_account": None,
                "authority_name": "FIRS",
            },
        )
        return ob

    def _accrue_output_vat(self, entity, period, *, net, vat, date=datetime.date(2026, 1, 10)):
        # A sale: Dr cash, Cr revenue, Cr output VAT.
        post_journal(self.make_entry(
            entity, period,
            [("1100", net + vat, 0), ("4100", 0, net), ("2200", 0, vat)],
            date=date,
        ))

    def _accrue_input_vat(self, entity, period, *, net, vat, date=datetime.date(2026, 1, 12)):
        # A purchase: Dr expense, Dr input VAT, Cr cash.
        post_journal(self.make_entry(
            entity, period,
            [("5300", net, 0), ("1300", vat, 0), ("1100", 0, net + vat)],
            date=date,
        ))

    def _accrue_wht(self, entity, period, *, amount, date=datetime.date(2026, 1, 12)):
        # A vendor payment withholding: Dr expense, Cr WHT payable, Cr cash.
        post_journal(self.make_entry(
            entity, period,
            [("5300", amount * 10, 0), ("2300", 0, amount), ("1100", 0, amount * 9)],
            date=date,
        ))

    def test_prepare_vat_nets_input_against_output(self):
        entity, _, periods = self.build_books()
        ob = self._vat_obligation(entity)
        self._accrue_output_vat(entity, periods[0], net=1000000, vat=75000)
        self._accrue_input_vat(entity, periods[0], net=400000, vat=30000)
        filing = prepare_filing(
            ob, period_start=datetime.date(2026, 1, 1), period_end=datetime.date(2026, 1, 31),
        )
        self.assertEqual(filing.filing_status, TaxFilingStatus.DRAFT)
        self.assertEqual(filing.gross_liability, 75000)
        self.assertEqual(filing.recoverable_amount, 30000)
        self.assertEqual(filing.amount_due, 45000)

    def test_prepare_is_idempotent_for_same_period(self):
        entity, _, periods = self.build_books()
        ob = self._vat_obligation(entity)
        self._accrue_output_vat(entity, periods[0], net=1000000, vat=75000)
        a = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                           period_end=datetime.date(2026, 1, 31))
        b = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                           period_end=datetime.date(2026, 1, 31))
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(TaxFiling.objects.filter(entity=entity, obligation=ob).count(), 1)

    def test_file_nets_input_vat_then_pay_clears_liability(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        ob = self._vat_obligation(entity)
        self._accrue_output_vat(entity, periods[0], net=1000000, vat=75000)
        self._accrue_input_vat(entity, periods[0], net=400000, vat=30000)
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                                period_end=datetime.date(2026, 1, 31))
        file_filing(filing, filed_date=datetime.date(2026, 2, 5), filing_reference="VAT-202601")
        filing.refresh_from_db()
        self.assertEqual(filing.filing_status, TaxFilingStatus.FILED)
        # Netting journal cleared input VAT 1300 against output 2200.
        self.assertIsNotNone(filing.filing_journal)
        self.assertEqual(filing.filing_journal.lines.get(account__code="1300").credit, 30000)
        self.assertEqual(filing.filing_journal.lines.get(account__code="2200").debit, 30000)

        pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 20))
        filing.refresh_from_db()
        self.assertEqual(filing.filing_status, TaxFilingStatus.PAID)
        self.assertEqual(filing.payment_status, InvoicePaymentStatus.PAID)
        # Output VAT control account 2200 is now flat: 75,000 Cr − 30,000 net − 45,000 paid.
        vat_acc = Account.objects.get(entity=entity, code="2200")
        agg = JournalLine.objects.filter(
            account=vat_acc, entry__status=DocumentStatus.POSTED,
        ).aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual((agg["c"] or 0) - (agg["d"] or 0), 0)

    def test_wht_filing_no_recoverable_pays_full(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        ob = self._wht_obligation(entity)
        self._accrue_wht(entity, periods[0], amount=50000)
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                                period_end=datetime.date(2026, 1, 31))
        self.assertEqual(filing.gross_liability, 50000)
        self.assertEqual(filing.recoverable_amount, 0)
        self.assertEqual(filing.amount_due, 50000)
        # No recoverable + no penalty → filing posts no journal.
        file_filing(filing, filed_date=datetime.date(2026, 2, 5))
        filing.refresh_from_db()
        self.assertIsNone(filing.filing_journal)
        pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 10))
        filing.refresh_from_db()
        self.assertEqual(filing.filing_status, TaxFilingStatus.PAID)
        # The remittance Dr 2300 / Cr bank flattens the WHT payable control account.
        for code in ("2300", "1100"):
            acc = Account.objects.get(entity=entity, code=code)
            agg = JournalLine.objects.filter(
                account=acc, entry__status=DocumentStatus.POSTED,
            ).aggregate(d=Sum("debit"), c=Sum("credit"))
            if code == "2300":
                self.assertEqual((agg["c"] or 0) - (agg["d"] or 0), 0)  # payable cleared
        # The bank-side remittance leg credited cash by 50,000.
        remit = JournalLine.objects.get(
            account__code="2300", entry__status=DocumentStatus.POSTED, debit=50000,
        )
        self.assertEqual(remit.entry.lines.get(account__code="1100").credit, 50000)

    def test_partial_remittance(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        ob = self._wht_obligation(entity)
        self._accrue_wht(entity, periods[0], amount=50000)
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                                period_end=datetime.date(2026, 1, 31))
        file_filing(filing, filed_date=datetime.date(2026, 2, 5))
        pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 10), amount=20000)
        filing.refresh_from_db()
        self.assertEqual(filing.payment_status, InvoicePaymentStatus.PARTIAL)
        self.assertEqual(filing.filing_status, TaxFilingStatus.FILED)
        self.assertEqual(filing.balance_due, 30000)
        pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 25))
        filing.refresh_from_db()
        self.assertEqual(filing.filing_status, TaxFilingStatus.PAID)
        self.assertEqual(filing.balance_due, 0)

    def test_file_with_penalty_books_expense_and_raises_due(self):
        entity, _, periods = self.build_books()
        ob = self._wht_obligation(entity)
        self._accrue_wht(entity, periods[0], amount=50000)
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                                period_end=datetime.date(2026, 1, 31))
        file_filing(
            filing, filed_date=datetime.date(2026, 3, 5),
            adjustment_amount=5000,
            adjustment_account=Account.objects.get(entity=entity, code="5300"),
        )
        filing.refresh_from_db()
        self.assertEqual(filing.adjustment_amount, 5000)
        self.assertEqual(filing.amount_due, 55000)
        # Dr 5300 penalty 5,000 ; Cr 2300 payable 5,000.
        self.assertEqual(filing.filing_journal.lines.get(account__code="5300").debit, 5000)
        self.assertEqual(filing.filing_journal.lines.get(account__code="2300").credit, 5000)

    def test_pay_before_file_is_rejected_and_audited(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        ob = self._wht_obligation(entity)
        self._accrue_wht(entity, periods[0], amount=50000)
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                                period_end=datetime.date(2026, 1, 31))
        with self.assertRaises(TaxFilingError):
            pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 10))
        filing.refresh_from_db()
        self.assertEqual(filing.filing_status, TaxFilingStatus.DRAFT)
        self.assertTrue(
            FinanceAuditLog.objects.filter(
                entity=entity,
                action=FinanceAuditAction.TAX_FILING_REJECTED,
                status=FinanceAuditStatus.FAILED,
            ).exists()
        )

    def test_outstanding_obligations_reports_net(self):
        entity, _, periods = self.build_books()
        ob = self._vat_obligation(entity)
        self._accrue_output_vat(entity, periods[0], net=1000000, vat=75000)
        self._accrue_input_vat(entity, periods[0], net=400000, vat=30000)
        rows = {r["code"]: r for r in outstanding_obligations(entity)}
        vat = rows["VAT"]
        self.assertEqual(vat["payable_balance"], 75000)
        self.assertEqual(vat["recoverable_balance"], 30000)
        self.assertEqual(vat["net_outstanding"], 45000)

    def test_seed_creates_four_nigerian_obligations(self):
        entity, _, _ = self.build_books()
        # seed_chart_of_accounts (run by the fixture) seeds obligations too.
        rows = TaxObligation.objects.filter(entity=entity).order_by("code")
        self.assertEqual(
            list(rows.values_list("code", flat=True)),
            ["PAYE", "PENSION", "VAT", "WHT"],
        )
        vat = rows.get(code="VAT")
        self.assertEqual(vat.liability_account.code, "2200")
        self.assertEqual(vat.recoverable_account.code, "1300")
        wht = rows.get(code="WHT")
        self.assertEqual(wht.liability_account.code, "2300")
        self.assertIsNone(wht.recoverable_account)
        # Re-running is idempotent — no duplicates.
        seed_tax_obligations(entity)
        self.assertEqual(TaxObligation.objects.filter(entity=entity).count(), 4)


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


class FinancialStatementTests(_Phase4FixtureMixin, TestCase):
    """The three primary statements over one coherent set of transactions.

    A tiny but complete first month:
      * owner injects 1,000,000 capital (financing inflow)
      * buys 400,000 of equipment for cash (investing outflow)
      * earns 300,000 cash revenue (operating inflow)
      * pays 120,000 cash salaries (operating outflow)
    """

    def _seed_activity(self, entity, period):
        post_journal(self.make_entry(
            entity, period, [("1100", 1000000, 0), ("3100", 0, 1000000)],
        ))  # capital
        post_journal(self.make_entry(
            entity, period, [("1500", 400000, 0), ("1100", 0, 400000)],
        ))  # buy equipment
        post_journal(self.make_entry(
            entity, period, [("1100", 300000, 0), ("4100", 0, 300000)],
        ))  # cash revenue
        post_journal(self.make_entry(
            entity, period, [("5200", 120000, 0), ("1100", 0, 120000)],
        ))  # salaries

    def test_income_statement_nets_revenue_less_expense(self):
        entity, _, periods = self.build_books()
        self._seed_activity(entity, periods[0])

        pnl = income_statement(entity, period=periods[0])
        self.assertEqual(pnl.total_income, 300000)
        self.assertEqual(pnl.total_expense, 120000)
        self.assertEqual(pnl.net_income, 180000)
        # Income rows carry positive (credit-natural) magnitudes.
        rev = next(r for r in pnl.income_rows if r.code == "4100")
        self.assertEqual(rev.amount, 300000)

    def test_income_statement_aggregates_all_periods_when_unscoped(self):
        entity, _, periods = self.build_books()
        # Revenue split across two months.
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 100000, 0), ("4100", 0, 100000)],
            date=datetime.date(2026, 1, 10),
        ))
        post_journal(self.make_entry(
            entity, periods[1], [("1100", 250000, 0), ("4100", 0, 250000)],
            date=datetime.date(2026, 2, 10),
        ))
        self.assertEqual(income_statement(entity).total_income, 350000)
        self.assertEqual(income_statement(entity, period=periods[0]).total_income, 100000)

    def test_balance_sheet_balances_with_unclosed_net_income(self):
        entity, _, periods = self.build_books()
        self._seed_activity(entity, periods[0])

        bs = balance_sheet(entity)
        self.assertEqual(bs.total_assets, 1180000)        # 780k cash + 400k PP&E
        self.assertEqual(bs.total_liabilities, 0)
        self.assertEqual(bs.total_equity_accounts, 1000000)  # share capital
        self.assertEqual(bs.retained_earnings, 180000)       # unclosed net income
        self.assertEqual(bs.total_equity, 1180000)
        self.assertTrue(bs.is_balanced)
        self.assertEqual(bs.difference, 0)

    def test_cash_flow_reconciles_and_classifies(self):
        entity, _, periods = self.build_books()
        self.make_bank(entity)  # 1100 is also a mapped bank account
        self._seed_activity(entity, periods[0])

        cf = cash_flow_statement(entity)
        self.assertEqual(cf.opening_cash, 0)
        self.assertEqual(cf.closing_cash, 780000)
        self.assertEqual(cf.by_activity["operating"], 180000)   # 300k rev - 120k pay
        self.assertEqual(cf.by_activity["investing"], -400000)  # equipment
        self.assertEqual(cf.by_activity["financing"], 1000000)  # capital
        self.assertEqual(cf.net_change, 780000)
        self.assertTrue(cf.is_reconciled)

    def test_cash_flow_ignores_non_cash_journals(self):
        entity, _, periods = self.build_books()
        # An accrual that never touches cash (Dr expense, Cr payable) must not move cash.
        post_journal(self.make_entry(
            entity, periods[0], [("5300", 50000, 0), ("2100", 0, 50000)],
        ))
        cf = cash_flow_statement(entity)
        self.assertEqual(cf.closing_cash, 0)
        self.assertEqual(cf.net_change, 0)
        self.assertTrue(cf.is_reconciled)


class FinanceAPITests(_Phase4FixtureMixin, TestCase):
    """The /v1/finance/ REST surface: entity scoping, reports, documents, actions.

    Authenticated as a Vision super admin, which bypasses the per-endpoint RBAC gate
    (so these tests exercise routing/serialisation, not the RBAC matrix itself).
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment

        User = get_user_model()
        self.user = User.objects.create_user(
            email="fin-admin@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="Finance", last_name="Admin",
        )
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(
            user=self.user, role=role, assignment_status="ACTIVE",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _seed(self):
        entity, _, periods = self.build_books()
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 1000000, 0), ("3100", 0, 1000000)],
        ))
        post_journal(self.make_entry(
            entity, periods[0], [("1500", 400000, 0), ("1100", 0, 400000)],
        ))
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 300000, 0), ("4100", 0, 300000)],
        ))
        post_journal(self.make_entry(
            entity, periods[0], [("5200", 120000, 0), ("1100", 0, 120000)],
        ))
        return entity, periods

    def test_entity_param_is_required_and_validated(self):
        entity, _ = self._seed()
        # Missing entity → 400.
        resp = self.client.get("/v1/finance/reports/trial-balance/")
        self.assertEqual(resp.status_code, 400)
        # Unknown entity → 404.
        resp = self.client.get("/v1/finance/reports/trial-balance/?entity=NOPE")
        self.assertEqual(resp.status_code, 404)
        # Known entity (by code) → 200.
        resp = self.client.get(f"/v1/finance/reports/trial-balance/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["data"]["is_balanced"])

    def test_entities_and_accounts_endpoints(self):
        entity, _ = self._seed()
        resp = self.client.get("/v1/finance/entities/")
        self.assertEqual(resp.status_code, 200)
        codes = {e["code"] for e in resp.json()["data"]}
        self.assertIn(entity.code, codes)

        resp = self.client.get(f"/v1/finance/accounts/?entity={entity.code}&account_type=ASSET")
        self.assertEqual(resp.status_code, 200)
        types = {a["account_type"] for a in resp.json()["data"]}
        self.assertEqual(types, {"ASSET"})

    def test_statement_endpoints_match_service_output(self):
        entity, _ = self._seed()
        ec = entity.code

        pnl = self.client.get(f"/v1/finance/reports/income-statement/?entity={ec}").json()["data"]
        self.assertEqual(pnl["net_income"]["kobo"], 180000)

        bs = self.client.get(f"/v1/finance/reports/balance-sheet/?entity={ec}").json()["data"]
        self.assertTrue(bs["is_balanced"])
        self.assertEqual(bs["total_assets"]["kobo"], 1180000)
        self.assertEqual(bs["retained_earnings"]["kobo"], 180000)

        cf = self.client.get(f"/v1/finance/reports/cash-flow/?entity={ec}").json()["data"]
        self.assertTrue(cf["is_reconciled"])
        self.assertEqual(cf["closing_cash"]["kobo"], 780000)
        self.assertEqual(cf["by_activity"]["financing"]["kobo"], 1000000)

    def test_journal_list_detail_and_post_action(self):
        entity, periods = self._seed()
        ec = entity.code

        # A fresh DRAFT journal posted through the API.
        draft = self.make_entry(entity, periods[0], [("1100", 5000, 0), ("4100", 0, 5000)])
        self.assertEqual(draft.status, DocumentStatus.DRAFT)
        resp = self.client.post(f"/v1/finance/journals/{draft.id}/post/?entity={ec}")
        self.assertEqual(resp.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.status, DocumentStatus.POSTED)

        # Detail view returns the lines and balanced totals.
        resp = self.client.get(f"/v1/finance/journals/{draft.id}/?entity={ec}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["total_debit"], data["total_credit"])
        self.assertEqual(len(data["lines"]), 2)

        # List is scoped to the entity.
        resp = self.client.get(f"/v1/finance/journals/?entity={ec}&status=POSTED")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.json()["pagination"]["totalItems"], 5)

    def test_unbalanced_post_returns_typed_error_envelope(self):
        entity, periods = self._seed()
        ec = entity.code
        bad = JournalEntry.objects.create(
            entity=entity, date=datetime.date(2026, 1, 15), period=periods[0],
        )
        JournalLine.objects.create(entry=bad, account=Account.objects.get(entity=entity, code="1100"),
                                    debit=5000, credit=0, line_no=1)
        JournalLine.objects.create(entry=bad, account=Account.objects.get(entity=entity, code="4100"),
                                    debit=0, credit=4000, line_no=2)
        resp = self.client.post(f"/v1/finance/journals/{bad.id}/post/?entity={ec}")
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"]["code"], "JOURNAL_UNBALANCED")

    def test_period_close_action_runs_checklist(self):
        entity, periods = self._seed()
        ec = entity.code
        resp = self.client.post(
            f"/v1/finance/periods/{periods[0].id}/close/?entity={ec}",
            data={"soft": False}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["period"]["status"], PeriodStatus.CLOSED)
        self.assertTrue(data["checklist"]["passed"])

    def test_trial_balance_exports_in_each_format(self):
        entity, _ = self._seed()
        ec = entity.code
        cases = {
            "csv": "text/csv",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pdf": "application/pdf",
        }
        for fmt, ctype in cases.items():
            resp = self.client.get(f"/v1/finance/reports/trial-balance/?entity={ec}&export={fmt}")
            self.assertEqual(resp.status_code, 200, fmt)
            self.assertEqual(resp["Content-Type"], ctype)
            self.assertIn(f"trial_balance_{ec}.{fmt}", resp["Content-Disposition"])
            self.assertTrue(resp.content)
        # CSV body actually contains the data.
        csv_resp = self.client.get(f"/v1/finance/reports/trial-balance/?entity={ec}&export=csv")
        text = csv_resp.content.decode("utf-8")
        self.assertIn("Trial Balance", text)
        self.assertIn("TOTAL", text)

    def test_statement_exports_available(self):
        entity, _ = self._seed()
        ec = entity.code
        for path in ("income-statement", "balance-sheet", "ar-aging"):
            resp = self.client.get(f"/v1/finance/reports/{path}/?entity={ec}&export=xlsx")
            self.assertEqual(resp.status_code, 200, path)
            self.assertTrue(resp.content)

    def test_unknown_export_format_is_rejected(self):
        entity, _ = self._seed()
        resp = self.client.get(
            f"/v1/finance/reports/trial-balance/?entity={entity.code}&export=docx"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["success"])
