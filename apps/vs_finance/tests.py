"""Phase-0 foundation + Phase-1 GL tests: money, guards, entities, numbering, and
the double-entry ledger (chart of accounts, posting, reversal, trial balance)."""
from __future__ import annotations  # Import project symbols exercised by these tests.

import datetime  # Import dependency used by this test module.
from decimal import Decimal  # Import project symbols exercised by these tests.

from django.db.models import Sum  # Import project symbols exercised by these tests.
from django.test import TestCase  # Import project symbols exercised by these tests.

from vs_finance.constants import (  # Import project symbols exercised by these tests.
    AccountType,  # Continue structured test data.
    DocType,  # Continue structured test data.
    DocumentStatus,  # Continue structured test data.
    NormalBalance,  # Continue structured test data.
    PeriodStatus,  # Continue structured test data.
    PLATFORM_ENTITY_CODE,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.exceptions import (  # Import project symbols exercised by these tests.
    InactiveAccountError,  # Continue structured test data.
    PeriodClosedError,  # Continue structured test data.
    PostingError,  # Continue structured test data.
    UnbalancedJournalError,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.constants import (  # Import project symbols exercised by these tests.
    CreditNoteKind,  # Continue structured test data.
    FinanceAuditAction,  # Continue structured test data.
    FinanceAuditStatus,  # Continue structured test data.
    InvoicePaymentStatus,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.constants import (  # Import project symbols exercised by these tests.
    AssetStatus,  # Continue structured test data.
    BankLineStatus,  # Continue structured test data.
    BudgetStatus,  # Continue structured test data.
    PayrollRunStatus,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.exceptions import (  # Import project symbols exercised by these tests.
    BankReconciliationError,  # Continue structured test data.
    BudgetError,  # Continue structured test data.
    DepreciationError,  # Continue structured test data.
    ExpenseClaimError,  # Continue structured test data.
    PayrollError,  # Continue structured test data.
    PeriodCloseError,  # Continue structured test data.
    PettyCashError,  # Continue structured test data.
    PettyCashOverdrawError,  # Continue structured test data.
    TaxFilingError,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.models import (  # Import project symbols exercised by these tests.
    Account,  # Continue structured test data.
    AccountBalance,  # Continue structured test data.
    BankAccount,  # Continue structured test data.
    BankStatementLine,  # Continue structured test data.
    Budget,  # Continue structured test data.
    Concession,  # Continue structured test data.
    CreditNote,  # Continue structured test data.
    CreditNoteLine,  # Continue structured test data.
    Customer,  # Continue structured test data.
    DepreciationSchedule,  # Continue structured test data.
    DunningNotice,  # Continue structured test data.
    DunningPolicy,  # Continue structured test data.
    DunningStage,  # Continue structured test data.
    ExpenseClaim,  # Continue structured test data.
    ExpenseClaimLine,  # Continue structured test data.
    FinanceAuditLog,  # Continue structured test data.
    FiscalPeriod,  # Continue structured test data.
    FiscalYear,  # Continue structured test data.
    FixedAsset,  # Continue structured test data.
    Invoice,  # Continue structured test data.
    InvoiceLine,  # Continue structured test data.
    JournalEntry,  # Continue structured test data.
    JournalLine,  # Continue structured test data.
    LedgerEntity,  # Continue structured test data.
    Payment,  # Continue structured test data.
    PaymentPlan,  # Continue structured test data.
    PaymentPlanInstallment,  # Continue structured test data.
    PayrollLine,  # Continue structured test data.
    PayrollRun,  # Continue structured test data.
    PettyCashFund,  # Continue structured test data.
    PettyCashVoucher,  # Continue structured test data.
    PettyCashVoucherLine,  # Continue structured test data.
    Refund,  # Continue structured test data.
    TaxCode,  # Continue structured test data.
    TaxFiling,  # Continue structured test data.
    TaxObligation,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.money import format_naira, to_kobo, to_naira  # Import project symbols exercised by these tests.
from vs_finance.numbering import next_document_number  # Import project symbols exercised by these tests.
from vs_finance.posting import ensure_balanced, ensure_period_open, post_journal, reverse_journal  # Import project symbols exercised by these tests.
from vs_finance.receivables import allocate_payment, customer_credit_balance, post_invoice, post_payment  # Import project symbols exercised by these tests.
from vs_finance.credit_notes import (  # Import project symbols exercised by these tests.
    allocate_credit_note,  # Continue structured test data.
    post_credit_note,  # Continue structured test data.
    post_refund,  # Continue structured test data.
    write_off_invoice,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.installments import (  # Import project symbols exercised by these tests.
    activate_payment_plan,  # Continue structured test data.
    build_installments,  # Continue structured test data.
    cancel_payment_plan,  # Continue structured test data.
    post_concession,  # Continue structured test data.
    refresh_plan_progress,  # Continue structured test data.
    split_amount,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.dunning import (  # Import project symbols exercised by these tests.
    cancel_notice,  # Continue structured test data.
    ensure_default_policy,  # Continue structured test data.
    generate_dunning,  # Continue structured test data.
    mark_notice_sent,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.reports import (  # Import project symbols exercised by these tests.
    ar_aging,  # Continue structured test data.
    balance_sheet,  # Continue structured test data.
    budget_monthly_matrix,  # Continue structured test data.
    budget_vs_actual,  # Continue structured test data.
    cash_flow_statement,  # Continue structured test data.
    customer_statement,  # Continue structured test data.
    income_statement,  # Continue structured test data.
    income_statement_compare,  # Continue structured test data.
    reconcile_ar,  # Continue structured test data.
    statement_of_changes_in_equity,  # Continue structured test data.
    statutory_pack,  # Continue structured test data.
    trial_balance,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.banking import (  # Import project symbols exercised by these tests.
    auto_reconcile,  # Continue structured test data.
    import_statement_lines,  # Continue structured test data.
    match_line,  # Continue structured test data.
    post_bank_adjustment,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.expenses import (  # Import project symbols exercised by these tests.
    post_expense_claim,  # Continue structured test data.
    settle_expense_claim,  # Continue structured test data.
    void_expense_claim,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.petty_cash import (  # Import project symbols exercised by these tests.
    establish_fund,  # Continue structured test data.
    fund_status,  # Continue structured test data.
    gl_cash_on_hand,  # Continue structured test data.
    post_voucher,  # Continue structured test data.
    replenish_fund,  # Continue structured test data.
    void_voucher,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.tax_filing import (  # Import project symbols exercised by these tests.
    file_filing,  # Continue structured test data.
    outstanding_obligations,  # Continue structured test data.
    pay_filing,  # Continue structured test data.
    prepare_filing,  # Continue structured test data.
    unfile_filing,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.constants import TaxFilingStatus, TaxObligationType  # Import project symbols exercised by these tests.
from vs_finance.payroll import cancel_payroll_run, pay_payroll, post_payroll  # Import project symbols exercised by these tests.
from vs_finance.budgets import add_budget_line, approve_budget  # Import project symbols exercised by these tests.
from vs_finance.assets import (  # Import project symbols exercised by these tests.
    acquire_asset, build_depreciation_schedule, dispose_asset, post_depreciation,  # Continue structured test data.
    run_period_depreciation,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.close import (  # Import project symbols exercised by these tests.
    close_checklist,  # Continue structured test data.
    close_period,  # Continue structured test data.
    lock_period,  # Continue structured test data.
    reopen_period,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.seed import seed_chart_of_accounts, seed_currencies, seed_tax_obligations  # Import project symbols exercised by these tests.
from vs_schools.models import Branch, School  # Import project symbols exercised by these tests.


class MoneyTests(TestCase):  # Define a test fixture or test case class.
    def test_to_kobo_from_string_is_exact(self):  # Define a test helper or test method.
        self.assertEqual(to_kobo("1250.50"), 125050)  # Check the expected test outcome.

    def test_to_kobo_handles_float_boundary_without_drift(self):  # Define a test helper or test method.
        # The classic 0.1 + 0.2 trap: must land on 30 kobo, not 29 or 30.0000001.
        self.assertEqual(to_kobo(Decimal("0.1") + Decimal("0.2")), 30)  # Check the expected test outcome.
        self.assertEqual(to_kobo(0.1 + 0.2), 30)  # Check the expected test outcome.

    def test_round_trip(self):  # Define a test helper or test method.
        self.assertEqual(to_naira(125050), Decimal("1250.50"))  # Check the expected test outcome.
        self.assertEqual(to_kobo(to_naira(99)), 99)  # Check the expected test outcome.

    def test_half_up_rounding(self):  # Define a test helper or test method.
        self.assertEqual(to_kobo("0.005"), 1)  # rounds up at the half

    def test_format(self):  # Define a test helper or test method.
        self.assertEqual(format_naira(125050), "₦1,250.50")  # Check the expected test outcome.

    def test_to_naira_rejects_non_int(self):  # Define a test helper or test method.
        with self.assertRaises(TypeError):  # Enter a test context manager.
            to_naira(12.5)  # type: ignore[arg-type]


class PostingGuardTests(TestCase):  # Define a test fixture or test case class.
    class _Period:  # Define a test fixture or test case class.
        def __init__(self, status):  # Define a test helper or test method.
            self.status = status  # Assign test setup data.

        def __str__(self):  # Define a test helper or test method.
            return f"2026-01 [{self.status}]"  # Return the prepared test value.

    def test_open_period_allows_posting(self):  # Define a test helper or test method.
        ensure_period_open(self._Period(PeriodStatus.OPEN))  # no raise

    def test_closed_and_locked_block_posting(self):  # Define a test helper or test method.
        for status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):  # Iterate through test data.
            with self.assertRaises(PeriodClosedError):  # Enter a test context manager.
                ensure_period_open(self._Period(status))  # Execute the test step.

    def test_soft_closed_blocked_by_default_allowed_when_privileged(self):  # Define a test helper or test method.
        with self.assertRaises(PeriodClosedError):  # Enter a test context manager.
            ensure_period_open(self._Period(PeriodStatus.SOFT_CLOSED))  # Execute the test step.
        ensure_period_open(self._Period(PeriodStatus.SOFT_CLOSED), allow_restricted=True)  # Assign test setup data.

    def test_missing_period_fails_closed(self):  # Define a test helper or test method.
        with self.assertRaises(PeriodClosedError):  # Enter a test context manager.
            ensure_period_open(None)  # Execute the test step.

    def test_balanced_check(self):  # Define a test helper or test method.
        ensure_balanced(125050, 125050)  # no raise
        with self.assertRaises(UnbalancedJournalError):  # Enter a test context manager.
            ensure_balanced(125050, 125000)  # Execute the test step.


class LedgerEntityTests(TestCase):  # Define a test fixture or test case class.
    def test_platform_entity_seeded_with_no_school(self):  # Define a test helper or test method.
        # The 0002 data migration seeds Codex's platform books; assert its shape.
        codex = LedgerEntity.objects.platform()  # Assign test setup data.
        self.assertIsNotNone(codex)  # Check the expected test outcome.
        self.assertEqual(codex.code, PLATFORM_ENTITY_CODE)  # Check the expected test outcome.
        self.assertTrue(codex.is_platform)  # Check the expected test outcome.
        self.assertIsNone(codex.source_school_id)  # Check the expected test outcome.
        # base_currency is now a real Currency FK (still stored as the "NGN" code).
        self.assertEqual(codex.base_currency_id, "NGN")  # Check the expected test outcome.
        self.assertEqual(codex.base_currency.symbol, "₦")  # Check the expected test outcome.

    def test_one_school_can_own_multiple_entities(self):  # Define a test helper or test method.
        school = School.objects.create(name="Greenfield", slug="greenfield")  # Create test database data.
        a = LedgerEntity.objects.create(  # Create test database data.
            name="Greenfield (Platform-managed)", code="GREEN1",  # Continue structured test data.
            kind=LedgerEntity.Kind.TENANT, source_school=school,  # Continue structured test data.
        )  # Close the grouped test expression.
        b = LedgerEntity.objects.create(  # Create test database data.
            name="Greenfield (Own books)", code="GREEN2",  # Continue structured test data.
            kind=LedgerEntity.Kind.TENANT, source_school=school,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(  # Check the expected test outcome.
            set(LedgerEntity.objects.for_school(school).values_list("code", flat=True)),  # Continue structured test data.
            {"GREEN1", "GREEN2"},  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertNotEqual(a.code, b.code)  # Check the expected test outcome.


class NumberingTests(TestCase):  # Define a test fixture or test case class.
    def setUp(self):  # Define a test helper or test method.
        self.school = School.objects.create(name="Test Org", slug="test-org")  # Create test database data.
        self.branch = Branch.objects.create(school=self.school, name="HQ", _type="Main")  # Create test database data.
        self.entity = LedgerEntity.objects.create(  # Create test database data.
            name="Test Org Books", code="LEKKI",  # Continue structured test data.
            kind=LedgerEntity.Kind.TENANT, source_school=self.school,  # Continue structured test data.
        )  # Close the grouped test expression.
        # Use the platform entity seeded by migration 0002 (code CODEX).
        self.platform = LedgerEntity.objects.platform()  # Assign test setup data.

    def test_format_and_increment_with_branch(self):  # Define a test helper or test method.
        n1 = next_document_number(  # Continue structured test data.
            entity=self.entity, branch=self.branch, doc_type=DocType.INVOICE, fiscal_year=2026,  # Continue structured test data.
        )  # Close the grouped test expression.
        n2 = next_document_number(  # Continue structured test data.
            entity=self.entity, branch=self.branch, doc_type=DocType.INVOICE, fiscal_year=2026,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(n1, f"CFX-LEKKI-B{self.branch.code:02d}-INV-2026-00001")  # Check the expected test outcome.
        self.assertEqual(n2, f"CFX-LEKKI-B{self.branch.code:02d}-INV-2026-00002")  # Check the expected test outcome.

    def test_entity_level_doc_omits_branch_segment(self):  # Define a test helper or test method.
        n = next_document_number(  # Continue structured test data.
            entity=self.platform, branch=None, doc_type=DocType.PAYMENT, fiscal_year=2026,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(n, "CFX-CODEX-PAY-2026-00001")  # Check the expected test outcome.

    def test_scopes_are_independent(self):  # Define a test helper or test method.
        inv = next_document_number(  # Continue structured test data.
            entity=self.entity, branch=self.branch, doc_type=DocType.INVOICE, fiscal_year=2026,  # Continue structured test data.
        )  # Close the grouped test expression.
        po = next_document_number(  # Continue structured test data.
            entity=self.entity, branch=self.branch, doc_type=DocType.PURCHASE_ORDER, fiscal_year=2026,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertTrue(inv.endswith("INV-2026-00001"))  # Check the expected test outcome.
        self.assertTrue(po.endswith("PO-2026-00001"))  # Check the expected test outcome.

    def test_two_entities_keep_independent_series(self):  # Define a test helper or test method.
        a = next_document_number(  # Continue structured test data.
            entity=self.entity, branch=None, doc_type=DocType.JOURNAL, fiscal_year=2026,  # Continue structured test data.
        )  # Close the grouped test expression.
        b = next_document_number(  # Continue structured test data.
            entity=self.platform, branch=None, doc_type=DocType.JOURNAL, fiscal_year=2026,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(a, "CFX-LEKKI-JNL-2026-00001")  # Check the expected test outcome.
        self.assertEqual(b, "CFX-CODEX-JNL-2026-00001")  # Check the expected test outcome.


class _GLFixtureMixin:  # Define a test fixture or test case class.
    """Builds an entity with a seeded chart, a fiscal year and one open period."""

    def build_ledger(self, *, period_status=PeriodStatus.OPEN):  # Define a test helper or test method.
        seed_currencies()  # Execute the test step.
        entity = LedgerEntity.objects.create(  # Create test database data.
            name="Test Books", code="TBOOK", kind=LedgerEntity.Kind.TENANT,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_chart_of_accounts(entity)  # Execute the test step.
        year = FiscalYear.objects.create(  # Create test database data.
            entity=entity, year=2026,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        period = FiscalPeriod.objects.create(  # Create test database data.
            entity=entity, fiscal_year=year, period_no=1, name="Jan 2026",  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),  # Continue structured test data.
            status=period_status,  # Continue structured test data.
        )  # Close the grouped test expression.
        return entity, period  # Return the prepared test value.

    def make_entry(self, entity, period, pairs, *, date=datetime.date(2026, 1, 15)):  # Define a test helper or test method.
        """pairs: list of (account_code, debit_kobo, credit_kobo)."""
        entry = JournalEntry.objects.create(  # Create test database data.
            entity=entity, date=date, period=period, narration="test",  # Continue structured test data.
        )  # Close the grouped test expression.
        for i, (code, dr, cr) in enumerate(pairs, start=1):  # Iterate through test data.
            acc = Account.objects.get(entity=entity, code=code)  # Fetch test database data.
            JournalLine.objects.create(  # Create test database data.
                entry=entry, account=acc, debit=dr, credit=cr, line_no=i,  # Continue structured test data.
            )  # Close the grouped test expression.
        return entry  # Return the prepared test value.


class ChartOfAccountsTests(_GLFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_seed_creates_five_roots_and_links_parents(self):  # Define a test helper or test method.
        entity, _ = self.build_ledger()  # Assign test setup data.
        roots = Account.objects.filter(entity=entity, parent__isnull=True)  # Query test database data.
        self.assertEqual(  # Check the expected test outcome.
            set(roots.values_list("account_type", flat=True)),  # Continue structured test data.
            {AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY,  # Continue structured test data.
             AccountType.INCOME, AccountType.EXPENSE},  # Continue structured test data.
        )  # Close the grouped test expression.
        cash = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        self.assertEqual(cash.parent.code, "1000")  # Check the expected test outcome.

    def test_normal_balance_derived_and_contra_flips(self):  # Define a test helper or test method.
        entity, _ = self.build_ledger()  # Assign test setup data.
        cash = Account.objects.get(entity=entity, code="1100")        # asset
        self.assertEqual(cash.normal_balance, NormalBalance.DEBIT)  # Check the expected test outcome.
        accum_dep = Account.objects.get(entity=entity, code="1900")   # contra asset
        self.assertEqual(accum_dep.normal_balance, NormalBalance.CREDIT)  # Check the expected test outcome.
        revenue = Account.objects.get(entity=entity, code="4100")     # income
        self.assertEqual(revenue.normal_balance, NormalBalance.CREDIT)  # Check the expected test outcome.

    def test_seed_is_idempotent(self):  # Define a test helper or test method.
        entity, _ = self.build_ledger()  # Assign test setup data.
        before = Account.objects.filter(entity=entity).count()  # Query test database data.
        seed_chart_of_accounts(entity)  # Execute the test step.
        self.assertEqual(Account.objects.filter(entity=entity).count(), before)  # Check the expected test outcome.


class PostingTests(_GLFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_balanced_post_updates_balances_and_stamps_posted(self):  # Define a test helper or test method.
        entity, period = self.build_ledger()  # Assign test setup data.
        entry = self.make_entry(entity, period, [("1100", 50000, 0), ("4100", 0, 50000)])  # Assign test setup data.
        post_journal(entry)  # Execute the test step.

        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertIsNotNone(entry.posted_at)  # Check the expected test outcome.
        self.assertTrue(entry.document_number.startswith("CFX-TBOOK-JNL-"))  # Check the expected test outcome.

        cash_bal = AccountBalance.objects.get(  # Fetch test database data.
            account__code="1100", period=period,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(cash_bal.debit_total, 50000)  # Check the expected test outcome.
        rev_bal = AccountBalance.objects.get(account__code="4100", period=period)  # Fetch test database data.
        self.assertEqual(rev_bal.credit_total, 50000)  # Check the expected test outcome.

    def test_unbalanced_entry_is_rejected(self):  # Define a test helper or test method.
        entity, period = self.build_ledger()  # Assign test setup data.
        entry = self.make_entry(entity, period, [("1100", 50000, 0), ("4100", 0, 40000)])  # Assign test setup data.
        with self.assertRaises(UnbalancedJournalError):  # Enter a test context manager.
            post_journal(entry)  # Execute the test step.
        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.DRAFT)  # Check the expected test outcome.
        self.assertFalse(AccountBalance.objects.filter(period=period).exists())  # Check the expected test outcome.

    def test_closed_period_blocks_posting(self):  # Define a test helper or test method.
        entity, period = self.build_ledger(period_status=PeriodStatus.CLOSED)  # Assign test setup data.
        entry = self.make_entry(entity, period, [("1100", 10000, 0), ("4100", 0, 10000)])  # Assign test setup data.
        with self.assertRaises(PeriodClosedError):  # Enter a test context manager.
            post_journal(entry)  # Execute the test step.

    def test_inactive_account_blocks_posting(self):  # Define a test helper or test method.
        entity, period = self.build_ledger()  # Assign test setup data.
        Account.objects.filter(entity=entity, code="4100").update(is_active=False)  # Query test database data.
        entry = self.make_entry(entity, period, [("1100", 10000, 0), ("4100", 0, 10000)])  # Assign test setup data.
        with self.assertRaises(InactiveAccountError):  # Enter a test context manager.
            post_journal(entry)  # Execute the test step.

    def test_cannot_double_post(self):  # Define a test helper or test method.
        entity, period = self.build_ledger()  # Assign test setup data.
        entry = self.make_entry(entity, period, [("1100", 10000, 0), ("4100", 0, 10000)])  # Assign test setup data.
        post_journal(entry)  # Execute the test step.
        with self.assertRaises(PostingError):  # Enter a test context manager.
            post_journal(entry)  # Execute the test step.

    def test_reversal_nets_balances_to_zero(self):  # Define a test helper or test method.
        entity, period = self.build_ledger()  # Assign test setup data.
        entry = self.make_entry(entity, period, [("1100", 30000, 0), ("4100", 0, 30000)])  # Assign test setup data.
        post_journal(entry)  # Execute the test step.

        reversal = reverse_journal(entry)  # Assign test setup data.
        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.REVERSED)  # Check the expected test outcome.
        self.assertEqual(reversal.reverses_id, entry.id)  # Check the expected test outcome.
        self.assertEqual(reversal.status, DocumentStatus.POSTED)  # Check the expected test outcome.

        cash_bal = AccountBalance.objects.get(account__code="1100", period=period)  # Fetch test database data.
        self.assertEqual(cash_bal.debit_total, 30000)  # Check the expected test outcome.
        self.assertEqual(cash_bal.credit_total, 30000)  # reversal credited it back
        self.assertEqual(cash_bal.net_kobo, 0)  # Check the expected test outcome.

    def test_cannot_reverse_twice(self):  # Define a test helper or test method.
        entity, period = self.build_ledger()  # Assign test setup data.
        entry = self.make_entry(entity, period, [("1100", 30000, 0), ("4100", 0, 30000)])  # Assign test setup data.
        post_journal(entry)  # Execute the test step.
        reverse_journal(entry)  # Execute the test step.
        with self.assertRaises(PostingError):  # Enter a test context manager.
            reverse_journal(entry)  # Execute the test step.

    def test_reverse_into_open_period_when_original_closed(self):  # Define a test helper or test method.
        # Prior-period correction: the original journal's period has since closed, so
        # the reversal is booked into a still-open period given an explicit date. Also
        # guards the fix where the reversal's period follows the date rather than being
        # pinned to the original's (now-closed) period.
        entity, jan = self.build_ledger()  # Assign test setup data.
        feb = FiscalPeriod.objects.create(  # Create test database data.
            entity=entity, fiscal_year=jan.fiscal_year, period_no=2, name="Feb 2026",  # Continue structured test data.
            start_date=datetime.date(2026, 2, 1), end_date=datetime.date(2026, 2, 28),  # Continue structured test data.
            status=PeriodStatus.OPEN,  # Continue structured test data.
        )  # Close the grouped test expression.
        entry = self.make_entry(entity, jan, [("1100", 30000, 0), ("4100", 0, 30000)])  # Assign test setup data.
        post_journal(entry)  # Execute the test step.
        jan.status = PeriodStatus.CLOSED           # Jan closes after the journal posted
        jan.save(update_fields=["status"])  # Assign test setup data.

        reversal = reverse_journal(entry, date=datetime.date(2026, 2, 15))  # Assign test setup data.
        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.REVERSED)  # Check the expected test outcome.
        self.assertEqual(reversal.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertEqual(reversal.period_id, feb.id)          # booked into the open period
        self.assertEqual(reversal.date, datetime.date(2026, 2, 15))  # Check the expected test outcome.


class TrialBalanceTests(_GLFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_trial_balance_balances(self):  # Define a test helper or test method.
        entity, period = self.build_ledger()  # Assign test setup data.
        # Two transactions: cash sale, and a salary payment.
        post_journal(self.make_entry(entity, period, [("1100", 100000, 0), ("4100", 0, 100000)]))  # Execute the test step.
        post_journal(self.make_entry(entity, period, [("5200", 25000, 0), ("1100", 0, 25000)]))  # Execute the test step.

        tb = trial_balance(entity)  # Assign test setup data.
        self.assertTrue(tb.is_balanced)  # Check the expected test outcome.
        self.assertEqual(tb.difference, 0)  # Check the expected test outcome.
        self.assertEqual(tb.total_debit, tb.total_credit)  # Check the expected test outcome.
        # Cash net debit 75,000; revenue credit 100,000; salary debit 25,000.
        cash_row = next(r for r in tb.rows if r.code == "1100")  # Execute the test step.
        self.assertEqual(cash_row.debit, 75000)  # Check the expected test outcome.
        self.assertEqual(cash_row.credit, 0)  # Check the expected test outcome.

    def test_empty_ledger_trivially_balances(self):  # Define a test helper or test method.
        entity, _ = self.build_ledger()  # Assign test setup data.
        tb = trial_balance(entity)  # Assign test setup data.
        self.assertTrue(tb.is_balanced)  # Check the expected test outcome.
        self.assertEqual(tb.rows, [])  # Check the expected test outcome.

    def test_period_scope_is_cumulative_and_all_periods_is_not_double_counted(self):  # Define a test helper or test method.
        """A period-scoped TB is the running balance *through* that period; the
        all-periods TB is the cumulative all-time balance — never a sum that
        double-counts across periods."""
        entity, jan = self.build_ledger()  # Assign test setup data.
        feb = FiscalPeriod.objects.create(  # Create test database data.
            entity=entity, fiscal_year=jan.fiscal_year, period_no=2, name="Feb 2026",  # Continue structured test data.
            start_date=datetime.date(2026, 2, 1), end_date=datetime.date(2026, 2, 28),  # Continue structured test data.
            status=PeriodStatus.OPEN,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_journal(self.make_entry(entity, jan, [("1100", 100000, 0), ("4100", 0, 100000)],  # Continue structured test data.
                                     date=datetime.date(2026, 1, 15)))  # Assign test setup data.
        post_journal(self.make_entry(entity, feb, [("1100", 40000, 0), ("4100", 0, 40000)],  # Continue structured test data.
                                     date=datetime.date(2026, 2, 15)))  # Assign test setup data.

        cash = lambda tb: next(r for r in tb.rows if r.code == "1100").debit  # Execute the test step.
        self.assertEqual(cash(trial_balance(entity, period=jan)), 100000)   # through Jan
        self.assertEqual(cash(trial_balance(entity, period=feb)), 140000)   # cumulative through Feb
        self.assertEqual(cash(trial_balance(entity)), 140000)              # all-time, not 240000
        self.assertTrue(trial_balance(entity).is_balanced)  # Check the expected test outcome.


class FinanceAuditTests(_GLFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_post_writes_authoritative_audit_row(self):  # Define a test helper or test method.
        entity, period = self.build_ledger()  # Assign test setup data.
        entry = self.make_entry(entity, period, [("1100", 40000, 0), ("4100", 0, 40000)])  # Assign test setup data.
        post_journal(entry)  # Execute the test step.

        log = FinanceAuditLog.objects.get(  # Fetch test database data.
            action=FinanceAuditAction.JOURNAL_POSTED, target_id=str(entry.pk),  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(log.status, FinanceAuditStatus.SUCCESS)  # Check the expected test outcome.
        self.assertEqual(log.entity_id, entity.id)  # Check the expected test outcome.
        self.assertEqual(log.document_number, entry.document_number)  # Check the expected test outcome.
        self.assertEqual(log.metadata.get("debit"), 40000)  # Check the expected test outcome.

    def test_reversal_writes_reversed_audit_row(self):  # Define a test helper or test method.
        entity, period = self.build_ledger()  # Assign test setup data.
        entry = self.make_entry(entity, period, [("1100", 40000, 0), ("4100", 0, 40000)])  # Assign test setup data.
        post_journal(entry)  # Execute the test step.
        reversal = reverse_journal(entry)  # Assign test setup data.

        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(  # Query test database data.
                action=FinanceAuditAction.JOURNAL_REVERSED, target_id=str(entry.pk),  # Continue structured test data.
            ).exists()  # Execute the test step.
        )  # Close the grouped test expression.
        # The reversing entry's own post is audited too.
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(  # Query test database data.
                action=FinanceAuditAction.JOURNAL_POSTED, target_id=str(reversal.pk),  # Continue structured test data.
            ).exists()  # Execute the test step.
        )  # Close the grouped test expression.

    def test_rejected_post_records_failure_durably(self):  # Define a test helper or test method.
        # An unbalanced post rolls back, but the rejection audit must survive.
        entity, period = self.build_ledger()  # Assign test setup data.
        entry = self.make_entry(entity, period, [("1100", 40000, 0), ("4100", 0, 30000)])  # Assign test setup data.
        with self.assertRaises(UnbalancedJournalError):  # Enter a test context manager.
            post_journal(entry)  # Execute the test step.

        log = FinanceAuditLog.objects.get(  # Fetch test database data.
            action=FinanceAuditAction.JOURNAL_POST_REJECTED, target_id=str(entry.pk),  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(log.status, FinanceAuditStatus.FAILED)  # Check the expected test outcome.
        self.assertEqual(log.metadata.get("error_code"), "JOURNAL_UNBALANCED")  # Check the expected test outcome.
        # And nothing posted: no balances, entry still draft.
        self.assertFalse(AccountBalance.objects.filter(period=period).exists())  # Check the expected test outcome.

    def test_audit_log_is_append_only(self):  # Define a test helper or test method.
        entity, period = self.build_ledger()  # Assign test setup data.
        entry = self.make_entry(entity, period, [("1100", 1000, 0), ("4100", 0, 1000)])  # Assign test setup data.
        post_journal(entry)  # Execute the test step.
        log = FinanceAuditLog.objects.filter(target_id=str(entry.pk)).first()  # Query test database data.
        log.message = "tampered"  # Assign test setup data.
        with self.assertRaises(ValueError):  # Enter a test context manager.
            log.save()  # Execute the test step.
        with self.assertRaises(ValueError):  # Enter a test context manager.
            log.delete()  # Execute the test step.

    def test_audit_log_immutable_at_db_level(self):  # Define a test helper or test method.
        # Queryset .update()/.delete() bypass the Python model hooks, but the DB
        # triggers (Postgres) must still block them. A normal INSERT keeps working.
        from django.db import Error, transaction  # Import project symbols exercised by these tests.

        entity, period = self.build_ledger()  # Assign test setup data.
        entry = self.make_entry(entity, period, [("1100", 1000, 0), ("4100", 0, 1000)])  # Assign test setup data.
        post_journal(entry)  # writes an audit row via a normal INSERT
        qs = FinanceAuditLog.objects.filter(target_id=str(entry.pk))  # Query test database data.
        self.assertTrue(qs.exists())  # Check the expected test outcome.

        with self.assertRaises(Error):  # Enter a test context manager.
            with transaction.atomic():  # Enter a test context manager.
                qs.update(message="tampered")  # Assign test setup data.
        with self.assertRaises(Error):  # Enter a test context manager.
            with transaction.atomic():  # Enter a test context manager.
                qs.delete()  # Execute the test step.
        # The row is untouched, and inserts still succeed.
        log = qs.first()  # Assign test setup data.
        self.assertNotEqual(log.message, "tampered")  # Check the expected test outcome.
        reversal = reverse_journal(entry)  # Assign test setup data.
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(  # Query test database data.
                action=FinanceAuditAction.JOURNAL_POSTED, target_id=str(reversal.pk),  # Continue structured test data.
            ).exists()  # Execute the test step.
        )  # Close the grouped test expression.


class _ARFixtureMixin(_GLFixtureMixin):  # Define a test fixture or test case class.
    """A ledger plus a customer wired to the AR control account and a VAT tax code."""

    def build_ar(self, *, period_status=PeriodStatus.OPEN):  # Define a test helper or test method.
        entity, period = self.build_ledger(period_status=period_status)  # Assign test setup data.
        ar_control = Account.objects.get(entity=entity, code="1200")   # Accounts Receivable
        vat_output = Account.objects.get(entity=entity, code="2200")   # Output VAT
        customer = Customer.objects.create(  # Create test database data.
            entity=entity, code="CUST1", name="Acme Ltd",  # Continue structured test data.
            receivable_account=ar_control,  # Continue structured test data.
        )  # Close the grouped test expression.
        vat = TaxCode.objects.create(  # Create test database data.
            entity=entity, code="VAT", name="VAT 7.5%", rate_bps=750,  # Continue structured test data.
            collected_account=vat_output,  # Continue structured test data.
        )  # Close the grouped test expression.
        return entity, period, customer, vat  # Return the prepared test value.

    def make_invoice(self, entity, customer, *, lines, date=datetime.date(2026, 1, 10),  # Define a test helper or test method.
                     due=datetime.date(2026, 1, 25)):  # Start the nested test block.
        """lines: list of (revenue_code, quantity, unit_price_kobo, tax_code_or_None)."""
        inv = Invoice.objects.create(  # Create test database data.
            entity=entity, customer=customer, invoice_date=date, due_date=due,  # Continue structured test data.
        )  # Close the grouped test expression.
        for i, (code, qty, price, tax) in enumerate(lines, start=1):  # Iterate through test data.
            InvoiceLine.objects.create(  # Create test database data.
                invoice=inv, revenue_account=Account.objects.get(entity=entity, code=code),  # Fetch test database data.
                quantity=qty, unit_price=price, tax_code=tax, line_no=i,  # Continue structured test data.
            )  # Close the grouped test expression.
        return inv  # Return the prepared test value.


class InvoicePostingTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_invoice_posts_balanced_ar_journal_with_tax(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(  # Continue structured test data.
            entity, customer, lines=[("4100", 1, 100000, vat)],  # ₦1,000 + 7.5% VAT
        )  # Close the grouped test expression.
        post_invoice(inv)  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.

        self.assertEqual(inv.status, "POSTED")  # Check the expected test outcome.
        self.assertEqual(inv.subtotal, 100000)  # Check the expected test outcome.
        self.assertEqual(inv.tax_total, 7500)  # Check the expected test outcome.
        self.assertEqual(inv.total, 107500)  # Check the expected test outcome.
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.UNPAID)  # Check the expected test outcome.
        self.assertTrue(inv.document_number.startswith("CFX-TBOOK-INV-"))  # Check the expected test outcome.

        # Journal: Dr AR 107,500 ; Cr Revenue 100,000 ; Cr VAT 7,500.
        debit, credit = inv.journal.totals()  # Assign test setup data.
        self.assertEqual(debit, credit)  # Check the expected test outcome.
        self.assertEqual(debit, 107500)  # Check the expected test outcome.
        ar_bal = AccountBalance.objects.get(account__code="1200", period=period)  # Fetch test database data.
        self.assertEqual(ar_bal.debit_total, 107500)  # Check the expected test outcome.

    def test_invoice_in_closed_period_is_rejected(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar(period_status=PeriodStatus.CLOSED)  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        with self.assertRaises(PeriodClosedError):  # Enter a test context manager.
            post_invoice(inv)  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.status, "DRAFT")  # Check the expected test outcome.
        # Rejection durably audited.
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(  # Query test database data.
                action=FinanceAuditAction.INVOICE_POSTED,  # Continue structured test data.
                status=FinanceAuditStatus.FAILED, target_id=str(inv.pk),  # Continue structured test data.
            ).exists()  # Execute the test step.
        )  # Close the grouped test expression.


class PaymentAllocationTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_partial_then_full_payment_moves_status_and_aging(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # total ₦1,000 = 100,000 kobo

        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay1 = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 12),  # Continue structured test data.
            amount=40000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay1)  # auto-allocates oldest-first
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.amount_paid, 40000)  # Check the expected test outcome.
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PARTIAL)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 60000)  # Check the expected test outcome.

        pay2 = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 20),  # Continue structured test data.
            amount=60000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay2)  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PAID)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 0)  # Check the expected test outcome.

        # Bank debited twice; AR credited twice → AR control nets to zero here.
        bank_bal = AccountBalance.objects.get(account__code="1100", period=period)  # Fetch test database data.
        self.assertEqual(bank_bal.debit_total, 100000)  # Check the expected test outcome.

    def test_overpayment_leaves_unallocated_credit(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])  # Assign test setup data.
        post_invoice(inv)  # 50,000

        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),  # Continue structured test data.
            amount=70000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # Execute the test step.
        pay.refresh_from_db()  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PAID)  # Check the expected test outcome.
        self.assertEqual(pay.allocated_amount, 50000)  # Check the expected test outcome.
        self.assertEqual(pay.unallocated_amount, 20000)  # Check the expected test outcome.


class ARReconciliationTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_aging_buckets_and_control_reconciles(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        # One invoice due 2026-01-25, viewed as of 2026-03-01 → ~35 days overdue.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.

        report = ar_aging(entity, as_of=datetime.date(2026, 3, 1))  # Assign test setup data.
        row = report.rows[0]  # Assign test setup data.
        self.assertEqual(row.outstanding, 100000)  # Check the expected test outcome.
        self.assertEqual(row.buckets["31-60"], 100000)  # Check the expected test outcome.
        self.assertEqual(report.total_net, 100000)  # Check the expected test outcome.

        # Sub-ledger (customer balances) must equal the AR control GL balance.
        rec = reconcile_ar(entity, as_of=datetime.date(2026, 3, 1))  # Assign test setup data.
        self.assertTrue(rec.is_reconciled)  # Check the expected test outcome.
        self.assertEqual(rec.control_total, 100000)  # Check the expected test outcome.

    def test_reconciles_after_partial_payment(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 18),  # Continue structured test data.
            amount=30000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # Execute the test step.

        rec = reconcile_ar(entity)  # Assign test setup data.
        self.assertTrue(rec.is_reconciled)  # Check the expected test outcome.
        self.assertEqual(rec.subledger_total, 70000)  # Check the expected test outcome.
        self.assertEqual(rec.control_total, 70000)  # Check the expected test outcome.


class CreditNoteTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_credit_note_posts_reverses_ar_and_applies_to_invoice(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, vat)])  # Assign test setup data.
        post_invoice(inv)  # total 107,500 (Dr AR)

        note = CreditNote.objects.create(  # Create test database data.
            entity=entity, customer=customer, kind=CreditNoteKind.CREDIT,  # Continue structured test data.
            note_date=datetime.date(2026, 1, 15), invoice=inv, reason="Returned goods",  # Continue structured test data.
        )  # Close the grouped test expression.
        CreditNoteLine.objects.create(  # Create test database data.
            note=note, revenue_account=Account.objects.get(entity=entity, code="4900"),  # Fetch test database data.
            quantity=1, unit_price=40000, tax_code=vat, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_credit_note(note, auto_allocate=True)  # Assign test setup data.
        note.refresh_from_db()  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.

        # CRN total = 40,000 + 7.5% = 43,000; balanced journal that credits AR.
        self.assertEqual(note.status, "POSTED")  # Check the expected test outcome.
        self.assertEqual(note.total, 43000)  # Check the expected test outcome.
        self.assertTrue(note.document_number.startswith("CFX-TBOOK-CRN-"))  # Check the expected test outcome.
        debit, credit = note.journal.totals()  # Assign test setup data.
        self.assertEqual(debit, credit)  # Check the expected test outcome.
        self.assertEqual(credit, 43000)  # Check the expected test outcome.

        # Applied to the invoice as a non-cash reduction.
        self.assertEqual(inv.amount_credited, 43000)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 107500 - 43000)  # Check the expected test outcome.
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PARTIAL)  # Check the expected test outcome.

        # AR control nets to the reduced balance.
        rec = reconcile_ar(entity, as_of=datetime.date(2026, 2, 1))  # Assign test setup data.
        self.assertTrue(rec.is_reconciled)  # Check the expected test outcome.
        self.assertEqual(rec.control_total, 64500)  # Check the expected test outcome.

    def test_debit_note_increases_ar_and_cannot_be_allocated(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        note = CreditNote.objects.create(  # Create test database data.
            entity=entity, customer=customer, kind=CreditNoteKind.DEBIT,  # Continue structured test data.
            note_date=datetime.date(2026, 1, 20), reason="Under-billed",  # Continue structured test data.
        )  # Close the grouped test expression.
        CreditNoteLine.objects.create(  # Create test database data.
            note=note, revenue_account=Account.objects.get(entity=entity, code="4100"),  # Fetch test database data.
            quantity=1, unit_price=25000, tax_code=None, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_credit_note(note)  # Execute the test step.
        note.refresh_from_db()  # Execute the test step.
        self.assertEqual(note.total, 25000)  # Check the expected test outcome.
        self.assertTrue(note.document_number.startswith("CFX-TBOOK-DRN-"))  # Check the expected test outcome.
        # Dr AR (debit note raises the receivable).
        ar_bal = AccountBalance.objects.get(account__code="1200", period=period)  # Fetch test database data.
        self.assertEqual(ar_bal.debit_total, 25000)  # Check the expected test outcome.
        with self.assertRaises(PostingError):  # Enter a test context manager.
            allocate_credit_note(note)  # Execute the test step.

    def _post_debit_note(self, entity, customer, *, amount, date, tax=None):  # Define a test helper or test method.
        """Helper: create + post a single-line DEBIT note, return it refreshed."""
        note = CreditNote.objects.create(  # Create test database data.
            entity=entity, customer=customer, kind=CreditNoteKind.DEBIT,  # Continue structured test data.
            note_date=date, reason="Supplementary charge",  # Continue structured test data.
        )  # Close the grouped test expression.
        CreditNoteLine.objects.create(  # Create test database data.
            note=note, revenue_account=Account.objects.get(entity=entity, code="4100"),  # Fetch test database data.
            quantity=1, unit_price=amount, tax_code=tax, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_credit_note(note)  # Execute the test step.
        note.refresh_from_db()  # Execute the test step.
        return note  # Return the prepared test value.

    def test_receipt_settles_standalone_debit_note(self):  # Define a test helper or test method.
        # The reported bug: a debit note with no invoice, then a larger receipt. The
        # receipt must settle the debit note (not leave it dangling) and book only the
        # true excess as customer credit.
        entity, period, customer, _ = self.build_ar()  # Assign test setup data.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        note = self._post_debit_note(  # Continue structured test data.
            entity, customer, amount=20000, date=datetime.date(2026, 1, 10))  # Assign test setup data.

        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),  # Continue structured test data.
            amount=40000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # auto-allocates → should settle the debit note first
        note.refresh_from_db()  # Execute the test step.
        pay.refresh_from_db()  # Execute the test step.

        # Debit note fully settled by the receipt.
        self.assertEqual(note.amount_paid, 20000)  # Check the expected test outcome.
        self.assertEqual(note.balance_due, 0)  # Check the expected test outcome.
        self.assertEqual(note.settlement_status, InvoicePaymentStatus.PAID)  # Check the expected test outcome.
        # Only the true excess is unallocated credit.
        self.assertEqual(pay.allocated_amount, 20000)  # Check the expected test outcome.
        self.assertEqual(pay.unallocated_amount, 20000)  # Check the expected test outcome.
        self.assertEqual(customer_credit_balance(customer), 20000)  # Check the expected test outcome.
        # GL: DN debited AR 20k; the applied receipt credits AR 20k (nets to zero);
        # the 20k excess lands in customer credit (2140).
        ar_bal = AccountBalance.objects.get(account__code="1200", period=period)  # Fetch test database data.
        cc_bal = AccountBalance.objects.get(account__code="2140", period=period)  # Fetch test database data.
        self.assertEqual(ar_bal.debit_total, 20000)  # Check the expected test outcome.
        self.assertEqual(ar_bal.credit_total, 20000)  # Check the expected test outcome.
        self.assertEqual(cc_bal.credit_total, 20000)  # Check the expected test outcome.

    def test_explicit_receipt_allocation_to_debit_note(self):  # Define a test helper or test method.
        # An explicit allocation plan can target a debit note directly.
        entity, period, customer, _ = self.build_ar()  # Assign test setup data.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        note = self._post_debit_note(  # Continue structured test data.
            entity, customer, amount=20000, date=datetime.date(2026, 1, 10))  # Assign test setup data.

        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),  # Continue structured test data.
            amount=15000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay, auto_allocate=False, allocations=[(note, 15000)])  # Assign test setup data.
        note.refresh_from_db()  # Execute the test step.
        pay.refresh_from_db()  # Execute the test step.
        self.assertEqual(note.amount_paid, 15000)  # Check the expected test outcome.
        self.assertEqual(note.balance_due, 5000)  # Check the expected test outcome.
        self.assertEqual(note.settlement_status, InvoicePaymentStatus.PARTIAL)  # Check the expected test outcome.
        self.assertEqual(pay.allocated_amount, 15000)  # Check the expected test outcome.

    def test_stored_credit_settles_debit_note(self):  # Define a test helper or test method.
        # A receipt posted before the debit note leaves stored credit; allocating it
        # later drains the credit onto the open debit note.
        entity, period, customer, _ = self.build_ar()  # Assign test setup data.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 5),  # Continue structured test data.
            amount=20000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # no open items → 20k stored credit
        self.assertEqual(customer_credit_balance(customer), 20000)  # Check the expected test outcome.

        note = self._post_debit_note(  # Continue structured test data.
            entity, customer, amount=20000, date=datetime.date(2026, 1, 10))  # Assign test setup data.
        # A fresh debit note offsets the refundable credit until settled.
        self.assertEqual(customer_credit_balance(customer), 0)  # Check the expected test outcome.

        allocate_payment(pay)  # drain stored credit onto the debit note
        note.refresh_from_db()  # Execute the test step.
        self.assertEqual(note.balance_due, 0)  # Check the expected test outcome.
        self.assertEqual(note.settlement_status, InvoicePaymentStatus.PAID)  # Check the expected test outcome.
        cc_bal = AccountBalance.objects.get(account__code="2140", period=period)  # Fetch test database data.
        self.assertEqual(cc_bal.credit_total, 20000)  # booked on receipt
        self.assertEqual(cc_bal.debit_total, 20000)   # reclassed onto the DN → net 0

    def test_receipt_allocates_across_invoice_and_debit_note_oldest_first(self):  # Define a test helper or test method.
        # Mixed open items settle oldest-first regardless of document type.
        entity, period, customer, _ = self.build_ar()  # Assign test setup data.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        note = self._post_debit_note(  # Continue structured test data.
            entity, customer, amount=30000, date=datetime.date(2026, 1, 8))  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])  # Assign test setup data.
        inv.invoice_date = datetime.date(2026, 1, 20)  # Assign test setup data.
        inv.due_date = datetime.date(2026, 1, 20)  # Assign test setup data.
        inv.save(update_fields=["invoice_date", "due_date"])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.

        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 25),  # Continue structured test data.
            amount=40000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # oldest-first → DN (Jan 8) fully, then 10k onto the invoice
        note.refresh_from_db()  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(note.balance_due, 0)  # Check the expected test outcome.
        self.assertEqual(inv.amount_paid, 10000)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 40000)  # Check the expected test outcome.

    def test_credit_note_revenue_line_carries_cost_centre_to_gl(self):  # Define a test helper or test method.
        from .models import CostCenter  # Import project symbols exercised by these tests.

        entity, period, customer, _ = self.build_ar()  # Assign test setup data.
        pri = CostCenter.objects.create(entity=entity, code="PRI", name="Primary")  # Create test database data.
        note = CreditNote.objects.create(  # Create test database data.
            entity=entity, customer=customer, kind=CreditNoteKind.CREDIT,  # Continue structured test data.
            note_date=datetime.date(2026, 1, 15), reason="Returned goods",  # Continue structured test data.
        )  # Close the grouped test expression.
        CreditNoteLine.objects.create(  # Create test database data.
            note=note, revenue_account=Account.objects.get(entity=entity, code="4900"),  # Fetch test database data.
            quantity=1, unit_price=40000, tax_code=None, cost_center=pri, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_credit_note(note)  # Execute the test step.
        # The revenue/returns GL line (Dr 4900) carries the cost centre.
        returns_line = note.journal.lines.get(account__code="4900")  # Assign test setup data.
        self.assertEqual(returns_line.cost_center.code, "PRI")  # Check the expected test outcome.
        self.assertEqual(returns_line.debit, 40000)  # Check the expected test outcome.

    def test_overpayment_books_excess_as_customer_credit(self):  # Define a test helper or test method.
        # A receipt larger than the invoice settles AR and books the excess as a
        # customer-credit liability (2140) — AR never carries a credit balance.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),  # Continue structured test data.
            amount=150000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # auto-allocates oldest-first
        inv.refresh_from_db()  # Execute the test step.
        pay.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.balance_due, 0)  # Check the expected test outcome.
        self.assertEqual(pay.allocated_amount, 100000)  # Check the expected test outcome.
        self.assertEqual(pay.unallocated_amount, 50000)  # Check the expected test outcome.
        ar_bal = AccountBalance.objects.get(account__code="1200", period=period)  # Fetch test database data.
        cc_bal = AccountBalance.objects.get(account__code="2140", period=period)  # Fetch test database data.
        self.assertEqual(ar_bal.debit_total, 100000)   # invoice
        self.assertEqual(ar_bal.credit_total, 100000)  # applied portion of the receipt
        self.assertEqual(cc_bal.credit_total, 50000)   # excess → customer-credit liability
        self.assertEqual(customer_credit_balance(customer), 50000)  # Check the expected test outcome.

    def test_apply_stored_credit_reclasses_to_ar(self):  # Define a test helper or test method.
        # Stored customer credit applied to a later invoice moves 2140 → AR.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),  # Continue structured test data.
            amount=50000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # no invoices yet → all 50,000 → 2140
        self.assertEqual(customer_credit_balance(customer), 50000)  # Check the expected test outcome.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        allocate_payment(pay)  # apply the stored credit to the new invoice
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.balance_due, 0)  # Check the expected test outcome.
        self.assertEqual(customer_credit_balance(customer), 0)  # Check the expected test outcome.
        cc_bal = AccountBalance.objects.get(account__code="2140", period=period)  # Fetch test database data.
        self.assertEqual(cc_bal.credit_total, 50000)   # booked on receipt
        self.assertEqual(cc_bal.debit_total, 50000)    # reclassed out on apply → net 0

    def test_refund_draws_down_customer_credit(self):  # Define a test helper or test method.
        # A refund pays out a credit balance: Dr 2140 (customer credit), Cr bank.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),  # Continue structured test data.
            amount=30000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # → 30,000 customer credit
        refund = Refund.objects.create(  # Create test database data.
            entity=entity, customer=customer, refund_date=datetime.date(2026, 1, 18),  # Continue structured test data.
            amount=30000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_refund(refund)  # Execute the test step.
        refund.refresh_from_db()  # Execute the test step.
        self.assertEqual(refund.status, "POSTED")  # Check the expected test outcome.
        self.assertTrue(refund.document_number.startswith("CFX-TBOOK-RFD-"))  # Check the expected test outcome.
        debit, credit = refund.journal.totals()  # Assign test setup data.
        self.assertEqual(debit, credit)  # Check the expected test outcome.
        cc_bal = AccountBalance.objects.get(account__code="2140", period=period)  # Fetch test database data.
        bank_bal = AccountBalance.objects.get(account__code="1100", period=period)  # Fetch test database data.
        self.assertEqual(cc_bal.debit_total, 30000)    # refund draws down the liability
        self.assertEqual(bank_bal.credit_total, 30000)  # cash out
        self.assertEqual(customer_credit_balance(customer), 0)  # Check the expected test outcome.

    def test_refund_capped_at_available_credit(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        refund = Refund.objects.create(  # Create test database data.
            entity=entity, customer=customer, refund_date=datetime.date(2026, 1, 18),  # Continue structured test data.
            amount=30000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        with self.assertRaises(PostingError):  # Enter a test context manager.
            post_refund(refund)  # no credit available

    def test_write_off_clears_balance_as_bad_debt(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # 100,000 outstanding

        write_off_invoice(inv, write_off_date=datetime.date(2026, 1, 28))  # Assign test setup data.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.amount_credited, 100000)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 0)  # Check the expected test outcome.
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PAID)  # Check the expected test outcome.
        # Dr bad-debt expense (5300), Cr AR.
        exp_bal = AccountBalance.objects.get(account__code="5300", period=period)  # Fetch test database data.
        self.assertEqual(exp_bal.debit_total, 100000)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(  # Query test database data.
                action=FinanceAuditAction.INVOICE_WRITTEN_OFF, target_id=str(inv.pk),  # Continue structured test data.
            ).exists()  # Execute the test step.
        )  # Close the grouped test expression.

    def test_write_off_rejected_when_nothing_outstanding(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),  # Continue structured test data.
            amount=50000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.
        with self.assertRaises(PostingError):  # Enter a test context manager.
            write_off_invoice(inv)  # Execute the test step.


class ConcessionTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_discount_reduces_invoice_and_posts_to_allowances(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # 100,000 outstanding

        concession = Concession.objects.create(  # Create test database data.
            entity=entity, customer=customer, invoice=inv, kind="DISCOUNT",  # Continue structured test data.
            concession_date=datetime.date(2026, 1, 16), amount=20000,  # Continue structured test data.
            reason="Early-settlement discount",  # Continue structured test data.
        )  # Close the grouped test expression.
        post_concession(concession)  # Execute the test step.
        concession.refresh_from_db()  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.

        self.assertEqual(concession.status, "POSTED")  # Check the expected test outcome.
        self.assertTrue(concession.document_number.startswith("CFX-TBOOK-CNC-"))  # Check the expected test outcome.
        # Dr 4910 Discounts & Concessions, Cr AR — balanced.
        debit, credit = concession.journal.totals()  # Assign test setup data.
        self.assertEqual(debit, credit)  # Check the expected test outcome.
        self.assertEqual(debit, 20000)  # Check the expected test outcome.
        disc_bal = AccountBalance.objects.get(account__code="4910", period=period)  # Fetch test database data.
        self.assertEqual(disc_bal.debit_total, 20000)  # Check the expected test outcome.

        # Invoice reduced via the non-cash credit path.
        self.assertEqual(inv.amount_credited, 20000)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 80000)  # Check the expected test outcome.
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PARTIAL)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(  # Query test database data.
                action=FinanceAuditAction.CONCESSION_POSTED, target_id=str(concession.pk),  # Continue structured test data.
            ).exists()  # Execute the test step.
        )  # Close the grouped test expression.

    def test_concession_rejected_when_amount_exceeds_balance(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        concession = Concession.objects.create(  # Create test database data.
            entity=entity, customer=customer, invoice=inv, kind="WAIVER",  # Continue structured test data.
            concession_date=datetime.date(2026, 1, 16), amount=60000,  # Continue structured test data.
        )  # Close the grouped test expression.
        with self.assertRaises(PostingError):  # Enter a test context manager.
            post_concession(concession)  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.amount_credited, 0)  # Check the expected test outcome.


class PaymentPlanTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_split_amount_is_integer_exact(self):  # Define a test helper or test method.
        parts = split_amount(100000, 3)  # Assign test setup data.
        self.assertEqual(parts, [33333, 33333, 33334])  # Check the expected test outcome.
        self.assertEqual(sum(parts), 100000)  # Check the expected test outcome.

    def test_plan_builds_dated_installments_and_tracks_settlement(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # 100,000 outstanding

        plan = PaymentPlan.objects.create(  # Create test database data.
            entity=entity, customer=customer, invoice=inv,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 10), frequency="MONTHLY",  # Continue structured test data.
            installment_count=4, total_amount=inv.balance_due,  # Continue structured test data.
        )  # Close the grouped test expression.
        build_installments(plan)  # Execute the test step.
        self.assertTrue(plan.document_number.startswith("CFX-TBOOK-PPL-"))  # Check the expected test outcome.
        installs = list(plan.installments.order_by("seq_no"))  # Assign test setup data.
        self.assertEqual([i.amount for i in installs], [25000, 25000, 25000, 25000])  # Check the expected test outcome.
        self.assertEqual(  # Check the expected test outcome.
            [i.due_date for i in installs],  # Continue structured test data.
            [datetime.date(2026, 1, 10), datetime.date(2026, 2, 10),  # Continue structured test data.
             datetime.date(2026, 3, 10), datetime.date(2026, 4, 10)],  # Continue structured test data.
        )  # Close the grouped test expression.

        activate_payment_plan(plan)  # Execute the test step.
        plan.refresh_from_db()  # Execute the test step.
        self.assertEqual(plan.plan_status, "ACTIVE")  # Check the expected test outcome.

        # A ₦500 part-payment settles the first two installments oldest-first.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 12),  # Continue structured test data.
            amount=50000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # Execute the test step.
        refresh_plan_progress(plan)  # Execute the test step.
        plan.refresh_from_db()  # Execute the test step.
        statuses = [i.status for i in plan.installments.order_by("seq_no")]  # Assign test setup data.
        self.assertEqual(statuses, ["PAID", "PAID", "PENDING", "PENDING"])  # Check the expected test outcome.
        self.assertEqual(plan.settled_total, 50000)  # Check the expected test outcome.
        self.assertEqual(plan.plan_status, "ACTIVE")  # Check the expected test outcome.

        # Settle the rest → plan completes.
        pay2 = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 22),  # Continue structured test data.
            amount=50000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay2)  # Execute the test step.
        refresh_plan_progress(plan)  # Execute the test step.
        plan.refresh_from_db()  # Execute the test step.
        self.assertEqual(plan.plan_status, "COMPLETED")  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            all(i.status == "PAID" for i in plan.installments.all())  # Execute the test step.
        )  # Close the grouped test expression.

    def test_receipt_auto_refreshes_linked_plan(self):  # Define a test helper or test method.
        # A receipt advances the plan on its own — no manual refresh_plan_progress call.
        entity, period, customer, _ = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        plan = PaymentPlan.objects.create(  # Create test database data.
            entity=entity, customer=customer, invoice=inv,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 10), frequency="MONTHLY",  # Continue structured test data.
            installment_count=4, total_amount=inv.balance_due,  # Continue structured test data.
        )  # Close the grouped test expression.
        build_installments(plan)  # Execute the test step.
        activate_payment_plan(plan)  # Execute the test step.

        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 12),  # Continue structured test data.
            amount=50000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # deliberately NO refresh_plan_progress(plan) here
        plan.refresh_from_db()  # Execute the test step.
        statuses = [i.status for i in plan.installments.order_by("seq_no")]  # Assign test setup data.
        self.assertEqual(statuses, ["PAID", "PAID", "PENDING", "PENDING"])  # Check the expected test outcome.
        self.assertEqual(plan.settled_total, 50000)  # Check the expected test outcome.
        self.assertEqual(plan.plan_status, "ACTIVE")  # Check the expected test outcome.

    def test_pre_plan_waiver_does_not_pre_settle_installments(self):  # Define a test helper or test method.
        """A waiver applied before the plan reduces the spread total but must NOT count
        as an installment payment — the first installment stays fully PENDING."""
        entity, period, customer, _ = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 3225000, None)])  # Assign test setup data.
        post_invoice(inv)  # ₦3,225,000 outstanding

        # 10% waiver → 322,500 credited, balance 2,902,500.
        waiver = Concession.objects.create(  # Create test database data.
            entity=entity, customer=customer, invoice=inv, kind="WAIVER",  # Continue structured test data.
            concession_date=datetime.date(2026, 1, 12), amount=322500,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_concession(waiver)  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.balance_due, 2902500)  # Check the expected test outcome.

        # Spread the outstanding balance over 3 monthly installments of 967,500.
        plan = PaymentPlan.objects.create(  # Create test database data.
            entity=entity, customer=customer, invoice=inv,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 15), frequency="MONTHLY",  # Continue structured test data.
            installment_count=3, total_amount=inv.balance_due,  # Continue structured test data.
        )  # Close the grouped test expression.
        build_installments(plan)  # Execute the test step.
        self.assertEqual([i.amount for i in plan.installments.order_by("seq_no")],  # Check the expected test outcome.
                         [967500, 967500, 967500])  # Execute the test step.
        activate_payment_plan(plan)  # Execute the test step.
        plan.refresh_from_db()  # Execute the test step.

        # The waiver is the plan's baseline — nothing is pre-settled.
        self.assertEqual(plan.baseline_settled, 322500)  # Check the expected test outcome.
        installs = list(plan.installments.order_by("seq_no"))  # Assign test setup data.
        self.assertEqual([i.status for i in installs], ["PENDING", "PENDING", "PENDING"])  # Check the expected test outcome.
        self.assertEqual(installs[0].amount_settled, 0)  # Check the expected test outcome.
        self.assertEqual(installs[0].balance, 967500)  # Check the expected test outcome.

        # A real ₦967,500 payment then fully settles installment #1 only.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 16),  # Continue structured test data.
            amount=967500, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # auto-refreshes the linked plan
        plan.refresh_from_db()  # Execute the test step.
        self.assertEqual([i.status for i in plan.installments.order_by("seq_no")],  # Check the expected test outcome.
                         ["PAID", "PENDING", "PENDING"])  # Execute the test step.

        # Paying the remaining two installments completes the plan.
        pay2 = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 22),  # Continue structured test data.
            amount=1935000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay2)  # Execute the test step.
        plan.refresh_from_db()  # Execute the test step.
        self.assertEqual(plan.plan_status, "COMPLETED")  # Check the expected test outcome.
        self.assertTrue(all(i.status == "PAID" for i in plan.installments.all()))  # Check the expected test outcome.

    def test_build_rejects_mismatched_explicit_amounts(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        plan = PaymentPlan.objects.create(  # Create test database data.
            entity=entity, customer=customer,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 10), frequency="WEEKLY",  # Continue structured test data.
            installment_count=2, total_amount=100000,  # Continue structured test data.
        )  # Close the grouped test expression.
        with self.assertRaises(PostingError):  # Enter a test context manager.
            build_installments(plan, amounts=[40000, 40000])  # sums to 80,000 ≠ 100,000

    def test_activate_requires_a_built_schedule(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        plan = PaymentPlan.objects.create(  # Create test database data.
            entity=entity, customer=customer,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 10), frequency="MONTHLY",  # Continue structured test data.
            installment_count=3, total_amount=90000,  # Continue structured test data.
        )  # Close the grouped test expression.
        with self.assertRaises(PostingError):  # Enter a test context manager.
            activate_payment_plan(plan)  # Execute the test step.

    def test_cancel_marks_plan_cancelled(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        plan = PaymentPlan.objects.create(  # Create test database data.
            entity=entity, customer=customer,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 10), frequency="MONTHLY",  # Continue structured test data.
            installment_count=2, total_amount=80000,  # Continue structured test data.
        )  # Close the grouped test expression.
        build_installments(plan)  # Execute the test step.
        activate_payment_plan(plan)  # Execute the test step.
        cancel_payment_plan(plan)  # Execute the test step.
        plan.refresh_from_db()  # Execute the test step.
        self.assertEqual(plan.plan_status, "CANCELLED")  # Check the expected test outcome.


class CustomerStatementTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_statement_runs_balance_and_buckets_open_invoices(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.

        # Two invoices; one part-paid, one discounted via a concession.
        inv1 = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)],  # Continue structured test data.
                                 date=datetime.date(2026, 1, 5))  # Assign test setup data.
        post_invoice(inv1)  # +100,000
        inv2 = self.make_invoice(entity, customer, lines=[("4100", 1, 60000, None)],  # Continue structured test data.
                                 date=datetime.date(2026, 1, 18))  # Assign test setup data.
        post_invoice(inv2)  # +60,000

        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 12),  # Continue structured test data.
            amount=40000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # -40,000 against inv1

        concession = Concession.objects.create(  # Create test database data.
            entity=entity, customer=customer, invoice=inv1, kind="DISCOUNT",  # Continue structured test data.
            concession_date=datetime.date(2026, 1, 20), amount=10000,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_concession(concession)  # -10,000 against inv1

        stmt = customer_statement(customer, end_date=datetime.date(2026, 1, 31))  # Assign test setup data.

        # Opening (no start_date) is zero; running movements net to the live balance.
        self.assertEqual(stmt.opening_balance, 0)  # Check the expected test outcome.
        self.assertEqual(stmt.total_debits, 160000)   # 100,000 + 60,000
        self.assertEqual(stmt.total_credits, 50000)   # 40,000 receipt + 10,000 discount
        self.assertEqual(stmt.closing_balance, 110000)  # Check the expected test outcome.
        # Entries are ordered and carry a running balance ending at the close.
        self.assertEqual([e.doc_type for e in stmt.entries],  # Check the expected test outcome.
                         ["Invoice", "Receipt", "Invoice", "Discount"])  # Execute the test step.
        self.assertEqual(stmt.entries[-1].balance, 110000)  # Check the expected test outcome.
        # Aging sums the two still-open invoices' live balances.
        self.assertEqual(sum(stmt.aging.values()), 110000)  # Check the expected test outcome.

    def test_start_date_folds_prior_movements_into_opening_balance(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        early = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)],  # Continue structured test data.
                                  date=datetime.date(2026, 1, 3))  # Assign test setup data.
        post_invoice(early)  # Execute the test step.
        later = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)],  # Continue structured test data.
                                  date=datetime.date(2026, 1, 20))  # Assign test setup data.
        post_invoice(later)  # Execute the test step.

        stmt = customer_statement(  # Continue structured test data.
            customer, start_date=datetime.date(2026, 1, 10),  # Continue structured test data.
            end_date=datetime.date(2026, 1, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        # The 3 Jan invoice predates the window → opening balance, not an entry.
        self.assertEqual(stmt.opening_balance, 100000)  # Check the expected test outcome.
        self.assertEqual([e.document_number for e in stmt.entries],  # Check the expected test outcome.
                         [later.document_number])  # Execute the test step.
        self.assertEqual(stmt.closing_balance, 150000)  # Check the expected test outcome.


class DunningTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_ensure_default_policy_is_idempotent_with_a_ladder(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        p1 = ensure_default_policy(entity)  # Assign test setup data.
        p2 = ensure_default_policy(entity)  # Assign test setup data.
        self.assertEqual(p1.pk, p2.pk)  # Check the expected test outcome.
        self.assertTrue(p1.is_default)  # Check the expected test outcome.
        self.assertEqual(p1.stages.count(), 3)  # Check the expected test outcome.
        self.assertEqual(  # Check the expected test outcome.
            [s.min_days_overdue for s in p1.stages.order_by("level")], [1, 14, 30],  # Continue structured test data.
        )  # Close the grouped test expression.

    def test_generate_advances_one_rung_lowest_unissued_first(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        ensure_default_policy(entity)  # Execute the test step.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)],  # Continue structured test data.
                                due=datetime.date(2026, 1, 25))  # Assign test setup data.
        post_invoice(inv)  # 100,000 outstanding, due 25 Jan

        # 35 days late qualifies for all three rungs, but a run advances ONE step —
        # the lowest rung not yet issued (L1), not straight to the final notice.
        notices = generate_dunning(entity, as_of=datetime.date(2026, 3, 1))  # 35 days late
        self.assertEqual(len(notices), 1)  # Check the expected test outcome.
        notice = notices[0]  # Assign test setup data.
        self.assertEqual(notice.level, 1)            # lowest unissued qualifying rung
        self.assertEqual(notice.notice_status, "PENDING")  # Check the expected test outcome.
        self.assertEqual(notice.amount_due, 100000)  # Check the expected test outcome.
        self.assertEqual(notice.days_overdue, 35)  # Check the expected test outcome.
        self.assertTrue(notice.document_number.startswith("CFX-TBOOK-DUN-"))  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(action="DUNNING_RUN_GENERATED").exists()  # Query test database data.
        )  # Close the grouped test expression.

    def test_generate_escalates_one_rung_per_run_date(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        ensure_default_policy(entity)  # Execute the test step.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)],  # Continue structured test data.
                                due=datetime.date(2026, 1, 25))  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        # Runs on three successive dates climb one rung each (never skipping).
        for d, lvl in ((datetime.date(2026, 3, 1), 1),  # Iterate through test data.
                       (datetime.date(2026, 3, 2), 2),  # Continue structured test data.
                       (datetime.date(2026, 3, 3), 3)):  # Start the nested test block.
            self.assertEqual([n.level for n in generate_dunning(entity, as_of=d)], [lvl])  # Check the expected test outcome.
        # Nothing left to escalate after the final rung.
        self.assertEqual(generate_dunning(entity, as_of=datetime.date(2026, 3, 4)), [])  # Check the expected test outcome.
        self.assertEqual(  # Check the expected test outcome.
            sorted(DunningNotice.objects.filter(invoice=inv).values_list("level", flat=True)),  # Query test database data.
            [1, 2, 3],  # Continue structured test data.
        )  # Close the grouped test expression.

    def test_generate_is_idempotent_per_run_date(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        ensure_default_policy(entity)  # Execute the test step.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 80000, None)],  # Continue structured test data.
                                due=datetime.date(2026, 1, 25))  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.

        # Two runs on the SAME date advance only one rung total (re-runs are no-ops).
        first = generate_dunning(entity, as_of=datetime.date(2026, 2, 20))  # ~26 days
        second = generate_dunning(entity, as_of=datetime.date(2026, 2, 20))  # Assign test setup data.
        self.assertEqual(len(first), 1)  # Check the expected test outcome.
        self.assertEqual(first[0].level, 1)  # lowest unissued rung first
        self.assertEqual(len(second), 0)     # same run date → no further escalation
        self.assertEqual(DunningNotice.objects.filter(invoice=inv).count(), 1)  # Check the expected test outcome.

    def test_not_yet_due_invoice_is_skipped(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        ensure_default_policy(entity)  # Execute the test step.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)],  # Continue structured test data.
                                due=datetime.date(2026, 1, 25))  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        notices = generate_dunning(entity, as_of=datetime.date(2026, 1, 20))  # before due
        self.assertEqual(notices, [])  # Check the expected test outcome.

    def test_settled_invoice_marks_notice_resolved_and_no_new_one(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        ensure_default_policy(entity)  # Execute the test step.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)],  # Continue structured test data.
                                due=datetime.date(2026, 1, 25))  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        notice = generate_dunning(entity, as_of=datetime.date(2026, 3, 1))[0]  # Assign test setup data.

        # Customer pays in full; the next run resolves the open notice, raises nothing new.
        bank = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 28),  # Continue structured test data.
            amount=100000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # Execute the test step.
        again = generate_dunning(entity, as_of=datetime.date(2026, 3, 2))  # Assign test setup data.
        notice.refresh_from_db()  # Execute the test step.
        self.assertEqual(again, [])  # Check the expected test outcome.
        self.assertEqual(notice.notice_status, "RESOLVED")  # Check the expected test outcome.

    def test_mark_sent_then_cancel_lifecycle(self):  # Define a test helper or test method.
        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        ensure_default_policy(entity)  # Execute the test step.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 60000, None)],  # Continue structured test data.
                                due=datetime.date(2026, 1, 25))  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        notice = generate_dunning(entity, as_of=datetime.date(2026, 3, 1))[0]  # Assign test setup data.

        mark_notice_sent(notice)  # Execute the test step.
        notice.refresh_from_db()  # Execute the test step.
        self.assertEqual(notice.notice_status, "SENT")  # Check the expected test outcome.
        self.assertIsNotNone(notice.sent_at)  # Check the expected test outcome.

        cancel_notice(notice, reason="Customer disputed")  # Assign test setup data.
        notice.refresh_from_db()  # Execute the test step.
        self.assertEqual(notice.notice_status, "CANCELLED")  # Check the expected test outcome.


# =========================================================================== #
# Phase 4 — banking, expenses, payroll, budget, fixed assets, period close     #
# =========================================================================== #


class _Phase4FixtureMixin(_GLFixtureMixin):  # Define a test fixture or test case class.
    """A ledger with a full year of monthly periods and a bank account on 1100."""

    def build_books(self, *, period_status=PeriodStatus.OPEN):  # Define a test helper or test method.
        seed_currencies()  # Execute the test step.
        entity = LedgerEntity.objects.create(  # Create test database data.
            name="Test Books", code="TBOOK", kind=LedgerEntity.Kind.TENANT,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_chart_of_accounts(entity)  # Execute the test step.
        year = FiscalYear.objects.create(  # Create test database data.
            entity=entity, year=2026,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        periods = []  # Assign test setup data.
        for m in range(1, 13):  # Iterate through test data.
            start = datetime.date(2026, m, 1)  # Assign test setup data.
            end = (datetime.date(2026, m + 1, 1) if m < 12 else datetime.date(2027, 1, 1))  # Assign test setup data.
            end = end - datetime.timedelta(days=1)  # Assign test setup data.
            periods.append(FiscalPeriod.objects.create(  # Create test database data.
                entity=entity, fiscal_year=year, period_no=m,  # Continue structured test data.
                name=f"2026-{m:02d}", start_date=start, end_date=end,  # Continue structured test data.
                status=period_status,  # Continue structured test data.
            ))  # Execute the test step.
        return entity, year, periods  # Return the prepared test value.

    def make_bank(self, entity, *, gl_code="1100"):  # Define a test helper or test method.
        return BankAccount.objects.create(  # Return the prepared test value.
            entity=entity, name="GTBank Operations",  # Continue structured test data.
            gl_account=Account.objects.get(entity=entity, code=gl_code),  # Fetch test database data.
        )  # Close the grouped test expression.


class BankReconciliationTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_import_is_idempotent_on_external_id(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        rows = [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 5), "amount": 50000, "external_id": "A1"},  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 6), "amount": -2000, "external_id": "A2"},  # Continue structured test data.
        ]  # Close the grouped test expression.
        _, created, _ = import_statement_lines(bank, rows)  # Assign test setup data.
        self.assertEqual(len(created), 2)  # Check the expected test outcome.
        # Re-import the same export: nothing new.
        _, again, _ = import_statement_lines(bank, rows)  # Assign test setup data.
        self.assertEqual(again, [])  # Check the expected test outcome.
        self.assertEqual(BankStatementLine.objects.filter(bank_account=bank).count(), 2)  # Check the expected test outcome.

    def test_reimport_without_external_id_is_held_back_as_suspected(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        rows = [{"txn_date": datetime.date(2026, 1, 5), "amount": -1500,  # Continue structured test data.
                 "description": "Monthly fee"}]  # Execute the test step.
        _, created, suspected = import_statement_lines(bank, rows)  # Assign test setup data.
        self.assertEqual(len(created), 1)  # Check the expected test outcome.
        self.assertEqual(suspected, [])  # Check the expected test outcome.
        # Re-import the same content (no external_id): held back, not duplicated.
        _, created2, suspected2 = import_statement_lines(bank, rows)  # Assign test setup data.
        self.assertEqual(created2, [])  # Check the expected test outcome.
        self.assertEqual(len(suspected2), 1)  # Check the expected test outcome.
        self.assertEqual(BankStatementLine.objects.filter(bank_account=bank).count(), 1)  # Check the expected test outcome.
        # force=True imports it anyway (a genuine repeat charge).
        _, created3, _ = import_statement_lines(bank, rows, force=True)  # Assign test setup data.
        self.assertEqual(len(created3), 1)  # Check the expected test outcome.
        self.assertEqual(BankStatementLine.objects.filter(bank_account=bank).count(), 2)  # Check the expected test outcome.

    def test_identical_lines_in_one_fresh_batch_are_both_kept(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        # Two genuinely identical same-day charges in one upload → both imported.
        rows = [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 5), "amount": -1500, "description": "Fee"},  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 5), "amount": -1500, "description": "Fee"},  # Continue structured test data.
        ]  # Close the grouped test expression.
        _, created, suspected = import_statement_lines(bank, rows)  # Assign test setup data.
        self.assertEqual(len(created), 2)  # Check the expected test outcome.
        self.assertEqual(suspected, [])  # Check the expected test outcome.

    def test_auto_reconcile_leaves_ambiguous_ties_unmatched(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        # Two GL cash inflows of +50,000 on the same date — a statement line of +50,000
        # has two equally-good candidates, so auto-match must leave it for a human.
        for _ in range(2):  # Iterate through test data.
            post_journal(self.make_entry(  # Continue structured test data.
                entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],  # Continue structured test data.
                date=datetime.date(2026, 1, 15)))  # Assign test setup data.
        import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 16), "amount": 50000, "external_id": "S1"}])  # Execute the test step.
        matched = auto_reconcile(bank, tolerance_days=4)  # Assign test setup data.
        self.assertEqual(matched, [])  # Check the expected test outcome.
        self.assertEqual(  # Check the expected test outcome.
            BankStatementLine.objects.get(external_id="S1").status, BankLineStatus.UNMATCHED)  # Fetch test database data.

    def test_group_match_pairs_many_gl_lines_to_one_statement_line(self):  # Define a test helper or test method.
        from vs_finance.banking import group_match, unmatch_line, _unmatched_gl_lines  # Import project symbols exercised by these tests.
        from vs_finance.exceptions import BankReconciliationError  # Import project symbols exercised by these tests.

        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        # Two receipts of 30,000 and 20,000 land as one 50,000 bank settlement line.
        e1 = self.make_entry(entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)],  # Continue structured test data.
                             date=datetime.date(2026, 1, 15))  # Assign test setup data.
        e2 = self.make_entry(entity, periods[0], [("1100", 20000, 0), ("4100", 0, 20000)],  # Continue structured test data.
                             date=datetime.date(2026, 1, 15))  # Assign test setup data.
        post_journal(e1)  # Execute the test step.
        post_journal(e2)  # Execute the test step.
        gl1 = e1.lines.get(account__code="1100")  # Assign test setup data.
        gl2 = e2.lines.get(account__code="1100")  # Assign test setup data.
        _, lines, _ = import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 16), "amount": 50000}])  # Execute the test step.
        sline = lines[0]  # Assign test setup data.

        # Wrong total is rejected; the correct pair matches.
        with self.assertRaises(BankReconciliationError):  # Enter a test context manager.
            group_match(sline, [gl1])  # needs ≥2
        group_match(sline, [gl1, gl2])  # Execute the test step.
        sline.refresh_from_db()  # Execute the test step.
        self.assertEqual(sline.status, BankLineStatus.MATCHED)  # Check the expected test outcome.
        self.assertEqual(sline.line_matches.count(), 2)  # Check the expected test outcome.
        # Both GL lines drop out of the unmatched "book" side.
        self.assertNotIn(gl1.id, {l.id for l in _unmatched_gl_lines(bank)})  # Check the expected test outcome.
        self.assertNotIn(gl2.id, {l.id for l in _unmatched_gl_lines(bank)})  # Check the expected test outcome.

        # Unmatch drops the group links and frees the GL lines again.
        unmatch_line(sline)  # Execute the test step.
        sline.refresh_from_db()  # Execute the test step.
        self.assertEqual(sline.status, BankLineStatus.UNMATCHED)  # Check the expected test outcome.
        self.assertEqual(sline.line_matches.count(), 0)  # Check the expected test outcome.
        self.assertIn(gl1.id, {l.id for l in _unmatched_gl_lines(bank)})  # Check the expected test outcome.

    def test_split_match_pairs_one_gl_line_to_many_statement_lines(self):  # Define a test helper or test method.
        from vs_finance.banking import split_match, unmatch_line, _unmatched_gl_lines  # Import project symbols exercised by these tests.
        from vs_finance.exceptions import BankReconciliationError  # Import project symbols exercised by these tests.

        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        # One 50,000 ledger movement the bank reported as two lines (30k + 20k).
        entry = self.make_entry(entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],  # Continue structured test data.
                                date=datetime.date(2026, 1, 15))  # Assign test setup data.
        post_journal(entry)  # Execute the test step.
        gl = entry.lines.get(account__code="1100")  # Assign test setup data.
        _, lines, _ = import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 16), "amount": 30000, "external_id": "A"},  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 16), "amount": 20000, "external_id": "B"}])  # Execute the test step.
        a, b = lines  # Assign test setup data.

        with self.assertRaises(BankReconciliationError):  # Enter a test context manager.
            split_match(gl, [a])  # needs ≥2
        split_match(gl, [a, b])  # Execute the test step.
        a.refresh_from_db(); b.refresh_from_db()  # Execute the test step.
        self.assertEqual(a.status, BankLineStatus.MATCHED)  # Check the expected test outcome.
        self.assertEqual(b.status, BankLineStatus.MATCHED)  # Check the expected test outcome.
        self.assertEqual(list(_unmatched_gl_lines(bank)), [])  # the GL line is matched

        # Unmatching one split line frees just it; the GL line stays matched to the other.
        unmatch_line(a)  # Execute the test step.
        a.refresh_from_db()  # Execute the test step.
        self.assertEqual(a.status, BankLineStatus.UNMATCHED)  # Check the expected test outcome.
        self.assertEqual(list(_unmatched_gl_lines(bank)), [])  # gl still linked to B

    def test_split_match_rejects_mismatched_total(self):  # Define a test helper or test method.
        from vs_finance.banking import split_match  # Import project symbols exercised by these tests.
        from vs_finance.exceptions import BankReconciliationError  # Import project symbols exercised by these tests.

        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        entry = self.make_entry(entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)])  # Assign test setup data.
        post_journal(entry)  # Execute the test step.
        gl = entry.lines.get(account__code="1100")  # Assign test setup data.
        _, lines, _ = import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 16), "amount": 30000},  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 16), "amount": 25000}])  # sums to 55k ≠ 50k
        with self.assertRaises(BankReconciliationError):  # Enter a test context manager.
            split_match(gl, lines)  # Execute the test step.

    def test_ignore_line_excludes_it_from_unmatched(self):  # Define a test helper or test method.
        from vs_finance.banking import set_line_ignored  # Import project symbols exercised by these tests.
        from vs_finance.exceptions import BankReconciliationError  # Import project symbols exercised by these tests.
        from vs_finance.constants import BankLineStatus  # Import project symbols exercised by these tests.

        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        _, lines, _ = import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 5), "amount": 10000, "description": "Opening"}])  # Execute the test step.
        line = lines[0]  # Assign test setup data.
        set_line_ignored(line)  # Execute the test step.
        line.refresh_from_db()  # Execute the test step.
        self.assertEqual(line.status, BankLineStatus.IGNORED)  # Check the expected test outcome.
        # Ignored lines don't count as unmatched.
        self.assertEqual(  # Check the expected test outcome.
            bank.statement_lines.filter(status=BankLineStatus.UNMATCHED).count(), 0)  # Assign test setup data.
        # Revert; a matched line can't be ignored.
        set_line_ignored(line, ignored=False)  # Assign test setup data.
        line.refresh_from_db()  # Execute the test step.
        self.assertEqual(line.status, BankLineStatus.UNMATCHED)  # Check the expected test outcome.

    def test_auto_reconcile_group_sums_gl_lines_to_one_bank_line(self):  # Define a test helper or test method.
        from vs_finance.banking import auto_reconcile, _unmatched_gl_lines  # Import project symbols exercised by these tests.

        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        # Two receipts (30k + 20k) land as one 50,000 bank settlement line — no single
        # GL line equals 50,000, but their sum does.
        e1 = self.make_entry(entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)],  # Continue structured test data.
                             date=datetime.date(2026, 1, 15))  # Assign test setup data.
        e2 = self.make_entry(entity, periods[0], [("1100", 20000, 0), ("4100", 0, 20000)],  # Continue structured test data.
                             date=datetime.date(2026, 1, 15))  # Assign test setup data.
        post_journal(e1)  # Execute the test step.
        post_journal(e2)  # Execute the test step.
        _, lines, _ = import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 16), "amount": 50000, "external_id": "S1"}])  # Execute the test step.
        matched = auto_reconcile(bank, tolerance_days=4)  # Assign test setup data.
        self.assertEqual([m.external_id for m in matched], ["S1"])  # Check the expected test outcome.
        sline = BankStatementLine.objects.get(external_id="S1")  # Fetch test database data.
        self.assertEqual(sline.status, BankLineStatus.MATCHED)  # Check the expected test outcome.
        self.assertEqual(sline.line_matches.count(), 2)  # Check the expected test outcome.
        self.assertEqual(list(_unmatched_gl_lines(bank)), [])  # both GL lines consumed
        # group=False disables the second pass — nothing groups.
        _, l2, _ = import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 17), "amount": 50000, "external_id": "S2"}])  # Execute the test step.
        e3 = self.make_entry(entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)],  # Continue structured test data.
                             date=datetime.date(2026, 1, 17))  # Assign test setup data.
        e4 = self.make_entry(entity, periods[0], [("1100", 20000, 0), ("4100", 0, 20000)],  # Continue structured test data.
                             date=datetime.date(2026, 1, 17))  # Assign test setup data.
        post_journal(e3)  # Execute the test step.
        post_journal(e4)  # Execute the test step.
        self.assertEqual(auto_reconcile(bank, tolerance_days=4, group=False), [])  # Check the expected test outcome.

    def test_group_match_rejects_mismatched_total(self):  # Define a test helper or test method.
        from vs_finance.banking import group_match  # Import project symbols exercised by these tests.
        from vs_finance.exceptions import BankReconciliationError  # Import project symbols exercised by these tests.

        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        e1 = self.make_entry(entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)])  # Assign test setup data.
        e2 = self.make_entry(entity, periods[0], [("1100", 20000, 0), ("4100", 0, 20000)])  # Assign test setup data.
        post_journal(e1)  # Execute the test step.
        post_journal(e2)  # Execute the test step.
        _, lines, _ = import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 16), "amount": 60000}])  # ≠ 30k+20k
        with self.assertRaises(BankReconciliationError):  # Enter a test context manager.
            group_match(lines[0], [e1.lines.get(account__code="1100"),  # Continue structured test data.
                                   e2.lines.get(account__code="1100")])  # Assign test setup data.

    def test_auto_reconcile_matches_by_amount_and_date(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        # A cash inflow of +50,000 posted on 2026-01-15.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 15),  # Continue structured test data.
        ))  # Execute the test step.
        import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 16), "amount": 50000, "external_id": "S1"},  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 16), "amount": 99999, "external_id": "S2"},  # Continue structured test data.
        ])  # Execute the test step.
        matched = auto_reconcile(bank, tolerance_days=4)  # Assign test setup data.
        self.assertEqual(len(matched), 1)  # Check the expected test outcome.
        s1 = BankStatementLine.objects.get(external_id="S1")  # Fetch test database data.
        self.assertEqual(s1.status, BankLineStatus.MATCHED)  # Check the expected test outcome.
        self.assertIsNotNone(s1.matched_line)  # Check the expected test outcome.
        # The unmatched, amount-mismatched line is left for a human.
        s2 = BankStatementLine.objects.get(external_id="S2")  # Fetch test database data.
        self.assertEqual(s2.status, BankLineStatus.UNMATCHED)  # Check the expected test outcome.

    def test_manual_match_rejects_amount_mismatch(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        entry = self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)],  # Continue structured test data.
        )  # Close the grouped test expression.
        post_journal(entry)  # Execute the test step.
        gl_line = entry.lines.get(account__code="1100")  # Assign test setup data.
        line = import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 15), "amount": 31000},  # Continue structured test data.
        ])[1][0]  # Execute the test step.
        with self.assertRaises(BankReconciliationError):  # Enter a test context manager.
            match_line(line, gl_line)  # Execute the test step.
        # Correct amount matches cleanly.
        line.amount = 30000  # Assign test setup data.
        line.save(update_fields=["amount"])  # Assign test setup data.
        match_line(line, gl_line)  # Execute the test step.
        line.refresh_from_db()  # Execute the test step.
        self.assertEqual(line.status, BankLineStatus.MATCHED)  # Check the expected test outcome.

    def test_post_bank_adjustment_books_charge_and_matches(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        line = import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 20), "amount": -1500,  # Continue structured test data.
             "description": "Monthly fee"},  # Continue structured test data.
        ])[1][0]  # Execute the test step.
        entry = post_bank_adjustment(line)  # Assign test setup data.
        line.refresh_from_db()  # Execute the test step.
        self.assertEqual(line.status, BankLineStatus.MATCHED)  # Check the expected test outcome.
        self.assertEqual(line.adjusting_journal_id, entry.id)  # Check the expected test outcome.
        # Outflow: Dr 5500 Bank Charges, Cr 1100 cash.
        charge = entry.lines.get(account__code="5500")  # Assign test setup data.
        cash = entry.lines.get(account__code="1100")  # Assign test setup data.
        self.assertEqual(charge.debit, 1500)  # Check the expected test outcome.
        self.assertEqual(cash.credit, 1500)  # Check the expected test outcome.

    def test_adjustment_rejects_already_matched_line(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        line = import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 20), "amount": -1500},  # Continue structured test data.
        ])[1][0]  # Execute the test step.
        post_bank_adjustment(line)  # Execute the test step.
        with self.assertRaises(BankReconciliationError):  # Enter a test context manager.
            post_bank_adjustment(line)  # Execute the test step.

    def test_import_groups_lines_under_a_statement(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        statement, lines, _ = import_statement_lines(  # Continue structured test data.
            bank, [  # Continue structured test data.
                {"txn_date": datetime.date(2026, 1, 5), "amount": 50000},  # Continue structured test data.
                {"txn_date": datetime.date(2026, 1, 6), "amount": -2000},  # Continue structured test data.
            ], period_label="Jan 2026", opening_balance=10000)  # Assign test setup data.
        self.assertIsNotNone(statement)  # Check the expected test outcome.
        self.assertEqual(statement.period_label, "Jan 2026")  # Check the expected test outcome.
        self.assertEqual(statement.opening_balance, 10000)  # Check the expected test outcome.
        # Closing derived = opening + Σ amounts = 10,000 + 48,000.
        self.assertEqual(statement.closing_balance, 58000)  # Check the expected test outcome.
        self.assertEqual(statement.line_count, 2)  # Check the expected test outcome.
        self.assertTrue(all(l.statement_id == statement.id for l in lines))  # Check the expected test outcome.

    def test_auto_reconcile_records_a_reconciliation_and_closes_statement(self):  # Define a test helper or test method.
        from vs_finance.models import BankReconciliation  # Import project symbols exercised by these tests.
        from vs_finance.constants import BankStatementStatus  # Import project symbols exercised by these tests.

        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 15)))  # Assign test setup data.
        statement, _, _ = import_statement_lines(bank, [  # Continue structured test data.
            {"txn_date": datetime.date(2026, 1, 16), "amount": 50000}])  # Execute the test step.
        auto_reconcile(bank, tolerance_days=4)  # Assign test setup data.

        recon = BankReconciliation.objects.filter(bank_account=bank).first()  # Query test database data.
        self.assertIsNotNone(recon)  # Check the expected test outcome.
        self.assertEqual(recon.matched_count, 1)  # Check the expected test outcome.
        self.assertEqual(recon.book_balance, 50000)  # Check the expected test outcome.
        statement.refresh_from_db()  # Execute the test step.
        self.assertEqual(statement.status, BankStatementStatus.RECONCILED)  # Check the expected test outcome.


class ExpenseClaimTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    def _make_claim(self, entity, *, lines):  # Define a test helper or test method.
        claim = ExpenseClaim.objects.create(  # Create test database data.
            entity=entity, claimant_name="Jane Staff",  # Continue structured test data.
            claim_date=datetime.date(2026, 1, 10), title="Trip",  # Continue structured test data.
        )  # Close the grouped test expression.
        for i, (code, qty, price, tax) in enumerate(lines, start=1):  # Iterate through test data.
            ExpenseClaimLine.objects.create(  # Create test database data.
                claim=claim, expense_account=Account.objects.get(entity=entity, code=code),  # Fetch test database data.
                quantity=qty, unit_price=price, tax_code=tax, line_no=i,  # Continue structured test data.
            )  # Close the grouped test expression.
        return claim  # Return the prepared test value.

    def test_post_raises_liability_with_input_vat(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        vat = TaxCode.objects.create(  # Create test database data.
            entity=entity, code="VAT", name="VAT 7.5%", rate_bps=750,  # Continue structured test data.
            paid_account=Account.objects.get(entity=entity, code="1300"),  # input VAT
        )  # Close the grouped test expression.
        claim = self._make_claim(entity, lines=[("5500", 1, 100000, vat)])  # Assign test setup data.
        post_expense_claim(claim)  # Execute the test step.
        claim.refresh_from_db()  # Execute the test step.
        self.assertEqual(claim.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertEqual(claim.subtotal, 100000)  # Check the expected test outcome.
        self.assertEqual(claim.tax_total, 7500)  # Check the expected test outcome.
        self.assertEqual(claim.total, 107500)  # Check the expected test outcome.
        self.assertEqual(claim.payment_status, InvoicePaymentStatus.UNPAID)  # Check the expected test outcome.
        # Dr expense 100,000 + Dr input VAT 7,500 ; Cr accrued reimbursement 107,500.
        debit, credit = claim.journal.totals()  # Assign test setup data.
        self.assertEqual(debit, credit)  # Check the expected test outcome.
        self.assertEqual(debit, 107500)  # Check the expected test outcome.
        reimb = claim.journal.lines.get(account__code="2400")  # Assign test setup data.
        self.assertEqual(reimb.credit, 107500)  # Check the expected test outcome.

    def test_settle_partial_then_full(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        claim = self._make_claim(entity, lines=[("5500", 1, 100000, None)])  # Assign test setup data.
        post_expense_claim(claim)  # Execute the test step.

        settle_expense_claim(  # Continue structured test data.
            claim, bank_account=bank, pay_date=datetime.date(2026, 1, 15), amount=40000,  # Continue structured test data.
        )  # Close the grouped test expression.
        claim.refresh_from_db()  # Execute the test step.
        self.assertEqual(claim.amount_paid, 40000)  # Check the expected test outcome.
        self.assertEqual(claim.payment_status, InvoicePaymentStatus.PARTIAL)  # Check the expected test outcome.
        self.assertEqual(claim.balance_due, 60000)  # Check the expected test outcome.

        settle_expense_claim(claim, bank_account=bank, pay_date=datetime.date(2026, 1, 20))  # Assign test setup data.
        claim.refresh_from_db()  # Execute the test step.
        self.assertEqual(claim.payment_status, InvoicePaymentStatus.PAID)  # Check the expected test outcome.
        self.assertEqual(claim.balance_due, 0)  # Check the expected test outcome.

    def test_cannot_post_empty_claim(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        claim = ExpenseClaim.objects.create(  # Create test database data.
            entity=entity, claimant_name="Nobody",  # Continue structured test data.
            claim_date=datetime.date(2026, 1, 10),  # Continue structured test data.
        )  # Close the grouped test expression.
        with self.assertRaises(ExpenseClaimError):  # Enter a test context manager.
            post_expense_claim(claim)  # Execute the test step.

    def test_void_reverses_journal_and_cancels_unreimbursed_claim(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        claim = self._make_claim(entity, lines=[("5500", 1, 100000, None)])  # Assign test setup data.
        post_expense_claim(claim)  # Execute the test step.
        journal = claim.journal  # Assign test setup data.
        void_expense_claim(claim)  # Execute the test step.
        claim.refresh_from_db()  # Execute the test step.
        journal.refresh_from_db()  # Execute the test step.
        self.assertEqual(claim.status, DocumentStatus.CANCELLED)  # Check the expected test outcome.
        self.assertEqual(journal.status, DocumentStatus.REVERSED)  # Check the expected test outcome.
        # The reversal backs the liability and expense out to zero.
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(action="EXPENSE_CLAIM_VOIDED").exists())  # Query test database data.

    def test_void_refused_once_reimbursed(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        claim = self._make_claim(entity, lines=[("5500", 1, 100000, None)])  # Assign test setup data.
        post_expense_claim(claim)  # Execute the test step.
        settle_expense_claim(claim, bank_account=bank, pay_date=datetime.date(2026, 1, 15),  # Continue structured test data.
                             amount=40000)  # Assign test setup data.
        with self.assertRaises(ExpenseClaimError):  # Enter a test context manager.
            void_expense_claim(claim)  # cash already left → must reverse reimbursement first

    def test_void_refused_on_draft(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        claim = self._make_claim(entity, lines=[("5500", 1, 100000, None)])  # Assign test setup data.
        with self.assertRaises(ExpenseClaimError):  # Enter a test context manager.
            void_expense_claim(claim)  # a draft is rejected, not voided


class CostCenterPropagationTests(_Phase4FixtureMixin, _ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """Cost centres set on document lines must survive into the General Ledger.

    Regression for the gap where every sub-ledger posting aggregated lines by account
    only and dropped the cost centre. P&L lines (revenue/expense) now split by
    (account, cost centre); balance-sheet control and tax lines stay aggregated.
    """

    def test_invoice_revenue_splits_by_cost_centre_in_gl(self):  # Define a test helper or test method.
        from .models import CostCenter  # Import project symbols exercised by these tests.

        entity, period, customer, _ = self.build_ar()  # Assign test setup data.
        pri = CostCenter.objects.create(entity=entity, code="PRI", name="Primary")  # Create test database data.
        sec = CostCenter.objects.create(entity=entity, code="SEC", name="Secondary")  # Create test database data.
        inv = Invoice.objects.create(  # Create test database data.
            entity=entity, customer=customer,  # Continue structured test data.
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 25),  # Continue structured test data.
        )  # Close the grouped test expression.
        rev = Account.objects.get(entity=entity, code="4100")  # Fetch test database data.
        # Same revenue account, two cost centres → two GL lines, not one merged line.
        InvoiceLine.objects.create(invoice=inv, revenue_account=rev, quantity=1,  # Create test database data.
                                   unit_price=100000, cost_center=pri, line_no=1)  # Assign test setup data.
        InvoiceLine.objects.create(invoice=inv, revenue_account=rev, quantity=1,  # Create test database data.
                                   unit_price=50000, cost_center=sec, line_no=2)  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.

        rev_lines = inv.journal.lines.filter(account__code="4100")  # Assign test setup data.
        by_cc = {ln.cost_center.code: ln.credit for ln in rev_lines}  # Assign test setup data.
        self.assertEqual(by_cc, {"PRI": 100000, "SEC": 50000})  # Check the expected test outcome.
        # AR control line stays unallocated (balance-sheet account).
        ar_line = inv.journal.lines.get(account__code="1200")  # Assign test setup data.
        self.assertIsNone(ar_line.cost_center_id)  # Check the expected test outcome.
        debit, credit = inv.journal.totals()  # Assign test setup data.
        self.assertEqual(debit, credit)  # Check the expected test outcome.

    def test_expense_claim_expense_line_carries_cost_centre_to_gl(self):  # Define a test helper or test method.
        from .models import CostCenter  # Import project symbols exercised by these tests.

        entity, _, _ = self.build_books()  # Assign test setup data.
        pri = CostCenter.objects.create(entity=entity, code="PRI", name="Primary")  # Create test database data.
        claim = ExpenseClaim.objects.create(  # Create test database data.
            entity=entity, claimant_name="Jane Staff",  # Continue structured test data.
            claim_date=datetime.date(2026, 1, 10), title="Trip",  # Continue structured test data.
        )  # Close the grouped test expression.
        ExpenseClaimLine.objects.create(  # Create test database data.
            claim=claim, expense_account=Account.objects.get(entity=entity, code="5500"),  # Fetch test database data.
            quantity=1, unit_price=100000, cost_center=pri, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_expense_claim(claim)  # Execute the test step.
        claim.refresh_from_db()  # Execute the test step.
        exp_line = claim.journal.lines.get(account__code="5500")  # Assign test setup data.
        self.assertEqual(exp_line.cost_center.code, "PRI")  # Check the expected test outcome.
        self.assertEqual(exp_line.debit, 100000)  # Check the expected test outcome.


class PettyCashTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    def _make_fund(self, entity, *, name="Front Desk", float_amount=5000000, gl_code="1110"):  # Define a test helper or test method.
        return PettyCashFund.objects.create(  # Return the prepared test value.
            entity=entity, name=name, custodian_name="Tunde Custodian",  # Continue structured test data.
            gl_account=Account.objects.get(entity=entity, code=gl_code),  # Fetch test database data.
            float_amount=float_amount,  # Continue structured test data.
        )  # Close the grouped test expression.

    def _make_voucher(self, fund, *, lines, voucher_date=datetime.date(2026, 1, 12)):  # Define a test helper or test method.
        voucher = PettyCashVoucher.objects.create(  # Create test database data.
            entity=fund.entity, fund=fund, voucher_date=voucher_date,  # Continue structured test data.
            payee="Corner Shop",  # Continue structured test data.
        )  # Close the grouped test expression.
        for i, (code, qty, price, tax) in enumerate(lines, start=1):  # Iterate through test data.
            PettyCashVoucherLine.objects.create(  # Create test database data.
                voucher=voucher,  # Continue structured test data.
                expense_account=Account.objects.get(entity=fund.entity, code=code),  # Fetch test database data.
                quantity=qty, unit_price=price, tax_code=tax, line_no=i,  # Continue structured test data.
            )  # Close the grouped test expression.
        return voucher  # Return the prepared test value.

    def test_establish_moves_cash_from_bank_to_tin(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        fund = self._make_fund(entity, float_amount=5000000)  # Assign test setup data.
        entry = establish_fund(  # Continue structured test data.
            fund, bank_account=bank, amount=5000000, date=datetime.date(2026, 1, 1),  # Continue structured test data.
        )  # Close the grouped test expression.
        fund.refresh_from_db()  # Execute the test step.
        self.assertEqual(fund.current_balance, 5000000)  # Check the expected test outcome.
        # Dr 1110 petty cash 5,000,000 ; Cr 1100 bank 5,000,000.
        debit, credit = entry.totals()  # Assign test setup data.
        self.assertEqual(debit, credit)  # Check the expected test outcome.
        self.assertEqual(entry.lines.get(account__code="1110").debit, 5000000)  # Check the expected test outcome.
        self.assertEqual(entry.lines.get(account__code="1100").credit, 5000000)  # Check the expected test outcome.

    def test_establish_rejects_non_positive(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        fund = self._make_fund(entity)  # Assign test setup data.
        with self.assertRaises(PettyCashError):  # Enter a test context manager.
            establish_fund(fund, bank_account=bank, amount=0, date=datetime.date(2026, 1, 1))  # Assign test setup data.

    def test_voucher_posts_expense_and_lowers_balance(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        fund = self._make_fund(entity, float_amount=5000000)  # Assign test setup data.
        establish_fund(fund, bank_account=bank, amount=5000000, date=datetime.date(2026, 1, 1))  # Assign test setup data.
        vat = TaxCode.objects.create(  # Create test database data.
            entity=entity, code="VAT", name="VAT 7.5%", rate_bps=750,  # Continue structured test data.
            paid_account=Account.objects.get(entity=entity, code="1300"),  # Fetch test database data.
        )  # Close the grouped test expression.
        voucher = self._make_voucher(fund, lines=[("5500", 1, 100000, vat)])  # Assign test setup data.
        post_voucher(voucher)  # Execute the test step.
        voucher.refresh_from_db()  # Execute the test step.
        fund.refresh_from_db()  # Execute the test step.
        self.assertEqual(voucher.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertEqual(voucher.subtotal, 100000)  # Check the expected test outcome.
        self.assertEqual(voucher.tax_total, 7500)  # Check the expected test outcome.
        self.assertEqual(voucher.total, 107500)  # Check the expected test outcome.
        # Dr expense 100,000 + Dr input VAT 7,500 ; Cr petty cash 107,500.
        debit, credit = voucher.journal.totals()  # Assign test setup data.
        self.assertEqual(debit, credit)  # Check the expected test outcome.
        self.assertEqual(voucher.journal.lines.get(account__code="1110").credit, 107500)  # Check the expected test outcome.
        self.assertEqual(fund.current_balance, 5000000 - 107500)  # Check the expected test outcome.

    def test_overdraw_guard_uses_live_gl_and_resyncs_mirror(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        fund = self._make_fund(entity, float_amount=50000)  # Assign test setup data.
        establish_fund(fund, bank_account=bank, amount=50000, date=datetime.date(2026, 1, 1))  # Assign test setup data.
        # Corrupt the denormalised mirror to look flush; the GL still holds only 50,000.
        PettyCashFund.objects.filter(pk=fund.pk).update(current_balance=999999)  # Query test database data.
        over = self._make_voucher(fund, lines=[("5500", 1, 60000, None)])  # Assign test setup data.
        with self.assertRaises(PettyCashOverdrawError):  # Enter a test context manager.
            post_voucher(over)  # guard reads the live GL (50,000), not the drifted mirror
        # A within-limit voucher posts and re-syncs the mirror to the true GL balance.
        ok = self._make_voucher(fund, lines=[("5500", 1, 40000, None)])  # Assign test setup data.
        post_voucher(ok)  # Execute the test step.
        fund.refresh_from_db()  # Execute the test step.
        self.assertEqual(fund.current_balance, 10000)  # Check the expected test outcome.
        self.assertEqual(gl_cash_on_hand(fund), 10000)  # Check the expected test outcome.

    def test_void_voucher_reverses_journal_and_returns_cash(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        fund = self._make_fund(entity, float_amount=5000000)  # Assign test setup data.
        establish_fund(fund, bank_account=bank, amount=5000000, date=datetime.date(2026, 1, 1))  # Assign test setup data.
        voucher = self._make_voucher(fund, lines=[("5500", 1, 100000, None)])  # Assign test setup data.
        post_voucher(voucher)  # Execute the test step.
        journal = voucher.journal  # Assign test setup data.
        fund.refresh_from_db()  # Execute the test step.
        self.assertEqual(fund.current_balance, 4900000)  # Check the expected test outcome.
        void_voucher(voucher)  # Execute the test step.
        voucher.refresh_from_db(); fund.refresh_from_db(); journal.refresh_from_db()  # Execute the test step.
        self.assertEqual(voucher.status, DocumentStatus.CANCELLED)  # Check the expected test outcome.
        self.assertEqual(journal.status, DocumentStatus.REVERSED)  # Check the expected test outcome.
        self.assertEqual(fund.current_balance, 5000000)  # cash back in the tin
        self.assertEqual(gl_cash_on_hand(fund), 5000000)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(action="PETTY_CASH_VOUCHER_VOIDED").exists())  # Query test database data.

    def test_void_refused_on_draft_voucher(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        fund = self._make_fund(entity)  # Assign test setup data.
        draft = self._make_voucher(fund, lines=[("5500", 1, 10000, None)])  # Assign test setup data.
        with self.assertRaises(PettyCashError):  # Enter a test context manager.
            void_voucher(draft)  # Execute the test step.

    def test_voucher_overdraw_is_blocked_and_audited(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        fund = self._make_fund(entity, float_amount=50000)  # Assign test setup data.
        establish_fund(fund, bank_account=bank, amount=50000, date=datetime.date(2026, 1, 1))  # Assign test setup data.
        voucher = self._make_voucher(fund, lines=[("5500", 1, 80000, None)])  # Assign test setup data.
        with self.assertRaises(PettyCashOverdrawError):  # Enter a test context manager.
            post_voucher(voucher)  # Execute the test step.
        voucher.refresh_from_db()  # Execute the test step.
        fund.refresh_from_db()  # Execute the test step.
        self.assertEqual(voucher.status, DocumentStatus.DRAFT)  # Check the expected test outcome.
        self.assertEqual(fund.current_balance, 50000)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(  # Query test database data.
                entity=entity,  # Continue structured test data.
                action=FinanceAuditAction.PETTY_CASH_VOUCHER_REJECTED,  # Continue structured test data.
                status=FinanceAuditStatus.FAILED,  # Continue structured test data.
            ).exists()  # Execute the test step.
        )  # Close the grouped test expression.

    def test_replenish_restores_float_by_default(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        fund = self._make_fund(entity, float_amount=5000000)  # Assign test setup data.
        establish_fund(fund, bank_account=bank, amount=5000000, date=datetime.date(2026, 1, 1))  # Assign test setup data.
        voucher = self._make_voucher(fund, lines=[("5500", 1, 1200000, None)])  # Assign test setup data.
        post_voucher(voucher)  # Execute the test step.
        fund.refresh_from_db()  # Execute the test step.
        self.assertEqual(fund.current_balance, 5000000 - 1200000)  # Check the expected test outcome.

        entry = replenish_fund(fund, bank_account=bank, date=datetime.date(2026, 1, 31))  # Assign test setup data.
        fund.refresh_from_db()  # Execute the test step.
        self.assertEqual(fund.current_balance, 5000000)  # restored to float
        self.assertEqual(fund.last_replenished_at, datetime.date(2026, 1, 31))  # Check the expected test outcome.
        self.assertEqual(entry.lines.get(account__code="1110").debit, 1200000)  # Check the expected test outcome.
        self.assertEqual(entry.lines.get(account__code="1100").credit, 1200000)  # Check the expected test outcome.

    def test_replenish_with_nothing_to_top_up_is_rejected(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        fund = self._make_fund(entity, float_amount=5000000)  # Assign test setup data.
        establish_fund(fund, bank_account=bank, amount=5000000, date=datetime.date(2026, 1, 1))  # Assign test setup data.
        with self.assertRaises(PettyCashError):  # Enter a test context manager.
            replenish_fund(fund, bank_account=bank, date=datetime.date(2026, 1, 31))  # Assign test setup data.

    def test_fund_status_flags_low_balance(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        fund = self._make_fund(entity, float_amount=1000000)  # Assign test setup data.
        establish_fund(fund, bank_account=bank, amount=1000000, date=datetime.date(2026, 1, 1))  # Assign test setup data.
        # Spend down to 20% of float — below the default 25% threshold.
        voucher = self._make_voucher(fund, lines=[("5500", 1, 800000, None)])  # Assign test setup data.
        post_voucher(voucher)  # Execute the test step.
        rows = fund_status(entity)  # Assign test setup data.
        self.assertEqual(len(rows), 1)  # Check the expected test outcome.
        self.assertEqual(rows[0]["current_balance"], 200000)  # Check the expected test outcome.
        self.assertEqual(rows[0]["shortfall"], 800000)  # Check the expected test outcome.
        self.assertTrue(rows[0]["needs_replenish"])  # Check the expected test outcome.


class TaxFilingTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    def _vat_obligation(self, entity):  # Define a test helper or test method.
        # The fixture seeds a VAT obligation already; reuse it idempotently.
        ob, _ = TaxObligation.objects.update_or_create(  # Continue structured test data.
            entity=entity, code="VAT",  # Continue structured test data.
            defaults={  # Continue structured test data.
                "name": "Value Added Tax",  # Continue structured test data.
                "obligation_type": TaxObligationType.VAT,  # Continue structured test data.
                "liability_account": Account.objects.get(entity=entity, code="2200"),  # Fetch test database data.
                "recoverable_account": Account.objects.get(entity=entity, code="1300"),  # Fetch test database data.
                "authority_name": "FIRS",  # Continue structured test data.
            },  # Continue structured test data.
        )  # Close the grouped test expression.
        return ob  # Return the prepared test value.

    def _wht_obligation(self, entity):  # Define a test helper or test method.
        ob, _ = TaxObligation.objects.update_or_create(  # Continue structured test data.
            entity=entity, code="WHT",  # Continue structured test data.
            defaults={  # Continue structured test data.
                "name": "Withholding Tax",  # Continue structured test data.
                "obligation_type": TaxObligationType.WHT,  # Continue structured test data.
                "liability_account": Account.objects.get(entity=entity, code="2300"),  # Fetch test database data.
                "recoverable_account": None,  # Continue structured test data.
                "authority_name": "FIRS",  # Continue structured test data.
            },  # Continue structured test data.
        )  # Close the grouped test expression.
        return ob  # Return the prepared test value.

    def _accrue_output_vat(self, entity, period, *, net, vat, date=datetime.date(2026, 1, 10)):  # Define a test helper or test method.
        # A sale: Dr cash, Cr revenue, Cr output VAT.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period,  # Continue structured test data.
            [("1100", net + vat, 0), ("4100", 0, net), ("2200", 0, vat)],  # Continue structured test data.
            date=date,  # Continue structured test data.
        ))  # Execute the test step.

    def _accrue_input_vat(self, entity, period, *, net, vat, date=datetime.date(2026, 1, 12)):  # Define a test helper or test method.
        # A purchase: Dr expense, Dr input VAT, Cr cash.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period,  # Continue structured test data.
            [("5300", net, 0), ("1300", vat, 0), ("1100", 0, net + vat)],  # Continue structured test data.
            date=date,  # Continue structured test data.
        ))  # Execute the test step.

    def _accrue_wht(self, entity, period, *, amount, date=datetime.date(2026, 1, 12)):  # Define a test helper or test method.
        # A vendor payment withholding: Dr expense, Cr WHT payable, Cr cash.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period,  # Continue structured test data.
            [("5300", amount * 10, 0), ("2300", 0, amount), ("1100", 0, amount * 9)],  # Continue structured test data.
            date=date,  # Continue structured test data.
        ))  # Execute the test step.

    def test_prepare_defaults_due_date_from_filing_day(self):  # Define a test helper or test method.
        # filing_day defaults to 21 → day 21 of the month after period_end.
        entity, _, _ = self.build_books()  # Assign test setup data.
        ob = self._vat_obligation(entity)  # Assign test setup data.
        filing = prepare_filing(  # Continue structured test data.
            ob, period_start=datetime.date(2026, 6, 1),  # Continue structured test data.
            period_end=datetime.date(2026, 6, 30))  # Assign test setup data.
        self.assertEqual(filing.due_date, datetime.date(2026, 7, 21))  # Check the expected test outcome.

    def test_prepare_clamps_due_day_to_short_following_month(self):  # Define a test helper or test method.
        # period_end March 31 → April (30 days); filing_day 31 clamps to Apr 30.
        entity, _, _ = self.build_books()  # Assign test setup data.
        ob = self._vat_obligation(entity)  # Assign test setup data.
        ob.filing_day = 31  # Assign test setup data.
        ob.save(update_fields=["filing_day"])  # Assign test setup data.
        filing = prepare_filing(  # Continue structured test data.
            ob, period_start=datetime.date(2026, 3, 1),  # Continue structured test data.
            period_end=datetime.date(2026, 3, 31))  # Assign test setup data.
        self.assertEqual(filing.due_date, datetime.date(2026, 4, 30))  # Check the expected test outcome.

    def test_prepare_respects_explicit_due_date(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        ob = self._vat_obligation(entity)  # Assign test setup data.
        filing = prepare_filing(  # Continue structured test data.
            ob, period_start=datetime.date(2026, 6, 1),  # Continue structured test data.
            period_end=datetime.date(2026, 6, 30),  # Continue structured test data.
            due_date=datetime.date(2026, 7, 5))  # Assign test setup data.
        self.assertEqual(filing.due_date, datetime.date(2026, 7, 5))  # Check the expected test outcome.

    def test_prepare_vat_nets_input_against_output(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        ob = self._vat_obligation(entity)  # Assign test setup data.
        self._accrue_output_vat(entity, periods[0], net=1000000, vat=75000)  # Assign test setup data.
        self._accrue_input_vat(entity, periods[0], net=400000, vat=30000)  # Assign test setup data.
        filing = prepare_filing(  # Continue structured test data.
            ob, period_start=datetime.date(2026, 1, 1), period_end=datetime.date(2026, 1, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(filing.filing_status, TaxFilingStatus.DRAFT)  # Check the expected test outcome.
        self.assertEqual(filing.gross_liability, 75000)  # Check the expected test outcome.
        self.assertEqual(filing.recoverable_amount, 30000)  # Check the expected test outcome.
        self.assertEqual(filing.amount_due, 45000)  # Check the expected test outcome.

    def test_prepare_is_idempotent_for_same_period(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        ob = self._vat_obligation(entity)  # Assign test setup data.
        self._accrue_output_vat(entity, periods[0], net=1000000, vat=75000)  # Assign test setup data.
        a = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                           period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        b = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                           period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        self.assertEqual(a.pk, b.pk)  # Check the expected test outcome.
        self.assertEqual(TaxFiling.objects.filter(entity=entity, obligation=ob).count(), 1)  # Check the expected test outcome.

    def test_overlapping_period_is_rejected(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        ob = self._wht_obligation(entity)  # Assign test setup data.
        self._accrue_wht(entity, periods[0], amount=50000)  # Assign test setup data.
        prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                       period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        # A different-but-overlapping window (mid-Jan into Feb) clashes.
        with self.assertRaises(TaxFilingError):  # Enter a test context manager.
            prepare_filing(ob, period_start=datetime.date(2026, 1, 15),  # Continue structured test data.
                           period_end=datetime.date(2026, 2, 15))  # Assign test setup data.

    def test_adjacent_non_overlapping_period_is_accepted(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        ob = self._wht_obligation(entity)  # Assign test setup data.
        self._accrue_wht(entity, periods[0], amount=50000)  # Assign test setup data.
        jan = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                             period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        feb = prepare_filing(ob, period_start=datetime.date(2026, 2, 1),  # Continue structured test data.
                             period_end=datetime.date(2026, 2, 28))  # Assign test setup data.
        self.assertNotEqual(jan.pk, feb.pk)  # Check the expected test outcome.
        self.assertEqual(  # Check the expected test outcome.
            TaxFiling.objects.filter(entity=entity, obligation=ob).count(), 2)  # Query test database data.

    def test_file_nets_input_vat_then_pay_clears_liability(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        ob = self._vat_obligation(entity)  # Assign test setup data.
        self._accrue_output_vat(entity, periods[0], net=1000000, vat=75000)  # Assign test setup data.
        self._accrue_input_vat(entity, periods[0], net=400000, vat=30000)  # Assign test setup data.
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                                period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        file_filing(filing, filed_date=datetime.date(2026, 2, 5), filing_reference="VAT-202601")  # Assign test setup data.
        filing.refresh_from_db()  # Execute the test step.
        self.assertEqual(filing.filing_status, TaxFilingStatus.FILED)  # Check the expected test outcome.
        # Netting journal cleared input VAT 1300 against output 2200.
        self.assertIsNotNone(filing.filing_journal)  # Check the expected test outcome.
        self.assertEqual(filing.filing_journal.lines.get(account__code="1300").credit, 30000)  # Check the expected test outcome.
        self.assertEqual(filing.filing_journal.lines.get(account__code="2200").debit, 30000)  # Check the expected test outcome.

        pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 20))  # Assign test setup data.
        filing.refresh_from_db()  # Execute the test step.
        self.assertEqual(filing.filing_status, TaxFilingStatus.PAID)  # Check the expected test outcome.
        self.assertEqual(filing.payment_status, InvoicePaymentStatus.PAID)  # Check the expected test outcome.
        # Output VAT control account 2200 is now flat: 75,000 Cr − 30,000 net − 45,000 paid.
        vat_acc = Account.objects.get(entity=entity, code="2200")  # Fetch test database data.
        agg = JournalLine.objects.filter(  # Query test database data.
            account=vat_acc, entry__status=DocumentStatus.POSTED,  # Continue structured test data.
        ).aggregate(d=Sum("debit"), c=Sum("credit"))  # Assign test setup data.
        self.assertEqual((agg["c"] or 0) - (agg["d"] or 0), 0)  # Check the expected test outcome.

    def test_wht_filing_no_recoverable_pays_full(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        ob = self._wht_obligation(entity)  # Assign test setup data.
        self._accrue_wht(entity, periods[0], amount=50000)  # Assign test setup data.
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                                period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        self.assertEqual(filing.gross_liability, 50000)  # Check the expected test outcome.
        self.assertEqual(filing.recoverable_amount, 0)  # Check the expected test outcome.
        self.assertEqual(filing.amount_due, 50000)  # Check the expected test outcome.
        # No recoverable + no penalty → filing posts no journal.
        file_filing(filing, filed_date=datetime.date(2026, 2, 5))  # Assign test setup data.
        filing.refresh_from_db()  # Execute the test step.
        self.assertIsNone(filing.filing_journal)  # Check the expected test outcome.
        pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 10))  # Assign test setup data.
        filing.refresh_from_db()  # Execute the test step.
        self.assertEqual(filing.filing_status, TaxFilingStatus.PAID)  # Check the expected test outcome.
        # The remittance Dr 2300 / Cr bank flattens the WHT payable control account.
        for code in ("2300", "1100"):  # Iterate through test data.
            acc = Account.objects.get(entity=entity, code=code)  # Fetch test database data.
            agg = JournalLine.objects.filter(  # Query test database data.
                account=acc, entry__status=DocumentStatus.POSTED,  # Continue structured test data.
            ).aggregate(d=Sum("debit"), c=Sum("credit"))  # Assign test setup data.
            if code == "2300":  # Branch test setup or assertions.
                self.assertEqual((agg["c"] or 0) - (agg["d"] or 0), 0)  # payable cleared
        # The bank-side remittance leg credited cash by 50,000.
        remit = JournalLine.objects.get(  # Fetch test database data.
            account__code="2300", entry__status=DocumentStatus.POSTED, debit=50000,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(remit.entry.lines.get(account__code="1100").credit, 50000)  # Check the expected test outcome.

    def test_partial_remittance(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        ob = self._wht_obligation(entity)  # Assign test setup data.
        self._accrue_wht(entity, periods[0], amount=50000)  # Assign test setup data.
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                                period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        file_filing(filing, filed_date=datetime.date(2026, 2, 5))  # Assign test setup data.
        pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 10), amount=20000)  # Assign test setup data.
        filing.refresh_from_db()  # Execute the test step.
        self.assertEqual(filing.payment_status, InvoicePaymentStatus.PARTIAL)  # Check the expected test outcome.
        self.assertEqual(filing.filing_status, TaxFilingStatus.FILED)  # Check the expected test outcome.
        self.assertEqual(filing.balance_due, 30000)  # Check the expected test outcome.
        pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 25))  # Assign test setup data.
        filing.refresh_from_db()  # Execute the test step.
        self.assertEqual(filing.filing_status, TaxFilingStatus.PAID)  # Check the expected test outcome.
        self.assertEqual(filing.balance_due, 0)  # Check the expected test outcome.

    def test_file_with_penalty_books_expense_and_raises_due(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        ob = self._wht_obligation(entity)  # Assign test setup data.
        self._accrue_wht(entity, periods[0], amount=50000)  # Assign test setup data.
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                                period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        file_filing(  # Continue structured test data.
            filing, filed_date=datetime.date(2026, 3, 5),  # Continue structured test data.
            adjustment_amount=5000,  # Continue structured test data.
            adjustment_account=Account.objects.get(entity=entity, code="5300"),  # Fetch test database data.
        )  # Close the grouped test expression.
        filing.refresh_from_db()  # Execute the test step.
        self.assertEqual(filing.adjustment_amount, 5000)  # Check the expected test outcome.
        self.assertEqual(filing.amount_due, 55000)  # Check the expected test outcome.
        # Dr 5300 penalty 5,000 ; Cr 2300 payable 5,000.
        self.assertEqual(filing.filing_journal.lines.get(account__code="5300").debit, 5000)  # Check the expected test outcome.
        self.assertEqual(filing.filing_journal.lines.get(account__code="2300").credit, 5000)  # Check the expected test outcome.

    def test_pay_before_file_is_rejected_and_audited(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        ob = self._wht_obligation(entity)  # Assign test setup data.
        self._accrue_wht(entity, periods[0], amount=50000)  # Assign test setup data.
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                                period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        with self.assertRaises(TaxFilingError):  # Enter a test context manager.
            pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 10))  # Assign test setup data.
        filing.refresh_from_db()  # Execute the test step.
        self.assertEqual(filing.filing_status, TaxFilingStatus.DRAFT)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(  # Query test database data.
                entity=entity,  # Continue structured test data.
                action=FinanceAuditAction.TAX_FILING_REJECTED,  # Continue structured test data.
                status=FinanceAuditStatus.FAILED,  # Continue structured test data.
            ).exists()  # Execute the test step.
        )  # Close the grouped test expression.

    def test_unfile_reverses_netting_journal_and_reverts_to_draft(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        ob = self._vat_obligation(entity)  # Assign test setup data.
        self._accrue_output_vat(entity, periods[0], net=1000000, vat=75000)  # Assign test setup data.
        self._accrue_input_vat(entity, periods[0], net=400000, vat=30000)  # Assign test setup data.
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                                period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        file_filing(filing, filed_date=datetime.date(2026, 2, 5), filing_reference="VAT-202601")  # Assign test setup data.
        filing.refresh_from_db()  # Execute the test step.
        netting = filing.filing_journal  # Assign test setup data.
        self.assertIsNotNone(netting)  # Check the expected test outcome.

        unfile_filing(filing)  # Execute the test step.
        filing.refresh_from_db()  # Execute the test step.
        self.assertEqual(filing.filing_status, TaxFilingStatus.DRAFT)  # Check the expected test outcome.
        self.assertIsNone(filing.filing_journal)  # Check the expected test outcome.
        self.assertEqual(filing.filing_reference, "")  # Check the expected test outcome.
        self.assertIsNone(filing.filed_at)  # Check the expected test outcome.
        # The netting journal is reversed (audit-correct undo), not edited.
        netting.refresh_from_db()  # Execute the test step.
        self.assertEqual(netting.status, DocumentStatus.REVERSED)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(  # Query test database data.
                entity=entity, action=FinanceAuditAction.TAX_FILING_UNFILED,  # Continue structured test data.
            ).exists()  # Execute the test step.
        )  # Close the grouped test expression.

    def test_unfile_refused_once_any_payment_made(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        ob = self._wht_obligation(entity)  # Assign test setup data.
        self._accrue_wht(entity, periods[0], amount=50000)  # Assign test setup data.
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                                period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        file_filing(filing, filed_date=datetime.date(2026, 2, 5))  # Assign test setup data.
        pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 10), amount=20000)  # Assign test setup data.
        filing.refresh_from_db()  # Execute the test step.
        with self.assertRaises(TaxFilingError):  # Enter a test context manager.
            unfile_filing(filing)  # Execute the test step.
        filing.refresh_from_db()  # Execute the test step.
        self.assertEqual(filing.filing_status, TaxFilingStatus.FILED)  # Check the expected test outcome.

    def test_unfile_refused_on_draft(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        ob = self._wht_obligation(entity)  # Assign test setup data.
        self._accrue_wht(entity, periods[0], amount=50000)  # Assign test setup data.
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),  # Continue structured test data.
                                period_end=datetime.date(2026, 1, 31))  # Assign test setup data.
        with self.assertRaises(TaxFilingError):  # Enter a test context manager.
            unfile_filing(filing)  # Execute the test step.

    def test_outstanding_obligations_reports_net(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        ob = self._vat_obligation(entity)  # Assign test setup data.
        self._accrue_output_vat(entity, periods[0], net=1000000, vat=75000)  # Assign test setup data.
        self._accrue_input_vat(entity, periods[0], net=400000, vat=30000)  # Assign test setup data.
        rows = {r["code"]: r for r in outstanding_obligations(entity)}  # Assign test setup data.
        vat = rows["VAT"]  # Assign test setup data.
        self.assertEqual(vat["payable_balance"], 75000)  # Check the expected test outcome.
        self.assertEqual(vat["recoverable_balance"], 30000)  # Check the expected test outcome.
        self.assertEqual(vat["net_outstanding"], 45000)  # Check the expected test outcome.

    def test_seed_creates_four_nigerian_obligations(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        # seed_chart_of_accounts (run by the fixture) seeds obligations too.
        rows = TaxObligation.objects.filter(entity=entity).order_by("code")  # Query test database data.
        self.assertEqual(  # Check the expected test outcome.
            list(rows.values_list("code", flat=True)),  # Continue structured test data.
            ["PAYE", "PENSION", "VAT", "WHT"],  # Continue structured test data.
        )  # Close the grouped test expression.
        vat = rows.get(code="VAT")  # Assign test setup data.
        self.assertEqual(vat.liability_account.code, "2200")  # Check the expected test outcome.
        self.assertEqual(vat.recoverable_account.code, "1300")  # Check the expected test outcome.
        wht = rows.get(code="WHT")  # Assign test setup data.
        self.assertEqual(wht.liability_account.code, "2300")  # Check the expected test outcome.
        self.assertIsNone(wht.recoverable_account)  # Check the expected test outcome.
        # Re-running is idempotent — no duplicates.
        seed_tax_obligations(entity)  # Execute the test step.
        self.assertEqual(TaxObligation.objects.filter(entity=entity).count(), 4)  # Check the expected test outcome.


class PayrollTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    def _make_run(self, entity, *, lines):  # Define a test helper or test method.
        run = PayrollRun.objects.create(  # Create test database data.
            entity=entity, pay_date=datetime.date(2026, 1, 28), period_label="Jan 2026",  # Continue structured test data.
        )  # Close the grouped test expression.
        for i, (name, gross, paye, pension) in enumerate(lines, start=1):  # Iterate through test data.
            PayrollLine.objects.create(  # Create test database data.
                run=run, employee_name=name, gross_amount=gross,  # Continue structured test data.
                paye_amount=paye, pension_amount=pension, line_no=i,  # Continue structured test data.
            )  # Close the grouped test expression.
        return run  # Return the prepared test value.

    def test_accrual_posts_balanced_with_statutory_liabilities(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        run = self._make_run(entity, lines=[  # Continue structured test data.
            ("Ada", 300000, 30000, 15000),   # net 255,000
            ("Bola", 200000, 20000, 10000),  # net 170,000
        ])  # Execute the test step.
        post_payroll(run)  # Execute the test step.
        run.refresh_from_db()  # Execute the test step.
        self.assertEqual(run.run_status, PayrollRunStatus.POSTED)  # Check the expected test outcome.
        self.assertEqual(run.gross_total, 500000)  # Check the expected test outcome.
        self.assertEqual(run.paye_total, 50000)  # Check the expected test outcome.
        self.assertEqual(run.pension_total, 25000)  # Check the expected test outcome.
        self.assertEqual(run.net_total, 425000)  # Check the expected test outcome.
        # Dr 5200 gross ; Cr 2310 PAYE, 2320 pension, 2330 net.
        debit, credit = run.journal.totals()  # Assign test setup data.
        self.assertEqual(debit, credit)  # Check the expected test outcome.
        self.assertEqual(run.journal.lines.get(account__code="5200").debit, 500000)  # Check the expected test outcome.
        self.assertEqual(run.journal.lines.get(account__code="2330").credit, 425000)  # Check the expected test outcome.

    def test_accrual_splits_gross_salary_by_cost_centre(self):  # Define a test helper or test method.
        from .models import CostCenter  # Import project symbols exercised by these tests.

        entity, _, _ = self.build_books()  # Assign test setup data.
        pri = CostCenter.objects.create(entity=entity, code="PRI", name="Primary")  # Create test database data.
        sec = CostCenter.objects.create(entity=entity, code="SEC", name="Secondary")  # Create test database data.
        run = PayrollRun.objects.create(  # Create test database data.
            entity=entity, pay_date=datetime.date(2026, 1, 28), period_label="Jan 2026",  # Continue structured test data.
        )  # Close the grouped test expression.
        PayrollLine.objects.create(run=run, employee_name="Ada", gross_amount=300000,  # Create test database data.
                                   paye_amount=30000, pension_amount=15000, cost_center=pri, line_no=1)  # Assign test setup data.
        PayrollLine.objects.create(run=run, employee_name="Bola", gross_amount=200000,  # Create test database data.
                                   paye_amount=20000, pension_amount=10000, cost_center=sec, line_no=2)  # Assign test setup data.
        post_payroll(run)  # Execute the test step.
        run.refresh_from_db()  # Execute the test step.
        # Gross salary expense (5200) splits by cost centre; liabilities stay aggregated.
        salary_lines = run.journal.lines.filter(account__code="5200")  # Assign test setup data.
        by_cc = {ln.cost_center.code: ln.debit for ln in salary_lines}  # Assign test setup data.
        self.assertEqual(by_cc, {"PRI": 300000, "SEC": 200000})  # Check the expected test outcome.
        self.assertIsNone(run.journal.lines.get(account__code="2330").cost_center_id)  # Check the expected test outcome.
        debit, credit = run.journal.totals()  # Assign test setup data.
        self.assertEqual(debit, credit)  # Check the expected test outcome.

    def test_disburse_clears_net_payable(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        run = self._make_run(entity, lines=[("Ada", 300000, 30000, 15000)])  # Assign test setup data.
        post_payroll(run)  # Execute the test step.
        pay_payroll(run, bank_account=bank)  # Assign test setup data.
        run.refresh_from_db()  # Execute the test step.
        self.assertEqual(run.run_status, PayrollRunStatus.PAID)  # Check the expected test outcome.
        # Dr 2330 net payable ; Cr 1100 bank.
        disb = run.disbursement_journal  # Assign test setup data.
        self.assertEqual(disb.lines.get(account__code="2330").debit, 255000)  # Check the expected test outcome.
        self.assertEqual(disb.lines.get(account__code="1100").credit, 255000)  # Check the expected test outcome.

    def test_negative_net_is_rejected(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        run = self._make_run(entity, lines=[("Greedy", 100000, 80000, 30000)])  # net -10,000
        with self.assertRaises(PayrollError):  # Enter a test context manager.
            post_payroll(run)  # Execute the test step.
        run.refresh_from_db()  # Execute the test step.
        self.assertEqual(run.run_status, PayrollRunStatus.DRAFT)  # Check the expected test outcome.

    def test_cannot_pay_unposted_run(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        run = self._make_run(entity, lines=[("Ada", 300000, 30000, 15000)])  # Assign test setup data.
        with self.assertRaises(PayrollError):  # Enter a test context manager.
            pay_payroll(run, bank_account=bank)  # Assign test setup data.

    def test_cancel_draft_run_marks_cancelled(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        run = self._make_run(entity, lines=[("Ada", 300000, 30000, 15000)])  # Assign test setup data.
        cancel_payroll_run(run)  # Execute the test step.
        run.refresh_from_db()  # Execute the test step.
        self.assertEqual(run.run_status, PayrollRunStatus.CANCELLED)  # Check the expected test outcome.
        self.assertIsNone(run.journal_id)  # nothing was posted

    def test_void_posted_run_reverses_accrual(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        run = self._make_run(entity, lines=[("Ada", 300000, 30000, 15000)])  # Assign test setup data.
        post_payroll(run)  # Execute the test step.
        run.refresh_from_db()  # Execute the test step.
        journal = run.journal  # Assign test setup data.
        cancel_payroll_run(run)  # Execute the test step.
        run.refresh_from_db(); journal.refresh_from_db()  # Execute the test step.
        self.assertEqual(run.run_status, PayrollRunStatus.CANCELLED)  # Check the expected test outcome.
        self.assertEqual(journal.status, DocumentStatus.REVERSED)  # accrual backed out
        self.assertTrue(  # Check the expected test outcome.
            FinanceAuditLog.objects.filter(action="PAYROLL_CANCELLED").exists())  # Query test database data.

    def test_void_refused_once_paid(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        run = self._make_run(entity, lines=[("Ada", 300000, 30000, 15000)])  # Assign test setup data.
        post_payroll(run)  # Execute the test step.
        pay_payroll(run, bank_account=bank)  # Assign test setup data.
        with self.assertRaises(PayrollError):  # Enter a test context manager.
            cancel_payroll_run(run)  # net wages already left the bank


class BudgetTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_approve_locks_lines_against_edits(self):  # Define a test helper or test method.
        entity, year, _ = self.build_books()  # Assign test setup data.
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")  # Create test database data.
        salaries = Account.objects.get(entity=entity, code="5200")  # Fetch test database data.
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)  # Assign test setup data.
        approve_budget(budget)  # Execute the test step.
        budget.refresh_from_db()  # Execute the test step.
        self.assertEqual(budget.status, BudgetStatus.APPROVED)  # Check the expected test outcome.
        self.assertTrue(budget.is_locked)  # Check the expected test outcome.
        with self.assertRaises(BudgetError):  # Enter a test context manager.
            add_budget_line(budget, account=salaries, period_no=2, amount=10000)  # Assign test setup data.

    def test_period_no_must_be_in_range(self):  # Define a test helper or test method.
        entity, year, _ = self.build_books()  # Assign test setup data.
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")  # Create test database data.
        salaries = Account.objects.get(entity=entity, code="5200")  # Fetch test database data.
        with self.assertRaises(BudgetError):  # Enter a test context manager.
            add_budget_line(budget, account=salaries, period_no=13, amount=10000)  # Assign test setup data.

    def test_delete_draft_budget_removes_lines_and_writes_audit(self):  # Define a test helper or test method.
        from vs_finance.budgets import delete_budget  # Import project symbols exercised by these tests.
        from vs_finance.models import BudgetLine  # Import project symbols exercised by these tests.

        entity, year, _ = self.build_books()  # Assign test setup data.
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")  # Create test database data.
        salaries = Account.objects.get(entity=entity, code="5200")  # Fetch test database data.
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)  # Assign test setup data.
        bid = budget.id  # Assign test setup data.
        delete_budget(budget)  # Execute the test step.
        self.assertFalse(Budget.objects.filter(id=bid).exists())  # Check the expected test outcome.
        self.assertFalse(BudgetLine.objects.filter(budget_id=bid).exists())  # lines cascade
        self.assertTrue(FinanceAuditLog.objects.filter(  # Check the expected test outcome.
            action=FinanceAuditAction.BUDGET_DELETED, target_id=str(bid)).exists())  # Assign test setup data.

    def test_delete_approved_budget_refuses(self):  # Define a test helper or test method.
        from vs_finance.budgets import delete_budget  # Import project symbols exercised by these tests.

        entity, year, _ = self.build_books()  # Assign test setup data.
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")  # Create test database data.
        approve_budget(budget)  # Execute the test step.
        with self.assertRaises(BudgetError):  # Enter a test context manager.
            delete_budget(budget)  # Execute the test step.

    def test_budget_vs_actual_variance(self):  # Define a test helper or test method.
        entity, year, periods = self.build_books()  # Assign test setup data.
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")  # Create test database data.
        salaries = Account.objects.get(entity=entity, code="5200")  # Fetch test database data.
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)  # Assign test setup data.
        # Actual salary spend of 50,000 in Jan.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("5200", 50000, 0), ("1100", 0, 50000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 15),  # Continue structured test data.
        ))  # Execute the test step.
        report = budget_vs_actual(budget)  # Assign test setup data.
        row = next(r for r in report.rows if r.code == "5200")  # Execute the test step.
        self.assertEqual(row.budget, 60000)  # Check the expected test outcome.
        self.assertEqual(row.actual, 50000)  # Check the expected test outcome.
        self.assertEqual(row.variance, -10000)        # under budget
        self.assertEqual(report.total_budget, 60000)  # Check the expected test outcome.
        self.assertEqual(report.total_actual, 50000)  # Check the expected test outcome.

    def test_budget_vs_actual_scoped_to_period(self):  # Define a test helper or test method.
        entity, year, periods = self.build_books()  # Assign test setup data.
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")  # Create test database data.
        salaries = Account.objects.get(entity=entity, code="5200")  # Fetch test database data.
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)  # Assign test setup data.
        add_budget_line(budget, account=salaries, period_no=2, amount=60000)  # Assign test setup data.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[1], [("5200", 70000, 0), ("1100", 0, 70000)],  # Continue structured test data.
            date=datetime.date(2026, 2, 15),  # Continue structured test data.
        ))  # Execute the test step.
        feb = budget_vs_actual(budget, period_no=2)  # Assign test setup data.
        row = next(r for r in feb.rows if r.code == "5200")  # Execute the test step.
        self.assertEqual(row.budget, 60000)  # Check the expected test outcome.
        self.assertEqual(row.actual, 70000)  # Check the expected test outcome.
        self.assertEqual(row.variance, 10000)         # over budget

    def test_budget_monthly_matrix_builds_per_account_cells(self):  # Define a test helper or test method.
        entity, year, periods = self.build_books()  # Assign test setup data.
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")  # Create test database data.
        salaries = Account.objects.get(entity=entity, code="5200")  # Fetch test database data.
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)  # Assign test setup data.
        add_budget_line(budget, account=salaries, period_no=2, amount=60000)  # Assign test setup data.
        # Actual: 50,000 in Jan (period 1), 70,000 in Feb (period 2).
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("5200", 50000, 0), ("1100", 0, 50000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 15)))  # Assign test setup data.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[1], [("5200", 70000, 0), ("1100", 0, 70000)],  # Continue structured test data.
            date=datetime.date(2026, 2, 15)))  # Assign test setup data.
        matrix = budget_monthly_matrix(budget)  # Assign test setup data.
        self.assertEqual(len(matrix.periods), 12)  # Check the expected test outcome.
        row = next(r for r in matrix.rows if r.code == "5200")  # Execute the test step.
        self.assertEqual(len(row.cells), 12)  # Check the expected test outcome.
        self.assertEqual(row.budget_total, 120000)  # Check the expected test outcome.
        self.assertEqual(row.actual_total, 120000)  # Check the expected test outcome.
        c1 = next(c for c in row.cells if c["period_no"] == 1)  # Execute the test step.
        c2 = next(c for c in row.cells if c["period_no"] == 2)  # Execute the test step.
        self.assertEqual((c1["budget"], c1["actual"]), (60000, 50000))  # Check the expected test outcome.
        self.assertEqual((c2["budget"], c2["actual"]), (60000, 70000))  # Check the expected test outcome.


class FixedAssetTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    def _make_asset(self, entity, *, cost=1100000, salvage=0, life=11,  # Define a test helper or test method.
                    acq=datetime.date(2026, 1, 1)):  # Start the nested test block.
        return FixedAsset.objects.create(  # Return the prepared test value.
            entity=entity, name="Server rack", acquisition_date=acq,  # Continue structured test data.
            cost=cost, salvage_value=salvage, useful_life_months=life,  # Continue structured test data.
        )  # Close the grouped test expression.

    def test_acquire_capitalises_and_builds_schedule(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        asset = self._make_asset(entity)  # Assign test setup data.
        acquire_asset(asset, bank_account=bank)  # Assign test setup data.
        asset.refresh_from_db()  # Execute the test step.
        self.assertEqual(asset.asset_status, AssetStatus.ACTIVE)  # Check the expected test outcome.
        # Dr 1500 PP&E, Cr 1100 cash.
        self.assertEqual(asset.acquisition_journal.lines.get(account__code="1500").debit, 1100000)  # Check the expected test outcome.
        # Schedule sums to the depreciable base exactly.
        rows = list(asset.schedule.all())  # Assign test setup data.
        self.assertEqual(len(rows), 11)  # Check the expected test outcome.
        self.assertEqual(sum(r.amount for r in rows), asset.depreciable_base)  # Check the expected test outcome.

    def test_declining_balance_schedule_front_loads_and_lands_on_salvage(self):  # Define a test helper or test method.
        from vs_finance.constants import DepreciationMethod  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        asset = self._make_asset(entity, cost=1200000, salvage=200000, life=12)  # Assign test setup data.
        asset.method = DepreciationMethod.DECLINING_BALANCE  # Assign test setup data.
        asset.save(update_fields=["method"])  # Assign test setup data.
        build_depreciation_schedule(asset)  # Execute the test step.
        amounts = [r.amount for r in asset.schedule.all()]  # Assign test setup data.
        self.assertEqual(len(amounts), 12)  # Check the expected test outcome.
        # Sums to the depreciable base exactly (cost − salvage).
        self.assertEqual(sum(amounts), 1000000)  # Check the expected test outcome.
        # Front-loaded: first DB charge (2/12 of 1,200,000 = 200,000) beats straight-line.
        self.assertEqual(amounts[0], 200000)  # Check the expected test outcome.
        self.assertGreater(amounts[0], amounts[-1])  # Check the expected test outcome.
        # Never drives book value below salvage (every charge non-negative, monotone bv).
        bv = asset.cost  # Assign test setup data.
        for a in amounts:  # Iterate through test data.
            self.assertGreaterEqual(a, 0)  # Check the expected test outcome.
            bv -= a  # Assign test setup data.
        self.assertEqual(bv, asset.salvage_value)  # Check the expected test outcome.

    def test_schedule_remainder_lands_on_last_period(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        asset = self._make_asset(entity, cost=1000000, salvage=0, life=3)  # Assign test setup data.
        build_depreciation_schedule(asset)  # Execute the test step.
        amounts = [r.amount for r in asset.schedule.all()]  # Assign test setup data.
        # 1,000,000 / 3 = 333,333 r1 → last row carries the extra kobo.
        self.assertEqual(amounts, [333333, 333333, 333334])  # Check the expected test outcome.
        self.assertEqual(sum(amounts), 1000000)  # Check the expected test outcome.

    def test_post_depreciation_runs_and_completes(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)  # Assign test setup data.
        acquire_asset(asset, bank_account=bank)  # Assign test setup data.
        # Schedule charges Feb–Dec 2026 (100,000 each). Post the lot.
        posted = post_depreciation(asset, up_to_date=datetime.date(2026, 12, 31))  # Assign test setup data.
        asset.refresh_from_db()  # Execute the test step.
        self.assertEqual(len(posted), 11)  # Check the expected test outcome.
        self.assertEqual(asset.accumulated_depreciation, 1100000)  # Check the expected test outcome.
        self.assertEqual(asset.asset_status, AssetStatus.FULLY_DEPRECIATED)  # Check the expected test outcome.
        self.assertEqual(asset.net_book_value, 0)  # Check the expected test outcome.
        # Each charge: Dr 5400 expense, Cr 1900 accumulated depreciation.
        one = posted[0].journal  # Assign test setup data.
        self.assertEqual(one.lines.get(account__code="5400").debit, 100000)  # Check the expected test outcome.
        self.assertEqual(one.lines.get(account__code="1900").credit, 100000)  # Check the expected test outcome.

    def test_run_period_depreciation_posts_one_compound_journal(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        a1 = self._make_asset(entity, cost=1100000, salvage=0, life=11)  # Assign test setup data.
        a2 = self._make_asset(entity, cost=2200000, salvage=0, life=11)  # Assign test setup data.
        acquire_asset(a1, bank_account=bank)  # Assign test setup data.
        acquire_asset(a2, bank_account=bank)  # Assign test setup data.
        # Run everything due to Feb 2026: one charge each (100,000 + 200,000).
        result = run_period_depreciation(entity, up_to_date=datetime.date(2026, 2, 28))  # Assign test setup data.
        self.assertEqual(result["asset_count"], 2)  # Check the expected test outcome.
        self.assertEqual(result["total"], 300000)  # Check the expected test outcome.
        # One compound journal: Dr 5400 = 300,000, Cr 1900 = 300,000.
        from vs_finance.models import JournalEntry  # Import project symbols exercised by these tests.
        entry = JournalEntry.objects.get(id=result["journal_id"])  # Fetch test database data.
        self.assertEqual(entry.lines.get(account__code="5400").debit, 300000)  # Check the expected test outcome.
        self.assertEqual(entry.lines.get(account__code="1900").credit, 300000)  # Check the expected test outcome.
        a1.refresh_from_db()  # Execute the test step.
        self.assertEqual(a1.accumulated_depreciation, 100000)  # Check the expected test outcome.

    def test_run_period_depreciation_spanning_two_periods_posts_two_journals(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)  # Assign test setup data.
        acquire_asset(asset, bank_account=bank)  # Assign test setup data.
        # Charges due Feb 1 and Mar 1 (100,000 each). Run up to Mar 31.
        result = run_period_depreciation(entity, up_to_date=datetime.date(2026, 3, 31))  # Assign test setup data.
        self.assertEqual(result["period_count"], 2)  # Check the expected test outcome.
        self.assertEqual(len(result["journal_ids"]), 2)  # Check the expected test outcome.
        self.assertEqual(result["journal_id"], result["journal_ids"][0])  # Check the expected test outcome.
        self.assertEqual(result["total"], 200000)  # Check the expected test outcome.
        from vs_finance.models import JournalEntry  # Import project symbols exercised by these tests.
        entries = [JournalEntry.objects.get(id=j) for j in result["journal_ids"]]  # Fetch test database data.
        # Each journal is dated inside its own period and totals 100,000.
        for entry in entries:  # Iterate through test data.
            self.assertEqual(entry.lines.get(account__code="5400").debit, 100000)  # Check the expected test outcome.
            self.assertEqual(entry.lines.get(account__code="1900").credit, 100000)  # Check the expected test outcome.
            self.assertTrue(entry.period.start_date <= entry.date <= entry.period.end_date)  # Check the expected test outcome.
            self.assertLessEqual(entry.date, datetime.date(2026, 3, 31))  # Check the expected test outcome.
        # Chronological: first journal is February's.
        self.assertEqual(entries[0].period.period_no, 2)  # Check the expected test outcome.
        self.assertEqual(entries[1].period.period_no, 3)  # Check the expected test outcome.

    def test_run_period_depreciation_without_fiscal_period_raises_typed_error(self):  # Define a test helper or test method.
        # Schedule charges extending past the last seeded period (FY2026 only) must
        # surface a DepreciationError naming the date, not an AttributeError/500.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        # Acquired June 2026, 11 monthly charges → Jul 2026 … May 2027; no FY2027 exists.
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11,  # Continue structured test data.
                                 acq=datetime.date(2026, 6, 1))  # Assign test setup data.
        acquire_asset(asset, bank_account=bank)  # Assign test setup data.
        with self.assertRaises(DepreciationError) as ctx:  # Enter a test context manager.
            run_period_depreciation(entity, up_to_date=datetime.date(2027, 5, 31))  # Assign test setup data.
        self.assertIn("No fiscal period covers", str(ctx.exception))  # Check the expected test outcome.
        self.assertIn("2027", str(ctx.exception))  # Check the expected test outcome.

    def test_run_period_depreciation_single_period_returns_one_journal(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)  # Assign test setup data.
        acquire_asset(asset, bank_account=bank)  # Assign test setup data.
        result = run_period_depreciation(entity, up_to_date=datetime.date(2026, 2, 28))  # Assign test setup data.
        self.assertEqual(result["period_count"], 1)  # Check the expected test outcome.
        self.assertEqual(result["journal_ids"], [result["journal_id"]])  # Check the expected test outcome.
        self.assertEqual(result["total"], 100000)  # Check the expected test outcome.

    def test_dispose_asset_books_proceeds_and_gain_loss(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)  # Assign test setup data.
        acquire_asset(asset, bank_account=bank)  # Assign test setup data.
        post_depreciation(asset, up_to_date=datetime.date(2026, 3, 31))  # 2 charges = 200,000
        asset.refresh_from_db()  # Execute the test step.
        nbv = asset.net_book_value  # 900,000
        # Sell for 950,000 → 50,000 gain; gain to 4100 income. Dispose on Mar 31 so no
        # depreciation charge is yet due-but-unposted (the Apr 1 charge is future-dated).
        entry = dispose_asset(  # Continue structured test data.
            asset, disposal_date=datetime.date(2026, 3, 31), proceeds=950000,  # Continue structured test data.
            bank_account=bank, gain_loss_account=Account.objects.get(entity=entity, code="4100"))  # Fetch test database data.
        asset.refresh_from_db()  # Execute the test step.
        self.assertEqual(asset.asset_status, AssetStatus.DISPOSED)  # Check the expected test outcome.
        # Dr 1900 accum (200,000) + Dr cash 950,000; Cr 1500 cost 1,100,000; Cr 4100 gain 50,000.
        self.assertEqual(entry.lines.get(account__code="1900").debit, 200000)  # Check the expected test outcome.
        self.assertEqual(entry.lines.get(account__code="1500").credit, 1100000)  # Check the expected test outcome.
        self.assertEqual(entry.lines.get(account__code="4100").credit, 950000 - nbv)  # Check the expected test outcome.

    def test_post_depreciation_on_draft_asset_is_rejected(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        asset = self._make_asset(entity)  # DRAFT — never acquired
        self.assertEqual(asset.asset_status, AssetStatus.DRAFT)  # Check the expected test outcome.
        with self.assertRaises(DepreciationError):  # Enter a test context manager.
            post_depreciation(asset, up_to_date=datetime.date(2026, 12, 31))  # Assign test setup data.

    def test_cannot_rebuild_schedule_after_posting(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        asset = self._make_asset(entity)  # Assign test setup data.
        acquire_asset(asset, bank_account=bank)  # Assign test setup data.
        post_depreciation(asset, up_to_date=datetime.date(2026, 2, 28))  # Assign test setup data.
        with self.assertRaises(DepreciationError):  # Enter a test context manager.
            build_depreciation_schedule(asset)  # Execute the test step.

    def test_dispose_blocked_when_due_depreciation_unposted(self):  # Define a test helper or test method.
        # A charge due Feb 1 2026 is still unposted; disposing Mar 1 must refuse.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)  # Assign test setup data.
        acquire_asset(asset, bank_account=bank)  # Assign test setup data.
        with self.assertRaises(DepreciationError) as ctx:  # Enter a test context manager.
            dispose_asset(asset, disposal_date=datetime.date(2026, 3, 1),  # Continue structured test data.
                          proceeds=0, bank_account=bank)  # Assign test setup data.
        self.assertIn("unposted", str(ctx.exception).lower())  # Check the expected test outcome.
        asset.refresh_from_db()  # Execute the test step.
        self.assertEqual(asset.asset_status, AssetStatus.ACTIVE)  # nothing disposed

    def test_dispose_succeeds_after_posting_due_depreciation(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)  # Assign test setup data.
        acquire_asset(asset, bank_account=bank)  # Assign test setup data.
        # Post the two charges due up to the disposal date (Feb 1 + Mar 1), then dispose.
        post_depreciation(asset, up_to_date=datetime.date(2026, 3, 1))  # Assign test setup data.
        loss = Account.objects.get(entity=entity, code="5300")  # Fetch test database data.
        dispose_asset(asset, disposal_date=datetime.date(2026, 3, 1), proceeds=0,  # Continue structured test data.
                      bank_account=bank, gain_loss_account=loss)  # Assign test setup data.
        asset.refresh_from_db()  # Execute the test step.
        self.assertEqual(asset.asset_status, AssetStatus.DISPOSED)  # Check the expected test outcome.

    def test_dispose_ignores_future_dated_unposted_charges(self):  # Define a test helper or test method.
        # Disposing on the acquisition date: every charge (Feb+) is future-dated and may
        # be orphaned (life cut short), so the disposal is allowed.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)  # Assign test setup data.
        acquire_asset(asset, bank_account=bank)  # Assign test setup data.
        loss = Account.objects.get(entity=entity, code="5300")  # Fetch test database data.
        dispose_asset(asset, disposal_date=datetime.date(2026, 1, 1), proceeds=0,  # Continue structured test data.
                      bank_account=bank, gain_loss_account=loss)  # Assign test setup data.
        asset.refresh_from_db()  # Execute the test step.
        self.assertEqual(asset.asset_status, AssetStatus.DISPOSED)  # Check the expected test outcome.


class PeriodCloseTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_checklist_passes_on_clean_ledger(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],  # Continue structured test data.
        ))  # Execute the test step.
        checklist = close_checklist(entity, periods[0])  # Assign test setup data.
        self.assertTrue(checklist.passed)  # Check the expected test outcome.
        names = {i.name for i in checklist.items}  # Assign test setup data.
        self.assertIn("trial_balance_balanced", names)  # Check the expected test outcome.
        self.assertIn("ar_reconciled", names)  # Check the expected test outcome.
        self.assertIn("depreciation_posted", names)  # Check the expected test outcome.

    def test_close_reopen_and_lock_cycle(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        jan = periods[0]  # Assign test setup data.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, jan, [("1100", 50000, 0), ("4100", 0, 50000)],  # Continue structured test data.
        ))  # Execute the test step.
        period, checklist = close_period(entity, jan)  # Assign test setup data.
        self.assertEqual(period.status, PeriodStatus.CLOSED)  # Check the expected test outcome.
        self.assertIsNotNone(period.closed_at)  # Check the expected test outcome.

        reopen_period(entity, jan)  # Execute the test step.
        jan.refresh_from_db()  # Execute the test step.
        self.assertEqual(jan.status, PeriodStatus.OPEN)  # Check the expected test outcome.
        self.assertIsNone(jan.closed_at)  # Check the expected test outcome.

        close_period(entity, jan)  # Execute the test step.
        lock_period(entity, jan)  # Execute the test step.
        jan.refresh_from_db()  # Execute the test step.
        self.assertEqual(jan.status, PeriodStatus.LOCKED)  # Check the expected test outcome.
        # A LOCKED period cannot be reopened.
        with self.assertRaises(PeriodCloseError):  # Enter a test context manager.
            reopen_period(entity, jan)  # Execute the test step.

    def test_reopen_closed_period_returns_to_open(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        jan = periods[0]  # Assign test setup data.
        close_period(entity, jan)  # Execute the test step.
        jan.refresh_from_db()  # Execute the test step.
        self.assertEqual(jan.status, PeriodStatus.CLOSED)  # Check the expected test outcome.
        reopen_period(entity, jan)  # Execute the test step.
        jan.refresh_from_db()  # Execute the test step.
        self.assertEqual(jan.status, PeriodStatus.OPEN)  # Check the expected test outcome.

    def test_lock_closed_period_seals_it(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        jan = periods[0]  # Assign test setup data.
        close_period(entity, jan)  # Execute the test step.
        lock_period(entity, jan)  # Execute the test step.
        jan.refresh_from_db()  # Execute the test step.
        self.assertEqual(jan.status, PeriodStatus.LOCKED)  # Check the expected test outcome.

    def test_lock_refuses_non_closed_period(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        jan = periods[0]  # still OPEN
        with self.assertRaises(PeriodCloseError):  # Enter a test context manager.
            lock_period(entity, jan)  # Execute the test step.
        jan.refresh_from_db()  # Execute the test step.
        self.assertEqual(jan.status, PeriodStatus.OPEN)  # Check the expected test outcome.

    def test_reopen_refuses_locked_period(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        jan = periods[0]  # Assign test setup data.
        close_period(entity, jan)  # Execute the test step.
        lock_period(entity, jan)  # Execute the test step.
        with self.assertRaises(PeriodCloseError):  # Enter a test context manager.
            reopen_period(entity, jan)  # Execute the test step.

    def test_soft_close_allows_depreciation_auto_posting(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        period, _ = close_period(entity, periods[0], soft=True)  # Assign test setup data.
        self.assertEqual(period.status, PeriodStatus.SOFT_CLOSED)  # Check the expected test outcome.

    def test_blocking_failure_requires_force(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        jan = periods[0]  # Assign test setup data.
        # Post straight into the AR control with no sub-ledger invoice → control != sub-ledger.
        ar = Account.objects.get(entity=entity, code="1200")  # Fetch test database data.
        Customer.objects.create(entity=entity, code="C1", name="Acme", receivable_account=ar)  # Create test database data.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, jan, [("1200", 50000, 0), ("4100", 0, 50000)],  # Continue structured test data.
        ))  # Execute the test step.
        with self.assertRaises(PeriodCloseError):  # Enter a test context manager.
            close_period(entity, jan)  # Execute the test step.
        jan.refresh_from_db()  # Execute the test step.
        self.assertEqual(jan.status, PeriodStatus.OPEN)  # Check the expected test outcome.
        # Forcing over the failure closes it anyway.
        period, checklist = close_period(entity, jan, force=True)  # Assign test setup data.
        self.assertEqual(period.status, PeriodStatus.CLOSED)  # Check the expected test outcome.
        self.assertFalse(checklist.passed)  # Check the expected test outcome.

    def test_extra_checks_are_injected(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        calls = []  # Assign test setup data.

        def failing_check():  # Define a test helper or test method.
            calls.append(True)  # Execute the test step.
            return ("ap_reconciled", False, "sub-ledger 100 vs control 0")  # Return the prepared test value.

        with self.assertRaises(PeriodCloseError):  # Enter a test context manager.
            close_period(entity, periods[0], extra_checks=[failing_check])  # Assign test setup data.
        self.assertTrue(calls)  # the injected check actually ran


class FinancialStatementTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The three primary statements over one coherent set of transactions.

    A tiny but complete first month:
      * owner injects 1,000,000 capital (financing inflow)
      * buys 400,000 of equipment for cash (investing outflow)
      * earns 300,000 cash revenue (operating inflow)
      * pays 120,000 cash salaries (operating outflow)
    """

    def _seed_activity(self, entity, period):  # Define a test helper or test method.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period, [("1100", 1000000, 0), ("3100", 0, 1000000)],  # Continue structured test data.
        ))  # capital
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period, [("1500", 400000, 0), ("1100", 0, 400000)],  # Continue structured test data.
        ))  # buy equipment
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period, [("1100", 300000, 0), ("4100", 0, 300000)],  # Continue structured test data.
        ))  # cash revenue
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period, [("5200", 120000, 0), ("1100", 0, 120000)],  # Continue structured test data.
        ))  # salaries

    def test_income_statement_nets_revenue_less_expense(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        self._seed_activity(entity, periods[0])  # Execute the test step.

        pnl = income_statement(entity, period=periods[0])  # Assign test setup data.
        self.assertEqual(pnl.total_income, 300000)  # Check the expected test outcome.
        self.assertEqual(pnl.total_expense, 120000)  # Check the expected test outcome.
        self.assertEqual(pnl.net_income, 180000)  # Check the expected test outcome.
        # Income rows carry positive (credit-natural) magnitudes.
        rev = next(r for r in pnl.income_rows if r.code == "4100")  # Execute the test step.
        self.assertEqual(rev.amount, 300000)  # Check the expected test outcome.

    def test_income_statement_aggregates_all_periods_when_unscoped(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        # Revenue split across two months.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 100000, 0), ("4100", 0, 100000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 10),  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[1], [("1100", 250000, 0), ("4100", 0, 250000)],  # Continue structured test data.
            date=datetime.date(2026, 2, 10),  # Continue structured test data.
        ))  # Execute the test step.
        self.assertEqual(income_statement(entity).total_income, 350000)  # Check the expected test outcome.
        self.assertEqual(income_statement(entity, period=periods[0]).total_income, 100000)  # Check the expected test outcome.

    def test_balance_sheet_balances_with_unclosed_net_income(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        self._seed_activity(entity, periods[0])  # Execute the test step.

        bs = balance_sheet(entity)  # Assign test setup data.
        self.assertEqual(bs.total_assets, 1180000)        # 780k cash + 400k PP&E
        self.assertEqual(bs.total_liabilities, 0)  # Check the expected test outcome.
        self.assertEqual(bs.total_equity_accounts, 1000000)  # share capital
        self.assertEqual(bs.retained_earnings, 180000)       # unclosed net income
        self.assertEqual(bs.total_equity, 1180000)  # Check the expected test outcome.
        self.assertTrue(bs.is_balanced)  # Check the expected test outcome.
        self.assertEqual(bs.difference, 0)  # Check the expected test outcome.

    def test_cash_flow_reconciles_and_classifies(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        self.make_bank(entity)  # 1100 is also a mapped bank account
        self._seed_activity(entity, periods[0])  # Execute the test step.

        cf = cash_flow_statement(entity)  # Assign test setup data.
        self.assertEqual(cf.opening_cash, 0)  # Check the expected test outcome.
        self.assertEqual(cf.closing_cash, 780000)  # Check the expected test outcome.
        self.assertEqual(cf.by_activity["operating"], 180000)   # 300k rev - 120k pay
        self.assertEqual(cf.by_activity["investing"], -400000)  # equipment
        self.assertEqual(cf.by_activity["financing"], 1000000)  # capital
        self.assertEqual(cf.net_change, 780000)  # Check the expected test outcome.
        self.assertTrue(cf.is_reconciled)  # Check the expected test outcome.

    def test_balance_sheet_sections_group_by_ifrs_and_balance(self):  # Define a test helper or test method.
        from .reports import balance_sheet_sections  # Import project symbols exercised by these tests.
        entity, _, periods = self.build_books()  # Assign test setup data.
        self._seed_activity(entity, periods[0])  # Execute the test step.

        bs = balance_sheet_sections(entity)  # Assign test setup data.
        self.assertTrue(bs.is_balanced)  # Check the expected test outcome.
        self.assertEqual(bs.total_assets, 1180000)  # Check the expected test outcome.
        self.assertEqual(bs.total_liabilities, 0)  # Check the expected test outcome.
        self.assertEqual(bs.total_equity, 1180000)  # Check the expected test outcome.
        self.assertEqual(bs.current_year_earnings, 180000)  # Check the expected test outcome.

        by_key = {s.key: s for s in bs.sections}  # Assign test setup data.
        self.assertEqual(  # Check the expected test outcome.
            set(by_key),  # Continue structured test data.
            {"non_current_assets", "current_assets", "equity",  # Continue structured test data.
             "non_current_liabilities", "current_liabilities"})  # Execute the test step.
        # Cash (1100) → current assets 780,000; PP&E (1500) → non-current 400,000.
        self.assertEqual(by_key["current_assets"].total, 780000)  # Check the expected test outcome.
        self.assertEqual(by_key["non_current_assets"].total, 400000)  # Check the expected test outcome.
        # The unclosed net income shows as its own equity line, not folded away.
        self.assertIn(  # Check the expected test outcome.
            "Current year earnings", [g.label for g in by_key["equity"].groups])  # Execute the test step.

    def test_balance_sheet_nets_contra_asset_and_balances(self):  # Define a test helper or test method.
        # Accumulated depreciation is a contra-asset (credit balance). It must REDUCE
        # PP&E and keep the sheet balanced — not be added to assets.
        from .reports import balance_sheet_sections  # Import project symbols exercised by these tests.
        entity, _, periods = self.build_books()  # Assign test setup data.
        p = periods[0]  # Assign test setup data.
        post_journal(self.make_entry(entity, p, [("1100", 1000000, 0), ("3100", 0, 1000000)]))  # capital
        post_journal(self.make_entry(entity, p, [("1500", 400000, 0), ("1100", 0, 400000)]))     # buy equipment
        post_journal(self.make_entry(entity, p, [("5400", 100000, 0), ("1900", 0, 100000)]))      # depreciation

        bs = balance_sheet_sections(entity)  # Assign test setup data.
        self.assertTrue(bs.is_balanced)  # Check the expected test outcome.
        self.assertEqual(bs.total_assets, 900000)   # 600k cash + 300k net PP&E
        by_key = {s.key: s for s in bs.sections}  # Assign test setup data.
        ppe = next(g for g in by_key["non_current_assets"].groups if g.line == "PPE")  # Execute the test step.
        self.assertEqual(ppe.amount, 300000)         # 400k cost − 100k accumulated dep
        self.assertEqual(by_key["non_current_assets"].total, 300000)  # Check the expected test outcome.

    def test_cash_flow_ignores_non_cash_journals(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        # An accrual that never touches cash (Dr expense, Cr payable) must not move cash.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("5300", 50000, 0), ("2100", 0, 50000)],  # Continue structured test data.
        ))  # Execute the test step.
        cf = cash_flow_statement(entity)  # Assign test setup data.
        self.assertEqual(cf.closing_cash, 0)  # Check the expected test outcome.
        self.assertEqual(cf.net_change, 0)  # Check the expected test outcome.
        self.assertTrue(cf.is_reconciled)  # Check the expected test outcome.

    def test_cash_flow_breaks_activities_into_line_items(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        self._seed_activity(entity, periods[0])  # Execute the test step.

        cf = cash_flow_statement(entity)  # Assign test setup data.
        # Operating splits into the revenue (source) and salaries (use) counter-accounts.
        op = {ln.code: ln.amount for ln in cf.activity_lines["operating"]}  # Assign test setup data.
        self.assertEqual(op, {"4100": 300000, "5200": -120000})  # Check the expected test outcome.
        inv = {ln.code: ln.amount for ln in cf.activity_lines["investing"]}  # Assign test setup data.
        self.assertEqual(inv, {"1500": -400000})   # equipment purchase (cash out)
        fin = {ln.code: ln.amount for ln in cf.activity_lines["financing"]}  # Assign test setup data.
        self.assertEqual(fin, {"3100": 1000000})   # owner capital (cash in)
        # Line items foot to their activity subtotal.
        self.assertEqual(sum(op.values()), cf.by_activity["operating"])  # Check the expected test outcome.


class IncomeStatementCompareTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The P&L with Budget + Prior-year comparison columns (income_statement_compare)."""

    def _activity(self, entity, period, *, revenue, expense):  # Define a test helper or test method.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period, [("1100", revenue, 0), ("4100", 0, revenue)]))  # cash revenue
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period, [("5200", expense, 0), ("1100", 0, expense)]))  # cash expense

    def test_no_comparison_without_budget_or_prior_year(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        self._activity(entity, periods[0], revenue=300000, expense=120000)  # Assign test setup data.

        rep = income_statement_compare(entity, period=periods[0])  # Assign test setup data.
        self.assertFalse(rep.has_budget)  # Check the expected test outcome.
        self.assertFalse(rep.has_prior_year)  # Check the expected test outcome.
        inc = {r.code: r for r in rep.income_rows}  # Assign test setup data.
        exp = {r.code: r for r in rep.expense_rows}  # Assign test setup data.
        self.assertEqual(inc["4100"].amount, 300000)  # Check the expected test outcome.
        self.assertIsNone(inc["4100"].budget)  # Check the expected test outcome.
        self.assertIsNone(inc["4100"].prior_year)  # Check the expected test outcome.
        self.assertEqual(exp["5200"].amount, 120000)  # Check the expected test outcome.
        self.assertEqual(rep.net_totals.amount, 180000)  # Check the expected test outcome.
        self.assertIsNone(rep.net_totals.variance)  # Check the expected test outcome.

    def test_budget_and_prior_year_columns_populate_with_favourable_variance(self):  # Define a test helper or test method.
        from .constants import BudgetStatus  # Import project symbols exercised by these tests.
        from .models import Account, Budget, BudgetLine, FiscalPeriod, FiscalYear  # Import project symbols exercised by these tests.

        entity, year, periods = self.build_books()  # Assign test setup data.
        # Current-year actuals.
        self._activity(entity, periods[0], revenue=300000, expense=120000)  # Assign test setup data.

        # A prior fiscal year (2025) with its own activity.
        prior_year = FiscalYear.objects.create(  # Create test database data.
            entity=entity, year=2025,  # Continue structured test data.
            start_date=datetime.date(2025, 1, 1), end_date=datetime.date(2025, 12, 31))  # Assign test setup data.
        prior_period = FiscalPeriod.objects.create(  # Create test database data.
            entity=entity, fiscal_year=prior_year, period_no=1, name="2025-01",  # Continue structured test data.
            start_date=datetime.date(2025, 1, 1), end_date=datetime.date(2025, 1, 31))  # Assign test setup data.
        self._activity(entity, prior_period, revenue=200000, expense=80000)  # Assign test setup data.

        # An approved budget for the current year.
        budget = Budget.objects.create(  # Create test database data.
            entity=entity, fiscal_year=year, name="Plan", status=BudgetStatus.APPROVED)  # Assign test setup data.
        BudgetLine.objects.create(  # Create test database data.
            budget=budget, account=Account.objects.get(entity=entity, code="4100"),  # Fetch test database data.
            period_no=1, amount=250000)  # Assign test setup data.
        BudgetLine.objects.create(  # Create test database data.
            budget=budget, account=Account.objects.get(entity=entity, code="5200"),  # Fetch test database data.
            period_no=1, amount=150000)  # Assign test setup data.

        rep = income_statement_compare(entity)  # YTD → current FY = 2026 (latest)
        self.assertTrue(rep.has_budget)  # Check the expected test outcome.
        self.assertTrue(rep.has_prior_year)  # Check the expected test outcome.
        self.assertEqual(rep.fiscal_year, 2026)  # Check the expected test outcome.
        self.assertEqual(rep.prior_fiscal_year, 2025)  # Check the expected test outcome.

        inc = {r.code: r for r in rep.income_rows}["4100"]  # Assign test setup data.
        self.assertEqual(inc.amount, 300000)  # Check the expected test outcome.
        self.assertEqual(inc.budget, 250000)  # Check the expected test outcome.
        self.assertEqual(inc.variance, 50000)      # revenue: actual − budget (favourable)
        self.assertEqual(inc.prior_year, 200000)  # Check the expected test outcome.

        exp = {r.code: r for r in rep.expense_rows}["5200"]  # Assign test setup data.
        self.assertEqual(exp.amount, 120000)  # Check the expected test outcome.
        self.assertEqual(exp.budget, 150000)  # Check the expected test outcome.
        self.assertEqual(exp.variance, 30000)      # expense: budget − actual (favourable)
        self.assertEqual(exp.prior_year, 80000)  # Check the expected test outcome.

        self.assertEqual(rep.net_totals.amount, 180000)  # Check the expected test outcome.
        self.assertEqual(rep.net_totals.budget, 100000)  # Check the expected test outcome.
        self.assertEqual(rep.net_totals.variance, 80000)  # Check the expected test outcome.
        self.assertEqual(rep.net_totals.prior_year, 120000)  # Check the expected test outcome.


class ChangesInEquityTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The statement of changes in equity over a two-month, two-component scenario."""

    def _col(self, soce, key):  # Define a test helper or test method.
        return next(c for c in soce.columns if c.key == key)  # Return the prepared test value.

    def test_single_period_splits_capital_from_profit(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        # Jan: 1,000,000 capital + 180,000 net income (300k rev − 120k salaries).
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 1000000, 0), ("3100", 0, 1000000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 5),  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 300000, 0), ("4100", 0, 300000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 10),  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("5200", 120000, 0), ("1100", 0, 120000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 20),  # Continue structured test data.
        ))  # Execute the test step.

        soce = statement_of_changes_in_equity(entity, period=periods[0])  # Assign test setup data.
        cap = self._col(soce, "3100")  # Assign test setup data.
        self.assertEqual(cap.opening, 0)  # Check the expected test outcome.
        self.assertEqual(cap.contributions, 1000000)  # Check the expected test outcome.
        self.assertEqual(cap.closing, 1000000)  # Check the expected test outcome.
        re = self._col(soce, "retained_earnings")  # Assign test setup data.
        self.assertEqual(re.opening, 0)  # Check the expected test outcome.
        self.assertEqual(re.profit, 180000)  # Check the expected test outcome.
        self.assertEqual(re.closing, 180000)  # Check the expected test outcome.
        self.assertEqual(soce.total_opening, 0)  # Check the expected test outcome.
        self.assertEqual(soce.total_profit, 180000)  # Check the expected test outcome.
        self.assertEqual(soce.total_contributions, 1000000)  # Check the expected test outcome.
        self.assertEqual(soce.total_closing, 1180000)  # Check the expected test outcome.
        self.assertTrue(soce.is_reconciled)  # Check the expected test outcome.

    def test_period_carries_opening_and_books_distribution(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        # January.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 1000000, 0), ("3100", 0, 1000000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 5),  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 300000, 0), ("4100", 0, 300000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 10),  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("5200", 120000, 0), ("1100", 0, 120000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 20),  # Continue structured test data.
        ))  # Execute the test step.
        # February: 500k more capital, a 50k dividend (Dr retained earnings/Cr cash),
        # and 120k net income (200k rev − 80k expense).
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[1], [("1100", 500000, 0), ("3100", 0, 500000)],  # Continue structured test data.
            date=datetime.date(2026, 2, 4),  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[1], [("3200", 50000, 0), ("1100", 0, 50000)],  # Continue structured test data.
            date=datetime.date(2026, 2, 6),  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[1], [("1100", 200000, 0), ("4100", 0, 200000)],  # Continue structured test data.
            date=datetime.date(2026, 2, 12),  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[1], [("5300", 80000, 0), ("1100", 0, 80000)],  # Continue structured test data.
            date=datetime.date(2026, 2, 18),  # Continue structured test data.
        ))  # Execute the test step.

        soce = statement_of_changes_in_equity(entity, period=periods[1])  # Assign test setup data.
        cap = self._col(soce, "3100")  # Assign test setup data.
        self.assertEqual(cap.opening, 1000000)        # carried from January
        self.assertEqual(cap.contributions, 500000)  # Check the expected test outcome.
        self.assertEqual(cap.closing, 1500000)  # Check the expected test outcome.
        dist = self._col(soce, "3200")  # Assign test setup data.
        self.assertEqual(dist.opening, 0)  # Check the expected test outcome.
        self.assertEqual(dist.contributions, -50000)  # dividend is a distribution
        self.assertEqual(dist.closing, -50000)  # Check the expected test outcome.
        re = self._col(soce, "retained_earnings")  # Assign test setup data.
        self.assertEqual(re.opening, 180000)          # January's unclosed profit
        self.assertEqual(re.profit, 120000)  # Check the expected test outcome.
        self.assertEqual(re.closing, 300000)  # Check the expected test outcome.
        self.assertEqual(soce.total_opening, 1180000)  # Check the expected test outcome.
        self.assertEqual(soce.total_contributions, 450000)  # Check the expected test outcome.
        self.assertEqual(soce.total_profit, 120000)  # Check the expected test outcome.
        self.assertEqual(soce.total_closing, 1750000)  # Check the expected test outcome.
        self.assertTrue(soce.is_reconciled)  # Check the expected test outcome.

    def test_unscoped_reconciles_to_balance_sheet_equity(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 1000000, 0), ("3100", 0, 1000000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 5),  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 300000, 0), ("4100", 0, 300000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 10),  # Continue structured test data.
        ))  # Execute the test step.
        soce = statement_of_changes_in_equity(entity)  # Assign test setup data.
        # Life-to-date: everything is a movement from a zero opening.
        self.assertEqual(soce.total_opening, 0)  # Check the expected test outcome.
        self.assertEqual(soce.total_closing, balance_sheet(entity).total_equity)  # Check the expected test outcome.
        self.assertTrue(soce.is_reconciled)  # Check the expected test outcome.


class StatutoryPackTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The IFRS-for-SMEs statutory pack regroups the chart onto presentation lines."""

    def _seed_activity(self, entity, period):  # Define a test helper or test method.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period, [("1100", 1000000, 0), ("3100", 0, 1000000)],  # Continue structured test data.
        ))  # capital
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period, [("1500", 400000, 0), ("1100", 0, 400000)],  # Continue structured test data.
        ))  # buy equipment
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period, [("1100", 300000, 0), ("4100", 0, 300000)],  # Continue structured test data.
        ))  # cash revenue
        post_journal(self.make_entry(  # Continue structured test data.
            entity, period, [("5200", 120000, 0), ("1100", 0, 120000)],  # Continue structured test data.
        ))  # salaries

    def _group(self, section, line):  # Define a test helper or test method.
        return next((g for g in section.groups if g.line == line), None)  # Return the prepared test value.

    def _section(self, pack, key):  # Define a test helper or test method.
        return next(s for s in pack.sofp_sections if s.key == key)  # Return the prepared test value.

    def test_sofp_regroups_chart_onto_ifrs_lines(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        self._seed_activity(entity, periods[0])  # Execute the test step.

        pack = statutory_pack(entity)  # Assign test setup data.
        nca = self._section(pack, "non_current_assets")  # Assign test setup data.
        self.assertEqual(self._group(nca, "PPE").amount, 400000)  # Check the expected test outcome.
        ca = self._section(pack, "current_assets")  # Assign test setup data.
        self.assertEqual(self._group(ca, "CASH").amount, 780000)  # 1000k-400k+300k-120k
        eq = self._section(pack, "equity")  # Assign test setup data.
        self.assertEqual(self._group(eq, "SHARE_CAPITAL").amount, 1000000)  # Check the expected test outcome.
        # Unclosed P&L is folded into the retained-earnings equity line.
        self.assertEqual(self._group(eq, "RETAINED_EARNINGS").amount, 180000)  # Check the expected test outcome.

        self.assertEqual(pack.total_assets, 1180000)  # Check the expected test outcome.
        self.assertEqual(pack.total_equity, 1180000)  # Check the expected test outcome.
        self.assertEqual(pack.total_liabilities, 0)  # Check the expected test outcome.
        self.assertTrue(pack.is_balanced)  # Check the expected test outcome.
        self.assertEqual(pack.difference, 0)  # Check the expected test outcome.

    def test_income_statement_maps_to_ifrs_lines(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        self._seed_activity(entity, periods[0])  # Execute the test step.

        pack = statutory_pack(entity)  # Assign test setup data.
        lines = {g.line: g.amount for g in pack.income_lines}  # Assign test setup data.
        self.assertEqual(lines["REVENUE"], 300000)  # Check the expected test outcome.
        self.assertEqual(lines["ADMIN_EXPENSES"], 120000)  # salaries map here
        self.assertEqual(pack.total_income, 300000)  # Check the expected test outcome.
        self.assertEqual(pack.total_expense, 120000)  # Check the expected test outcome.
        self.assertEqual(pack.net_income, 180000)  # Check the expected test outcome.

    def test_companion_statements_ride_along_and_reconcile(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        self.make_bank(entity)  # Execute the test step.
        self._seed_activity(entity, periods[0])  # Execute the test step.

        pack = statutory_pack(entity)  # Assign test setup data.
        self.assertTrue(pack.cash_flow.is_reconciled)  # Check the expected test outcome.
        self.assertEqual(pack.cash_flow.closing_cash, 780000)  # Check the expected test outcome.
        self.assertTrue(pack.changes_in_equity.is_reconciled)  # Check the expected test outcome.
        self.assertEqual(pack.changes_in_equity.total_closing, 1180000)  # Check the expected test outcome.
        self.assertTrue(pack.trial_balance.is_balanced)  # Check the expected test outcome.

    def test_unmapped_account_falls_back_to_type_default(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        # A custom asset account with no explicit IFRS line.
        Account.objects.create(  # Create test database data.
            entity=entity, code="1250", name="Prepayments",  # Continue structured test data.
            account_type=AccountType.ASSET, is_postable=True,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1250", 90000, 0), ("4100", 0, 90000)],  # Continue structured test data.
        ))  # Execute the test step.
        pack = statutory_pack(entity)  # Assign test setup data.
        ca = self._section(pack, "current_assets")  # Assign test setup data.
        self.assertEqual(self._group(ca, "OTHER_CURRENT_ASSETS").amount, 90000)  # Check the expected test outcome.


class FinanceAPITests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The /v1/finance/ REST surface: entity scoping, reports, documents, actions.

    Authenticated as a Vision super admin, which bypasses the per-endpoint RBAC gate
    (so these tests exercise routing/serialisation, not the RBAC matrix itself).
    """

    def setUp(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from rest_framework.test import APIClient  # Import project symbols exercised by these tests.
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment  # Import project symbols exercised by these tests.

        User = get_user_model()  # Assign test setup data.
        self.user = User.objects.create_user(  # Create test database data.
            email="fin-admin@test.com", password="testpass123",  # Continue structured test data.
            user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Finance", last_name="Admin",  # Continue structured test data.
        )  # Close the grouped test expression.
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")  # Create test database data.
        PlatformUserRoleAssignment.objects.create(  # Create test database data.
            user=self.user, role=role, assignment_status="ACTIVE",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.client = APIClient()  # Assign test setup data.
        self.client.force_authenticate(user=self.user)  # Exercise the test HTTP client.

    def _create_claim(self, entity):  # Define a test helper or test method.
        return self.client.post(  # Return the prepared test value.
            f"/v1/finance/expense-claims/?entity={entity.code}",  # Continue structured test data.
            {"claimant_name": "Jane Staff", "claim_date": "2026-01-10", "title": "Trip",  # Continue structured test data.
             "lines": [{"description": "Diesel", "expense_account": "5300",  # Continue structured test data.
                        "quantity": 1, "unit_price": 100000}]}, format="json")  # Assign test setup data.

    def test_expense_claim_reject_only_from_draft(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        created = self._create_claim(entity)  # Assign test setup data.
        self.assertEqual(created.status_code, 201, created.content)  # Check the expected test outcome.
        cid = created.json()["data"]["id"]  # Assign test setup data.
        rej = self.client.post(f"/v1/finance/expense-claims/{cid}/reject/?entity={entity.code}", {}, format="json")  # Exercise the test HTTP client.
        self.assertEqual(rej.status_code, 200, rej.content)  # Check the expected test outcome.
        self.assertEqual(rej.json()["data"]["status"], "CANCELLED")  # Check the expected test outcome.
        # A cancelled claim can't be rejected again.
        again = self.client.post(f"/v1/finance/expense-claims/{cid}/reject/?entity={entity.code}", {}, format="json")  # Exercise the test HTTP client.
        self.assertEqual(again.status_code, 400, again.content)  # Check the expected test outcome.

    def test_expense_line_receipt_upload_and_remove(self):  # Define a test helper or test method.
        from django.core.files.uploadedfile import SimpleUploadedFile  # Import project symbols exercised by these tests.

        entity, _, _ = self.build_books()  # Assign test setup data.
        created = self._create_claim(entity)  # Assign test setup data.
        cid = created.json()["data"]["id"]  # Assign test setup data.
        line_id = created.json()["data"]["lines"][0]["id"]  # Assign test setup data.
        self.assertIsNone(created.json()["data"]["lines"][0]["receipt_url"])  # Check the expected test outcome.

        up = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/expense-claims/{cid}/lines/{line_id}/receipt/?entity={entity.code}",  # Continue structured test data.
            {"file": SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 fake", content_type="application/pdf")},  # Continue structured test data.
            format="multipart")  # Assign test setup data.
        self.assertEqual(up.status_code, 201, up.content)  # Check the expected test outcome.
        line = up.json()["data"]["lines"][0]  # Assign test setup data.
        self.assertTrue(line["receipt_name"].startswith("receipt"))  # Check the expected test outcome.
        self.assertTrue(line["receipt_url"])  # Check the expected test outcome.

        rm = self.client.delete(f"/v1/finance/expense-claims/{cid}/lines/{line_id}/receipt/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(rm.status_code, 200, rm.content)  # Check the expected test outcome.
        self.assertIsNone(rm.json()["data"]["lines"][0]["receipt_url"])  # Check the expected test outcome.

    def test_petty_cash_register_and_spent_week(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        fund = PettyCashFund.objects.create(  # Create test database data.
            entity=entity, name="Front Desk", custodian_name="Lola",  # Continue structured test data.
            gl_account=Account.objects.get(entity=entity, code="1110"), float_amount=5000000)  # Fetch test database data.
        establish_fund(fund, bank_account=bank, amount=5000000, date=datetime.date.today())  # Assign test setup data.
        v = PettyCashVoucher.objects.create(  # Create test database data.
            entity=entity, fund=fund, voucher_date=datetime.date.today(), payee="Shop")  # Assign test setup data.
        PettyCashVoucherLine.objects.create(  # Create test database data.
            voucher=v, expense_account=Account.objects.get(entity=entity, code="5300"),  # Fetch test database data.
            quantity=1, unit_price=120000, line_no=1)  # Assign test setup data.
        post_voucher(v)  # Execute the test step.

        resp = self.client.get(f"/v1/finance/petty-cash-funds/{fund.id}/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(data["spent_this_week"], 120000)  # Check the expected test outcome.
        reg = data["register"]  # Assign test setup data.
        # Newest first: the spend (out), then the establish top-up (in).
        self.assertEqual(reg[0]["out"], 120000)  # Check the expected test outcome.
        self.assertEqual(reg[0]["category"], Account.objects.get(entity=entity, code="5300").name)  # Check the expected test outcome.
        self.assertEqual(reg[0]["balance"], 4880000)  # 5,000,000 − 120,000
        self.assertEqual(reg[-1]["in"], 5000000)  # Check the expected test outcome.
        self.assertEqual(reg[-1]["category"], "Top-up")  # Check the expected test outcome.

    def test_customer_opening_balance_backdated_to_opening_date(self):  # Define a test helper or test method.
        # A historical opening_date inside an open period backdates the opening invoice
        # and its journal (F4).
        from vs_finance.constants import InvoiceSource  # Import project symbols exercised by these tests.
        from vs_finance.models import Customer, Invoice  # Import project symbols exercised by these tests.

        entity, _, _ = self.build_books()  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "OPENC", "name": "Backdated Co", "opening_balance": 5000000,  # Continue structured test data.
             "opening_date": "2026-03-15"}, format="json")  # Assign test setup data.
        self.assertEqual(resp.status_code, 201, resp.content)  # Check the expected test outcome.
        cust = Customer.objects.get(entity=entity, code="OPENC")  # Fetch test database data.
        inv = Invoice.objects.get(entity=entity, customer=cust, source=InvoiceSource.OPENING)  # Fetch test database data.
        self.assertEqual(inv.invoice_date, datetime.date(2026, 3, 15))  # Check the expected test outcome.
        self.assertEqual(inv.journal.date, datetime.date(2026, 3, 15))  # Check the expected test outcome.

    def test_customer_opening_balance_credits_equity_not_revenue(self):  # Define a test helper or test method.
        # Regression: an opening balance is prior-period value, so it must credit
        # equity (Retained Earnings 3200), never current-period revenue (4100) —
        # otherwise every migrated-in customer overstates the income statement.
        from vs_finance.constants import InvoiceSource  # Import project symbols exercised by these tests.
        from vs_finance.models import Customer, Invoice  # Import project symbols exercised by these tests.

        entity, _, _ = self.build_books()  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "OPENEQ", "name": "Opening Equity Co", "opening_balance": 5000000},  # Continue structured test data.
            format="json")  # Assign test setup data.
        self.assertEqual(resp.status_code, 201, resp.content)  # Check the expected test outcome.
        cust = Customer.objects.get(entity=entity, code="OPENEQ")  # Fetch test database data.
        inv = Invoice.objects.get(entity=entity, customer=cust, source=InvoiceSource.OPENING)  # Fetch test database data.
        credit_codes = {ln.account.code for ln in inv.journal.lines.all() if ln.credit > 0}  # Assign test setup data.
        self.assertIn("3200", credit_codes)        # Retained Earnings (equity)
        self.assertNotIn("4100", credit_codes)     # not Operating Revenue (income)

    def test_employee_salary_roster_generates_a_run(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        for nm, g, p, pe in [("Ada Obi", 50000000, 7500000, 4000000),  # Iterate through test data.
                             ("Bola Lawal", 30000000, 4500000, 2400000)]:  # Start the nested test block.
            r = self.client.post(  # Exercise the test HTTP client.
                f"/v1/finance/employee-salaries/?entity={entity.code}",  # Continue structured test data.
                {"name": nm, "gross_amount": g, "paye_amount": p, "pension_amount": pe},  # Continue structured test data.
                format="json")  # Assign test setup data.
            self.assertEqual(r.status_code, 201, r.content)  # Check the expected test outcome.
        # Roster lists both, net is gross − paye − pension.
        roster = self.client.get(f"/v1/finance/employee-salaries/?entity={entity.code}").json()["data"]  # Exercise the test HTTP client.
        self.assertEqual([s["name"] for s in roster], ["Ada Obi", "Bola Lawal"])  # Check the expected test outcome.
        self.assertEqual(roster[0]["net_amount"], 50000000 - 7500000 - 4000000)  # Check the expected test outcome.

        gen = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/payroll-runs/generate/?entity={entity.code}",  # Continue structured test data.
            {"pay_date": "2026-01-25", "period_label": "Jan 2026"}, format="json")  # Assign test setup data.
        self.assertEqual(gen.status_code, 201, gen.content)  # Check the expected test outcome.
        data = gen.json()["data"]  # Assign test setup data.
        self.assertEqual(len(data["lines"]), 2)  # Check the expected test outcome.
        self.assertEqual(data["gross_total"], 80000000)  # Check the expected test outcome.
        self.assertEqual(data["net_total"], 80000000 - 12000000 - 6400000)  # Check the expected test outcome.
        self.assertEqual(data["run_status"], "DRAFT")  # Check the expected test outcome.

    def test_salary_structure_derives_paye_pension_and_net_from_gross(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        # A structure: Basic 40% of gross, Housing 30%, Transport 30% (earnings);
        # PAYE 7% of gross, Pension 8% of basic (deductions).
        struct = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/salary-structures/?entity={entity.code}",  # Continue structured test data.
            {"name": "Senior staff", "components": [  # Continue structured test data.
                {"name": "Basic", "kind": "EARNING", "calc_method": "PERCENT_OF_GROSS",  # Continue structured test data.
                 "rate_bps": 4000, "is_basic": True},  # Continue structured test data.
                {"name": "Housing", "kind": "EARNING", "calc_method": "PERCENT_OF_GROSS",  # Continue structured test data.
                 "rate_bps": 3000},  # Continue structured test data.
                {"name": "Transport", "kind": "EARNING", "calc_method": "PERCENT_OF_GROSS",  # Continue structured test data.
                 "rate_bps": 3000},  # Continue structured test data.
                {"name": "PAYE", "kind": "DEDUCTION", "calc_method": "PERCENT_OF_GROSS",  # Continue structured test data.
                 "rate_bps": 700, "statutory_type": "PAYE"},  # Continue structured test data.
                {"name": "Pension", "kind": "DEDUCTION", "calc_method": "PERCENT_OF_BASIC",  # Continue structured test data.
                 "rate_bps": 800, "statutory_type": "PENSION"},  # Continue structured test data.
            ]}, format="json")  # Assign test setup data.
        self.assertEqual(struct.status_code, 201, struct.content)  # Check the expected test outcome.
        sid = struct.json()["data"]["id"]  # Assign test setup data.

        # A deduction tagged NONE is rejected (keeps the journal balanced).
        bad = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/salary-structures/?entity={entity.code}",  # Continue structured test data.
            {"name": "Bad", "components": [  # Continue structured test data.
                {"name": "Loan", "kind": "DEDUCTION", "calc_method": "FIXED", "amount": 100},  # Continue structured test data.
            ]}, format="json")  # Assign test setup data.
        self.assertEqual(bad.status_code, 400, bad.content)  # Check the expected test outcome.

        # Assign it to an employee on a ₦500,000 gross; PAYE/pension/net are derived.
        emp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/employee-salaries/?entity={entity.code}",  # Continue structured test data.
            {"name": "Ada Obi", "gross_amount": 50000000, "structure": sid}, format="json")  # Assign test setup data.
        self.assertEqual(emp.status_code, 201, emp.content)  # Check the expected test outcome.
        row = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/employee-salaries/?entity={entity.code}").json()["data"][0]  # Assign test setup data.
        self.assertEqual(row["paye_amount"], 3500000)            # 7% of 50,000,000
        self.assertEqual(row["pension_amount"], 1600000)         # 8% of basic (20,000,000)
        self.assertEqual(row["net_amount"], 50000000 - 3500000 - 1600000)  # Check the expected test outcome.
        self.assertEqual(len(row["components"]), 5)  # Check the expected test outcome.
        self.assertEqual(row["structure_name"], "Senior staff")  # Check the expected test outcome.

        # A generated run copies the derived figures + the payslip breakdown snapshot.
        gen = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/payroll-runs/generate/?entity={entity.code}",  # Continue structured test data.
            {"pay_date": "2026-01-25", "period_label": "Jan 2026"}, format="json")  # Assign test setup data.
        self.assertEqual(gen.status_code, 201, gen.content)  # Check the expected test outcome.
        line = gen.json()["data"]["lines"][0]  # Assign test setup data.
        self.assertEqual(line["paye_amount"], 3500000)  # Check the expected test outcome.
        self.assertEqual(line["pension_amount"], 1600000)  # Check the expected test outcome.
        self.assertEqual(len(line["components"]), 5)  # Check the expected test outcome.

        # Can't delete a structure that's assigned to someone.
        rm = self.client.delete(f"/v1/finance/salary-structures/{sid}/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(rm.status_code, 400, rm.content)  # Check the expected test outcome.

    def test_budget_list_enriched_and_heatmap_endpoint(self):  # Define a test helper or test method.
        entity, year, periods = self.build_books()  # Assign test setup data.
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")  # Create test database data.
        salaries = Account.objects.get(entity=entity, code="5200")  # Fetch test database data.
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)  # Assign test setup data.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("5200", 30000, 0), ("1100", 0, 30000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 15)))  # Assign test setup data.

        # List carries headline budget/actual/consumed so the table needs no extra call.
        lst = self.client.get(f"/v1/finance/budgets/?entity={entity.code}").json()["data"]  # Exercise the test HTTP client.
        b = next(x for x in lst if x["id"] == budget.id)  # Execute the test step.
        self.assertEqual(b["budgeted_total"], 60000)  # Check the expected test outcome.
        self.assertEqual(b["actual_ytd"], 30000)  # Check the expected test outcome.
        self.assertEqual(b["consumed_pct"], 50.0)  # Check the expected test outcome.

        # Heatmap: 12 periods, the Jan cell carries budget 60,000 / actual 30,000.
        hm = self.client.get(f"/v1/finance/budgets/{budget.id}/heatmap/?entity={entity.code}").json()["data"]  # Exercise the test HTTP client.
        self.assertEqual(len(hm["periods"]), 12)  # Check the expected test outcome.
        r = next(x for x in hm["rows"] if x["code"] == "5200")  # Execute the test step.
        c1 = next(c for c in r["cells"] if c["period_no"] == 1)  # Execute the test step.
        self.assertEqual((c1["budget"], c1["actual"]), (60000, 30000))  # Check the expected test outcome.

    def test_budget_create_with_lines_autocode_and_draft_edit(self):  # Define a test helper or test method.
        entity, year, _ = self.build_books()  # Assign test setup data.
        # Create a budget WITH lines in one call; it gets an auto code.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/budgets/?entity={entity.code}",  # Continue structured test data.
            {"name": "FY26 Operating", "fiscal_year": year.year, "lines": [  # Continue structured test data.
                {"account": "5200", "period_no": 1, "amount": 60000},  # Continue structured test data.
                {"account": "5200", "period_no": 2, "amount": 60000},  # Continue structured test data.
                {"account": "5100", "period_no": 1, "amount": 20000},  # Continue structured test data.
            ]}, format="json")  # Assign test setup data.
        self.assertEqual(resp.status_code, 201, resp.content)  # Check the expected test outcome.
        b = resp.json()["data"]  # Assign test setup data.
        self.assertTrue(b["code"].startswith(f"CFX-{entity.code}-BDG-{year.year}-"))  # Check the expected test outcome.
        self.assertEqual(len(b["lines"]), 3)  # Check the expected test outcome.
        bid = b["id"]  # Assign test setup data.

        # Budgets reject non-P&L accounts (variance is against income/expense only).
        bad = self.client.put(  # Exercise the test HTTP client.
            f"/v1/finance/budgets/{bid}/lines/?entity={entity.code}",  # Continue structured test data.
            {"lines": [{"account": "1100", "period_no": 1, "amount": 5000}]}, format="json")  # Assign test setup data.
        self.assertEqual(bad.status_code, 422, bad.content)  # Check the expected test outcome.

        # PUT replaces all lines wholesale.
        rep = self.client.put(  # Exercise the test HTTP client.
            f"/v1/finance/budgets/{bid}/lines/?entity={entity.code}",  # Continue structured test data.
            {"lines": [{"account": "5200", "period_no": 1, "amount": 99000}]}, format="json")  # Assign test setup data.
        self.assertEqual(rep.status_code, 200, rep.content)  # Check the expected test outcome.
        self.assertEqual(len(rep.json()["data"]["lines"]), 1)  # Check the expected test outcome.
        line_id = rep.json()["data"]["lines"][0]["id"]  # Assign test setup data.

        # PATCH renames a draft.
        pat = self.client.patch(  # Exercise the test HTTP client.
            f"/v1/finance/budgets/{bid}/?entity={entity.code}", {"name": "FY26 Opex"}, format="json")  # Assign test setup data.
        self.assertEqual(pat.status_code, 200, pat.content)  # Check the expected test outcome.
        self.assertEqual(pat.json()["data"]["name"], "FY26 Opex")  # Check the expected test outcome.

        # DELETE one line.
        d = self.client.delete(f"/v1/finance/budgets/{bid}/lines/{line_id}/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(d.status_code, 200, d.content)  # Check the expected test outcome.
        self.assertEqual(len(d.json()["data"]["lines"]), 0)  # Check the expected test outcome.

        # Once approved, edits are refused (the lock).
        self.client.post(f"/v1/finance/budgets/{bid}/approve/?entity={entity.code}")  # Exercise the test HTTP client.
        locked = self.client.patch(  # Exercise the test HTTP client.
            f"/v1/finance/budgets/{bid}/?entity={entity.code}", {"name": "nope"}, format="json")  # Assign test setup data.
        self.assertEqual(locked.status_code, 422, locked.content)  # Check the expected test outcome.

        # Fiscal-years endpoint lists the open year for the dropdown.
        fy = self.client.get(f"/v1/finance/fiscal-years/?entity={entity.code}").json()["data"]  # Exercise the test HTTP client.
        fy = fy if isinstance(fy, list) else fy.get("results", [])  # Assign test setup data.
        self.assertTrue(any(y["year"] == year.year for y in fy))  # Check the expected test outcome.

    def test_bank_account_detail_reports_metrics_and_transactions(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        # A +50,000 cash inflow on the cash account (book balance moves).
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 15)))  # Assign test setup data.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/bank-accounts/{bank.id}/statement-lines/?entity={entity.code}",  # Continue structured test data.
            {"lines": [{"txn_date": "2026-01-16", "amount": 50000}],  # Continue structured test data.
             "period_label": "Jan 2026", "opening_balance": 0}, format="json")  # Assign test setup data.

        resp = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/bank-accounts/{bank.id}/?entity={entity.code}")  # Assign test setup data.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(data["book_balance"], 50000)  # Check the expected test outcome.
        self.assertEqual(data["metrics"]["book_balance"], 50000)  # Check the expected test outcome.
        self.assertEqual(data["metrics"]["statement_balance"], 50000)  # Check the expected test outcome.
        self.assertEqual(data["metrics"]["unreconciled_diff"], 0)  # Check the expected test outcome.
        self.assertEqual(data["metrics"]["unreconciled_count"], 1)  # Check the expected test outcome.
        # Transactions carry a running balance; the latest equals the book balance.
        self.assertEqual(data["transactions"][0]["running_balance"], 50000)  # Check the expected test outcome.
        self.assertEqual(len(data["statements"]), 1)  # Check the expected test outcome.
        self.assertEqual(data["statements"][0]["closing_balance"], 50000)  # Check the expected test outcome.

    def test_bank_book_lines_and_complete_reconciliation(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        # Two posted cash movements (the "book" side).
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 15)))  # Assign test setup data.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 16)))  # Assign test setup data.
        # Import a statement line that matches the first; reconcile it.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/bank-accounts/{bank.id}/statement-lines/?entity={entity.code}",  # Continue structured test data.
            {"lines": [{"txn_date": "2026-01-15", "amount": 50000}]}, format="json")  # Assign test setup data.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/bank-accounts/{bank.id}/auto-reconcile/?entity={entity.code}",  # Continue structured test data.
            {"tolerance_days": 5}, format="json")  # Assign test setup data.

        # Book-lines now lists only the still-unmatched ₦30,000 movement.
        book = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/bank-accounts/{bank.id}/book-lines/?entity={entity.code}")  # Assign test setup data.
        self.assertEqual(book.status_code, 200, book.content)  # Check the expected test outcome.
        rows = book.json()["data"]  # Assign test setup data.
        self.assertEqual([r["amount"] for r in rows], [30000])  # Check the expected test outcome.

        # Complete records a reconciliation snapshot.
        done = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/bank-accounts/{bank.id}/reconcile/complete/?entity={entity.code}",  # Continue structured test data.
            {}, format="json")  # Assign test setup data.
        self.assertEqual(done.status_code, 201, done.content)  # Check the expected test outcome.
        self.assertEqual(done.json()["data"]["matched_count"], 1)  # Check the expected test outcome.
        self.assertIn(done.json()["data"]["status"], ("BALANCED", "OUT_OF_BALANCE"))  # Check the expected test outcome.

    def test_unmatch_drops_pairing_and_reverses_adjustment(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        bank = self.make_bank(entity)  # Assign test setup data.
        # 1) A plain match: post a +50,000 cash line, import + auto-match it.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],  # Continue structured test data.
            date=datetime.date(2026, 1, 15)))  # Assign test setup data.
        imp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/bank-accounts/{bank.id}/statement-lines/?entity={entity.code}",  # Continue structured test data.
            {"lines": [{"txn_date": "2026-01-15", "amount": 50000}]}, format="json")  # Assign test setup data.
        line_id = imp.json()["data"]["imported"][0]["id"]  # Assign test setup data.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/bank-accounts/{bank.id}/auto-reconcile/?entity={entity.code}",  # Continue structured test data.
            {"tolerance_days": 5}, format="json")  # Assign test setup data.
        # Unmatch → back to UNMATCHED, no ledger effect.
        un = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/statement-lines/{line_id}/unmatch/?entity={entity.code}", {}, format="json")  # Assign test setup data.
        self.assertEqual(un.status_code, 200, un.content)  # Check the expected test outcome.
        self.assertEqual(un.json()["data"]["status"], "UNMATCHED")  # Check the expected test outcome.

        # 2) An adjustment: a -1,500 charge → adjust (books a journal), then unmatch reverses it.
        from vs_finance.constants import DocumentStatus  # Import project symbols exercised by these tests.
        adj_imp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/bank-accounts/{bank.id}/statement-lines/?entity={entity.code}",  # Continue structured test data.
            {"lines": [{"txn_date": "2026-01-20", "amount": -1500, "description": "Fee"}]}, format="json")  # Assign test setup data.
        adj_line = adj_imp.json()["data"]["imported"][0]["id"]  # Assign test setup data.
        adj = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/statement-lines/{adj_line}/adjust/?entity={entity.code}", {}, format="json")  # Assign test setup data.
        self.assertEqual(adj.json()["data"]["match_source"], "ADJUSTMENT")  # Check the expected test outcome.
        je_id = adj.json()["data"]["adjusting_journal_id"]  # Assign test setup data.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/statement-lines/{adj_line}/unmatch/?entity={entity.code}", {}, format="json")  # Assign test setup data.
        from vs_finance.models import JournalEntry  # Import project symbols exercised by these tests.
        self.assertTrue(JournalEntry.objects.filter(reverses_id=je_id, status=DocumentStatus.POSTED).exists())  # Check the expected test outcome.

    def test_bank_account_patch_updates_settings_and_primary(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        a = self.make_bank(entity)  # Assign test setup data.
        b = BankAccount.objects.create(  # Create test database data.
            entity=entity, name="Access Collections",  # Continue structured test data.
            gl_account=Account.objects.get(entity=entity, code="1500"), is_primary=True)  # Fetch test database data.
        # Make `a` primary → `b` is demoted (at most one primary).
        resp = self.client.patch(  # Exercise the test HTTP client.
            f"/v1/finance/bank-accounts/{a.id}/?entity={entity.code}",  # Continue structured test data.
            {"is_primary": True, "bank_name": "GTBank"}, format="json")  # Assign test setup data.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        self.assertTrue(resp.json()["data"]["is_primary"])  # Check the expected test outcome.
        self.assertEqual(resp.json()["data"]["bank_name"], "GTBank")  # Check the expected test outcome.
        b.refresh_from_db()  # Execute the test step.
        self.assertFalse(b.is_primary)  # Check the expected test outcome.

    def _seed(self):  # Define a test helper or test method.
        entity, _, periods = self.build_books()  # Assign test setup data.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 1000000, 0), ("3100", 0, 1000000)],  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1500", 400000, 0), ("1100", 0, 400000)],  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("1100", 300000, 0), ("4100", 0, 300000)],  # Continue structured test data.
        ))  # Execute the test step.
        post_journal(self.make_entry(  # Continue structured test data.
            entity, periods[0], [("5200", 120000, 0), ("1100", 0, 120000)],  # Continue structured test data.
        ))  # Execute the test step.
        return entity, periods  # Return the prepared test value.

    def test_entity_param_is_required_and_validated(self):  # Define a test helper or test method.
        entity, _ = self._seed()  # Assign test setup data.
        # Missing entity → 400.
        resp = self.client.get("/v1/finance/reports/trial-balance/")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 400)  # Check the expected test outcome.
        # Unknown entity → 404.
        resp = self.client.get("/v1/finance/reports/trial-balance/?entity=NOPE")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 404)  # Check the expected test outcome.
        # Known entity (by code) → 200.
        resp = self.client.get(f"/v1/finance/reports/trial-balance/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        self.assertTrue(resp.json()["data"]["is_balanced"])  # Check the expected test outcome.

    def test_entities_and_accounts_endpoints(self):  # Define a test helper or test method.
        entity, _ = self._seed()  # Assign test setup data.
        resp = self.client.get("/v1/finance/entities/")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        codes = {e["code"] for e in resp.json()["data"]}  # Assign test setup data.
        self.assertIn(entity.code, codes)  # Check the expected test outcome.

        resp = self.client.get(f"/v1/finance/accounts/?entity={entity.code}&account_type=ASSET")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        types = {a["account_type"] for a in resp.json()["data"]}  # Assign test setup data.
        self.assertEqual(types, {"ASSET"})  # Check the expected test outcome.

    def test_chart_with_balance_and_create_account(self):  # Define a test helper or test method.
        entity, _ = self._seed()  # Assign test setup data.
        # ?with_balance returns the full tree with balance + tag + subtype fields.
        resp = self.client.get(f"/v1/finance/accounts/?entity={entity.code}&with_balance=true")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        rows = resp.json()["data"]  # Assign test setup data.
        cash = next(r for r in rows if r["code"] == "1100")  # Execute the test step.
        self.assertEqual(cash["tag"], "CASH")  # Check the expected test outcome.
        self.assertIsNotNone(cash["balance"])  # Check the expected test outcome.
        self.assertIn("subtype", cash)  # Check the expected test outcome.

        # Create a new account with a subtype; normal balance is derived for INCOME.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/accounts/?entity={entity.code}",  # Continue structured test data.
            {"code": "4150", "name": "Boarding Fees", "account_type": "INCOME",  # Continue structured test data.
             "subtype": "Operating revenue"},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 201)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(data["code"], "4150")  # Check the expected test outcome.
        self.assertEqual(data["subtype"], "Operating revenue")  # Check the expected test outcome.
        self.assertEqual(data["normal_balance"], "CREDIT")  # Check the expected test outcome.

        # Duplicate code is rejected.
        dup = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/accounts/?entity={entity.code}",  # Continue structured test data.
            {"code": "4150", "name": "dup", "account_type": "INCOME"}, format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(dup.status_code, 400)  # Check the expected test outcome.

    def test_account_detail_ledger_and_update(self):  # Define a test helper or test method.
        entity, _ = self._seed()  # Assign test setup data.
        from vs_finance.models import Account  # Import project symbols exercised by these tests.
        cash = Account.objects.get(entity=entity, code="1100")  # Fetch test database data.

        # Detail: summary + per-account posted activity (the _seed posts hit 1100).
        resp = self.client.get(f"/v1/finance/accounts/{cash.pk}/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        d = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(d["account"]["code"], "1100")  # Check the expected test outcome.
        self.assertTrue(d["summary"]["line_count"] > 0)  # Check the expected test outcome.
        self.assertTrue(d["activity"])  # Check the expected test outcome.
        self.assertIn("running_balance", d["activity"][0])  # Check the expected test outcome.

        # Update is gated on finance.account.update; safe fields only.
        patch = self.client.patch(  # Exercise the test HTTP client.
            f"/v1/finance/accounts/{cash.pk}/?entity={entity.code}",  # Continue structured test data.
            {"subtype": "Cash and cash equivalents", "name": "Cash & Bank (main)"}, format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(patch.status_code, 200)  # Check the expected test outcome.
        self.assertEqual(patch.json()["data"]["subtype"], "Cash and cash equivalents")  # Check the expected test outcome.
        cash.refresh_from_db()  # Execute the test step.
        self.assertEqual(cash.name, "Cash & Bank (main)")  # Check the expected test outcome.

    def test_direct_entry_endpoint_posts_capital_journal(self):  # Define a test helper or test method.
        # The honest way capital/equity enters: a posted journal, not magic.
        entity, _, _ = self.build_books()  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/direct-entries/?entity={entity.code}",  # Continue structured test data.
            {"narration": "Capital injection",  # Continue structured test data.
             "lines": [{"account": "1100", "debit": 5000000000},   # Dr Cash ₦50,000,000
                       {"account": "3100", "credit": 5000000000}]},  # Cr Share Capital
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 201, resp.content)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(data["source"], "OPENING")  # Check the expected test outcome.
        self.assertEqual(data["status"], "POSTED")  # Check the expected test outcome.
        self.assertEqual(data["total_debit"], 5000000000)  # Check the expected test outcome.
        self.assertEqual(data["total_credit"], 5000000000)  # Check the expected test outcome.

        # It is a real journal: it shows in the read-only journals list…
        journals = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/journals/?entity={entity.code}").json()["data"]  # Assign test setup data.
        self.assertIn(data["document_number"], {j["document_number"] for j in journals})  # Check the expected test outcome.
        # …and it moved the trial balance (which still balances).
        tb = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/reports/trial-balance/?entity={entity.code}").json()["data"]  # Assign test setup data.
        self.assertTrue(tb["is_balanced"])  # Check the expected test outcome.

    def test_direct_entry_rejects_unbalanced(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/direct-entries/?entity={entity.code}",  # Continue structured test data.
            {"lines": [{"account": "1100", "debit": 5000000000},  # Continue structured test data.
                       {"account": "3100", "credit": 4000000000}]},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 400, resp.content)  # Check the expected test outcome.

    def test_direct_entry_carries_cost_centre_to_gl(self):  # Define a test helper or test method.
        from .models import CostCenter  # Import project symbols exercised by these tests.

        entity, _, _ = self.build_books()  # Assign test setup data.
        CostCenter.objects.create(entity=entity, code="PRI", name="Primary")  # Create test database data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/direct-entries/?entity={entity.code}",  # Continue structured test data.
            {"narration": "Dept adjustment",  # Continue structured test data.
             "lines": [{"account": "5300", "debit": 100000, "cost_center": "PRI"},  # Continue structured test data.
                       {"account": "1100", "credit": 100000}]},  # cash leg unallocated
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 201, resp.content)  # Check the expected test outcome.
        by_acc = {ln["account_code"]: ln["cost_center"] for ln in resp.json()["data"]["lines"]}  # Assign test setup data.
        self.assertEqual(by_acc["5300"], "PRI")  # Check the expected test outcome.
        self.assertIsNone(by_acc["1100"])  # Check the expected test outcome.

    def test_direct_entry_rejects_unknown_cost_centre(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/direct-entries/?entity={entity.code}",  # Continue structured test data.
            {"lines": [{"account": "5300", "debit": 100000, "cost_center": "NOPE"},  # Continue structured test data.
                       {"account": "1100", "credit": 100000}]},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 400, resp.content)  # Check the expected test outcome.

    def test_customer_opening_balance_posts_opening_invoice(self):  # Define a test helper or test method.
        from .models import Invoice  # Import project symbols exercised by these tests.

        entity, _, _ = self.build_books()  # Assign test setup data.
        created = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "OPN1", "name": "Opening Co", "opening_balance": 500000},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(created.status_code, 201, created.content)  # Check the expected test outcome.
        # An opening invoice (Dr 1200 AR / Cr 3200 Retained Earnings) was raised —
        # opening balances credit equity, not current-period revenue.
        inv = Invoice.objects.get(entity=entity, source="OPENING", customer__code="OPN1")  # Fetch test database data.
        self.assertEqual(inv.status, "POSTED")  # Check the expected test outcome.
        self.assertEqual(inv.total, 500000)  # Check the expected test outcome.
        gl = {ln.account.code: (ln.debit, ln.credit) for ln in inv.journal.lines.all()}  # Assign test setup data.
        self.assertEqual(gl["1200"], (500000, 0))  # Check the expected test outcome.
        self.assertEqual(gl["3200"], (0, 500000))  # Check the expected test outcome.
        self.assertNotIn("4100", gl)  # Check the expected test outcome.
        # …and it surfaces in the customer's outstanding, now a paginated list.
        listed = self.client.get(f"/v1/finance/customers/?entity={entity.code}").json()  # Exercise the test HTTP client.
        self.assertIn("pagination", listed)  # Check the expected test outcome.
        row = next(r for r in listed["data"] if r["code"] == "OPN1")  # Execute the test step.
        self.assertEqual(row["balance"], 500000)  # Check the expected test outcome.

    def test_customer_summary_and_status_filter(self):  # Define a test helper or test method.
        """The summary aggregates over ALL customers (accurate while the list paginates),
        and the list's derived-status filter narrows server-side to the matching rows."""
        entity, _, _ = self.build_books()  # Assign test setup data.
        # An opening balance makes this customer OVERDUE-or-ACTIVE with a receivable.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "SUMA", "name": "Owes Money", "opening_balance": 300000}, format="json")  # Assign test setup data.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "SUMB", "name": "Flat Co", "is_active": False}, format="json")  # INACTIVE

        summ = self.client.get(f"/v1/finance/customers/summary/?entity={entity.code}").json()["data"]  # Exercise the test HTTP client.
        self.assertEqual(summ["total"], 2)  # Check the expected test outcome.
        self.assertEqual(summ["receivable"]["kobo"], 300000)  # SUMA owes; due today, so ACTIVE
        self.assertEqual(summ["status_counts"]["ACTIVE"], 1)  # Check the expected test outcome.
        self.assertEqual(summ["status_counts"]["INACTIVE"], 1)  # Check the expected test outcome.
        self.assertEqual(sum(summ["status_counts"].values()), 2)  # Check the expected test outcome.

        active = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}&status=ACTIVE").json()  # Assign test setup data.
        self.assertIn("pagination", active)  # Check the expected test outcome.
        self.assertEqual([r["code"] for r in active["data"]], ["SUMA"])  # Check the expected test outcome.

    def test_payment_summary_totals_and_counts(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        c = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "PSUM", "name": "Payer"}, format="json").json()["data"]  # Assign test setup data.
        # A receipt with no invoices → fully unallocated.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/{c['code']}/receipt/?entity={entity.code}",  # Continue structured test data.
            {"amount": 90000, "payment_date": "2026-01-15", "deposit_account": "1100",  # Continue structured test data.
             "auto_allocate": False}, format="json")  # Assign test setup data.
        summ = self.client.get(f"/v1/finance/payments/summary/?entity={entity.code}").json()["data"]  # Exercise the test HTTP client.
        self.assertEqual(summ["count"], 1)  # Check the expected test outcome.
        self.assertEqual(summ["unallocated"]["kobo"], 90000)  # Check the expected test outcome.
        self.assertEqual(summ["status_counts"]["UNALLOCATED"], 1)  # Check the expected test outcome.

    def test_receipt_largest_first_allocation(self):  # Define a test helper or test method.
        from .models import Invoice  # Import project symbols exercised by these tests.

        entity, _, _ = self.build_books()  # Assign test setup data.
        c = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "ALC", "name": "Alloc Co"}, format="json").json()["data"]  # Assign test setup data.

        def mk_invoice(price, date):  # Define a test helper or test method.
            return self.client.post(  # Return the prepared test value.
                f"/v1/finance/invoices/?entity={entity.code}",  # Continue structured test data.
                {"customer": "ALC", "invoice_date": date,  # Continue structured test data.
                 "lines": [{"revenue_account": "4100", "quantity": 1, "unit_price": price}]},  # Continue structured test data.
                format="json").json()["data"]  # Assign test setup data.

        small = mk_invoice(100000, "2026-01-05")   # older, smaller
        large = mk_invoice(300000, "2026-02-05")   # newer, larger
        # Receipt of exactly the large balance, largest-first → clears LARGE, leaves small.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/{c['id']}/receipt/?entity={entity.code}",  # Continue structured test data.
            {"amount": 300000, "payment_date": "2026-03-01", "deposit_account": "1100",  # Continue structured test data.
             "allocation_strategy": "largest"}, format="json")  # Assign test setup data.
        self.assertEqual(Invoice.objects.get(id=large["id"]).payment_status, "PAID")  # Check the expected test outcome.
        self.assertEqual(Invoice.objects.get(id=small["id"]).payment_status, "UNPAID")  # Check the expected test outcome.

    def test_receipt_rejects_unknown_allocation_strategy(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        c = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "BAD", "name": "Bad Co"}, format="json").json()["data"]  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/{c['id']}/receipt/?entity={entity.code}",  # Continue structured test data.
            {"amount": 100000, "payment_date": "2026-03-01", "deposit_account": "1100",  # Continue structured test data.
             "allocation_strategy": "fifo"}, format="json")  # Assign test setup data.
        self.assertEqual(resp.status_code, 400, resp.content)  # Check the expected test outcome.

    def test_entity_create_provisions_new_books(self):  # Define a test helper or test method.
        # Seed first so the NGN currency exists for the default base_currency.
        self._seed()  # Execute the test step.
        resp = self.client.post(  # Exercise the test HTTP client.
            "/v1/finance/entities/",  # Continue structured test data.
            {"code": "crest", "name": "Crestfield Academy", "kind": "TENANT"},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 201, resp.content)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(data["code"], "CREST")          # normalised to uppercase
        self.assertEqual(data["base_currency"], "NGN")   # model default
        self.assertTrue(data["is_active"])  # Check the expected test outcome.

        # And it now shows up in the list endpoint.
        listed = self.client.get("/v1/finance/entities/")  # Exercise the test HTTP client.
        self.assertIn("CREST", {e["code"] for e in listed.json()["data"]})  # Check the expected test outcome.

        # The one POST provisions a fully usable set of books: chart of accounts
        # and twelve open periods, so no CLI seed_finance step is needed.
        accounts = self.client.get("/v1/finance/accounts/?entity=CREST").json()["data"]  # Exercise the test HTTP client.
        codes = {a["code"] for a in accounts}  # Assign test setup data.
        self.assertTrue({"1100", "1200", "3100"}.issubset(codes))  # cash, AR, share capital

        periods = self.client.get("/v1/finance/periods/?entity=CREST").json()["data"]  # Exercise the test HTTP client.
        self.assertEqual(len(periods), 12)  # Check the expected test outcome.
        self.assertTrue(all(p["status"] == "OPEN" for p in periods))  # Check the expected test outcome.

    def test_customer_crud_and_invoice_filter(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        # Create — receivable account defaults to 1200.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "cust1", "name": "Acme Ltd", "billing_email": "a@acme.test"},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 201, resp.content)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(data["code"], "CUST1")               # normalised
        self.assertEqual(data["receivable_account_code"], "1200")  # Check the expected test outcome.

        # Duplicate code rejected.
        dup = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "CUST1", "name": "Dupe"}, format="json")  # Assign test setup data.
        self.assertEqual(dup.status_code, 400)  # Check the expected test outcome.

        # List + search.
        listed = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}&search=acme").json()["data"]  # Assign test setup data.
        self.assertEqual({c["code"] for c in listed}, {"CUST1"})  # Check the expected test outcome.

        # Detail by code + PATCH.
        det = self.client.get(f"/v1/finance/customers/CUST1/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(det.status_code, 200)  # Check the expected test outcome.
        patched = self.client.patch(  # Exercise the test HTTP client.
            f"/v1/finance/customers/CUST1/?entity={entity.code}",  # Continue structured test data.
            {"name": "Acme Renamed"}, format="json")  # Assign test setup data.
        self.assertEqual(patched.json()["data"]["name"], "Acme Renamed")  # Check the expected test outcome.

    def test_fee_structure_generates_posted_invoices(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "stu1", "name": "Student One"}, format="json")  # Assign test setup data.

        # A fee structure with one ₦100,000 tuition line.
        created = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/?entity={entity.code}",  # Continue structured test data.
            {"code": "jss1t1", "name": "JSS1 Term 1",  # Continue structured test data.
             "items": [{"description": "Tuition", "revenue_account": "4100", "amount": 10000000}]},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(created.status_code, 201, created.content)  # Check the expected test outcome.
        self.assertEqual(created.json()["data"]["total"], 10000000)  # Check the expected test outcome.

        # Generate → one posted invoice for the customer.
        gen = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/JSS1T1/generate/?entity={entity.code}",  # Continue structured test data.
            {"customers": ["STU1"], "invoice_date": "2026-01-10"}, format="json")  # Assign test setup data.
        self.assertEqual(gen.status_code, 201, gen.content)  # Check the expected test outcome.
        gdata = gen.json()["data"]  # Assign test setup data.
        self.assertEqual(gdata["generated"], 1)  # Check the expected test outcome.
        self.assertEqual(gdata["invoices"][0]["status"], "POSTED")  # Check the expected test outcome.
        self.assertEqual(gdata["invoices"][0]["total"], 10000000)  # Check the expected test outcome.

        # It shows under the customer filter on the invoices list…
        inv = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/invoices/?entity={entity.code}&customer=STU1").json()["data"]  # Assign test setup data.
        self.assertEqual(len(inv), 1)  # Check the expected test outcome.
        # …and the trial balance still balances (AR raised).
        tb = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/reports/trial-balance/?entity={entity.code}").json()["data"]  # Assign test setup data.
        self.assertTrue(tb["is_balanced"])  # Check the expected test outcome.

        # Re-running is idempotent — no second invoice for the same customer/structure.
        again = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/JSS1T1/generate/?entity={entity.code}",  # Continue structured test data.
            {"customers": ["STU1"]}, format="json")  # Assign test setup data.
        self.assertEqual(again.json()["data"]["generated"], 0)  # Check the expected test outcome.

    def test_fee_structure_applies_to_defaults_filters_and_edits(self):  # Define a test helper or test method.
        """`applies_to` defaults to CUSTOMER, is filterable, and PATCHable."""
        entity, _, _ = self.build_books()  # Assign test setup data.

        # Default when omitted = CUSTOMER.
        cust = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/?entity={entity.code}",  # Continue structured test data.
            {"code": "fs-cust", "name": "Client billing",  # Continue structured test data.
             "items": [{"description": "Tuition", "revenue_account": "4100", "amount": 5000000}]},  # Continue structured test data.
            format="json")  # Assign test setup data.
        self.assertEqual(cust.status_code, 201, cust.content)  # Check the expected test outcome.
        self.assertEqual(cust.json()["data"]["applies_to"], "CUSTOMER")  # Check the expected test outcome.
        self.assertEqual(cust.json()["data"]["applies_to_display"], "Customer")  # Check the expected test outcome.

        # Explicit non-customer type is accepted and case-insensitive.
        vend = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/?entity={entity.code}",  # Continue structured test data.
            {"code": "fs-vend", "name": "Vendor charges", "applies_to": "vendor",  # Continue structured test data.
             "items": [{"description": "Service", "revenue_account": "4100", "amount": 3000000}]},  # Continue structured test data.
            format="json")  # Assign test setup data.
        self.assertEqual(vend.status_code, 201, vend.content)  # Check the expected test outcome.
        self.assertEqual(vend.json()["data"]["applies_to"], "VENDOR")  # Check the expected test outcome.

        # A bogus value is rejected.
        bad = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/?entity={entity.code}",  # Continue structured test data.
            {"code": "fs-bad", "name": "x", "applies_to": "PARTNER",  # Continue structured test data.
             "items": [{"description": "x", "revenue_account": "4100", "amount": 100}]},  # Continue structured test data.
            format="json")  # Assign test setup data.
        self.assertEqual(bad.status_code, 400, bad.content)  # Check the expected test outcome.

        # ?applies_to= filters the list.
        only_vend = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/?entity={entity.code}&applies_to=VENDOR").json()["data"]  # Assign test setup data.
        self.assertEqual([s["code"] for s in only_vend], ["FS-VEND"])  # Check the expected test outcome.

        # PATCH can re-classify a structure.
        patched = self.client.patch(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/FS-CUST/?entity={entity.code}",  # Continue structured test data.
            {"applies_to": "STAFF"}, format="json")  # Assign test setup data.
        self.assertEqual(patched.status_code, 200, patched.content)  # Check the expected test outcome.
        self.assertEqual(patched.json()["data"]["applies_to"], "STAFF")  # Check the expected test outcome.

    def test_fee_structure_lines_carry_code_optional_and_tax_breakdown(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        vat = TaxCode.objects.create(  # Create test database data.
            entity=entity, code="VAT", name="VAT 7.5%", rate_bps=750,  # Continue structured test data.
            collected_account=Account.objects.get(entity=entity, code="2200"))  # Fetch test database data.

        created = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/?entity={entity.code}",  # Continue structured test data.
            {"code": "fs-rich", "name": "Rich structure", "items": [  # Continue structured test data.
                {"code": "TUITION", "description": "Tuition", "revenue_account": "4100",  # Continue structured test data.
                 "amount": 10000000},  # Continue structured test data.
                {"code": "TRANSPORT", "description": "Transport", "revenue_account": "4100",  # Continue structured test data.
                 "amount": 2000000, "tax_code": "VAT", "is_optional": True},  # Continue structured test data.
            ]}, format="json")  # Assign test setup data.
        self.assertEqual(created.status_code, 201, created.content)  # Check the expected test outcome.
        data = created.json()["data"]  # Assign test setup data.
        # Subtotal (net) + tax (7.5% on the ₦20,000 transport line only) = gross.
        self.assertEqual(data["total"], 12000000)  # Check the expected test outcome.
        self.assertEqual(data["tax_total"], 150000)            # 2,000,000 × 750 / 10000
        self.assertEqual(data["total_with_tax"], 12150000)  # Check the expected test outcome.
        items = {it["code"]: it for it in data["items"]}  # Assign test setup data.
        self.assertFalse(items["TUITION"]["is_optional"])  # Check the expected test outcome.
        self.assertTrue(items["TRANSPORT"]["is_optional"])  # Check the expected test outcome.
        self.assertEqual(items["TRANSPORT"]["tax_code_value"], "VAT")  # Check the expected test outcome.

    def test_fee_structure_detail_reports_usage_and_can_be_duplicated(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "stu1", "name": "Student One"}, format="json")  # Assign test setup data.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/?entity={entity.code}",  # Continue structured test data.
            {"code": "fs-src", "name": "Source", "items": [  # Continue structured test data.
                {"code": "TUITION", "description": "Tuition", "revenue_account": "4100",  # Continue structured test data.
                 "amount": 5000000, "is_optional": False}]}, format="json")  # Assign test setup data.
        # Generate one invoice → usage count should reflect it.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/FS-SRC/generate/?entity={entity.code}",  # Continue structured test data.
            {"all_active": True, "invoice_date": "2026-01-10"}, format="json")  # Assign test setup data.

        detail = self.client.get(f"/v1/finance/fee-structures/FS-SRC/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(detail.status_code, 200)  # Check the expected test outcome.
        usage = detail.json()["data"]["usage"]  # Assign test setup data.
        self.assertEqual(usage["invoices_generated"], 1)  # Check the expected test outcome.
        self.assertIsNotNone(usage["last_generated_at"])  # Check the expected test outcome.

        # Duplicate → a new INACTIVE clone carrying the same lines (incl. fee code).
        dup = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/FS-SRC/duplicate/?entity={entity.code}",  # Continue structured test data.
            {"code": "fs-copy", "name": "Copy"}, format="json")  # Assign test setup data.
        self.assertEqual(dup.status_code, 201, dup.content)  # Check the expected test outcome.
        clone = dup.json()["data"]  # Assign test setup data.
        self.assertEqual(clone["code"], "FS-COPY")  # Check the expected test outcome.
        self.assertFalse(clone["is_active"])  # Check the expected test outcome.
        self.assertEqual(clone["items"][0]["code"], "TUITION")  # Check the expected test outcome.
        self.assertEqual(clone["usage"]["invoices_generated"], 0)  # Check the expected test outcome.
        # Duplicating onto an existing code is rejected.
        clash = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/FS-SRC/duplicate/?entity={entity.code}",  # Continue structured test data.
            {"code": "fs-copy"}, format="json")  # Assign test setup data.
        self.assertEqual(clash.status_code, 400, clash.content)  # Check the expected test outcome.

    def test_fee_structure_generate_blocked_for_non_customer(self):  # Define a test helper or test method.
        """Only CUSTOMER structures can raise AR invoices."""
        entity, _, _ = self.build_books()  # Assign test setup data.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/customers/?entity={entity.code}",  # Continue structured test data.
            {"code": "stu1", "name": "Student One"}, format="json")  # Assign test setup data.
        self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/?entity={entity.code}",  # Continue structured test data.
            {"code": "fs-staff", "name": "Staff deductions", "applies_to": "STAFF",  # Continue structured test data.
             "items": [{"description": "Levy", "revenue_account": "4100", "amount": 100000}]},  # Continue structured test data.
            format="json")  # Assign test setup data.
        gen = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/fee-structures/FS-STAFF/generate/?entity={entity.code}",  # Continue structured test data.
            {"all_active": True}, format="json")  # Assign test setup data.
        self.assertEqual(gen.status_code, 400, gen.content)  # Check the expected test outcome.

    def test_entity_create_accepts_explicit_fiscal_year(self):  # Define a test helper or test method.
        self._seed()  # Execute the test step.
        resp = self.client.post(  # Exercise the test HTTP client.
            "/v1/finance/entities/",  # Continue structured test data.
            {"code": "LEKKI", "name": "Lekki Books", "fiscal_year": 2027},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 201, resp.content)  # Check the expected test outcome.
        periods = self.client.get("/v1/finance/periods/?entity=LEKKI").json()["data"]  # Exercise the test HTTP client.
        self.assertEqual(len(periods), 12)  # Check the expected test outcome.
        self.assertTrue(all(p["name"].startswith("2027-") for p in periods))  # Check the expected test outcome.

    def test_entity_create_supports_school_year_start_month(self):  # Define a test helper or test method.
        # A school running Sept 2026 → Aug 2027: twelve periods roll over the
        # calendar boundary, labelled by the actual calendar month.
        self._seed()  # Execute the test step.
        resp = self.client.post(  # Exercise the test HTTP client.
            "/v1/finance/entities/",  # Continue structured test data.
            {"code": "STMARY", "name": "St Mary's", "fiscal_year": 2026,  # Continue structured test data.
             "fiscal_start_month": 9},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 201, resp.content)  # Check the expected test outcome.
        periods = self.client.get("/v1/finance/periods/?entity=STMARY").json()["data"]  # Exercise the test HTTP client.
        names = [p["name"] for p in periods]  # Assign test setup data.
        self.assertEqual(len(names), 12)  # Check the expected test outcome.
        self.assertEqual(names[0], "2026-09")    # first period: September 2026
        self.assertEqual(names[-1], "2027-08")   # last period: August 2027
        self.assertEqual(periods[0]["start_date"], "2026-09-01")  # Check the expected test outcome.
        self.assertEqual(periods[-1]["end_date"], "2027-08-31")  # Check the expected test outcome.
        self.assertTrue(all(p["status"] == "OPEN" for p in periods))  # Check the expected test outcome.

    def test_entity_create_rejects_duplicate_code(self):  # Define a test helper or test method.
        self._seed()  # Execute the test step.
        first = self.client.post(  # Exercise the test HTTP client.
            "/v1/finance/entities/", {"code": "CREST", "name": "Crestfield"}, format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(first.status_code, 201, first.content)  # Check the expected test outcome.
        dupe = self.client.post(  # Exercise the test HTTP client.
            "/v1/finance/entities/", {"code": "crest", "name": "Crestfield Dup"}, format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(dupe.status_code, 400)  # Check the expected test outcome.

    def test_statement_endpoints_match_service_output(self):  # Define a test helper or test method.
        entity, _ = self._seed()  # Assign test setup data.
        ec = entity.code  # Assign test setup data.

        pnl = self.client.get(f"/v1/finance/reports/income-statement/?entity={ec}").json()["data"]  # Exercise the test HTTP client.
        self.assertEqual(pnl["totals"]["net"]["amount"]["kobo"], 180000)  # Check the expected test outcome.

        bs = self.client.get(f"/v1/finance/reports/balance-sheet/?entity={ec}").json()["data"]  # Exercise the test HTTP client.
        self.assertTrue(bs["is_balanced"])  # Check the expected test outcome.
        self.assertEqual(bs["total_assets"]["kobo"], 1180000)  # Check the expected test outcome.
        self.assertEqual(bs["retained_earnings"]["kobo"], 180000)  # Check the expected test outcome.

        cf = self.client.get(f"/v1/finance/reports/cash-flow/?entity={ec}").json()["data"]  # Exercise the test HTTP client.
        self.assertTrue(cf["is_reconciled"])  # Check the expected test outcome.
        self.assertEqual(cf["closing_cash"]["kobo"], 780000)  # Check the expected test outcome.
        self.assertEqual(cf["by_activity"]["financing"]["kobo"], 1000000)  # Check the expected test outcome.

        soce = self.client.get(f"/v1/finance/reports/changes-in-equity/?entity={ec}").json()["data"]  # Exercise the test HTTP client.
        self.assertTrue(soce["is_reconciled"])  # Check the expected test outcome.
        self.assertEqual(soce["total_closing"]["kobo"], 1180000)  # Check the expected test outcome.
        re = next(c for c in soce["columns"] if c["key"] == "retained_earnings")  # Execute the test step.
        self.assertEqual(re["profit"]["kobo"], 180000)  # Check the expected test outcome.

        pack = self.client.get(f"/v1/finance/reports/statutory-pack/?entity={ec}").json()["data"]  # Exercise the test HTTP client.
        sofp = pack["statement_of_financial_position"]  # Assign test setup data.
        self.assertTrue(sofp["is_balanced"])  # Check the expected test outcome.
        self.assertEqual(sofp["total_assets"]["kobo"], 1180000)  # Check the expected test outcome.
        self.assertEqual(sofp["total_equity"]["kobo"], 1180000)  # Check the expected test outcome.
        self.assertEqual(pack["income_statement"]["net_income"]["kobo"], 180000)  # Check the expected test outcome.
        self.assertTrue(pack["cash_flow"]["is_reconciled"])  # Check the expected test outcome.
        self.assertTrue(pack["trial_balance"]["is_balanced"])  # Check the expected test outcome.

    def test_journal_list_detail_and_post_action(self):  # Define a test helper or test method.
        entity, periods = self._seed()  # Assign test setup data.
        ec = entity.code  # Assign test setup data.

        # A fresh DRAFT journal posted through the API.
        draft = self.make_entry(entity, periods[0], [("1100", 5000, 0), ("4100", 0, 5000)])  # Assign test setup data.
        self.assertEqual(draft.status, DocumentStatus.DRAFT)  # Check the expected test outcome.
        resp = self.client.post(f"/v1/finance/journals/{draft.id}/post/?entity={ec}")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        draft.refresh_from_db()  # Execute the test step.
        self.assertEqual(draft.status, DocumentStatus.POSTED)  # Check the expected test outcome.

        # Detail view returns the lines and balanced totals.
        resp = self.client.get(f"/v1/finance/journals/{draft.id}/?entity={ec}")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(data["total_debit"], data["total_credit"])  # Check the expected test outcome.
        self.assertEqual(len(data["lines"]), 2)  # Check the expected test outcome.

        # List is scoped to the entity.
        resp = self.client.get(f"/v1/finance/journals/?entity={ec}&status=POSTED")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        self.assertGreaterEqual(resp.json()["pagination"]["totalItems"], 5)  # Check the expected test outcome.

    def test_unbalanced_post_returns_typed_error_envelope(self):  # Define a test helper or test method.
        entity, periods = self._seed()  # Assign test setup data.
        ec = entity.code  # Assign test setup data.
        bad = JournalEntry.objects.create(  # Create test database data.
            entity=entity, date=datetime.date(2026, 1, 15), period=periods[0],  # Continue structured test data.
        )  # Close the grouped test expression.
        JournalLine.objects.create(entry=bad, account=Account.objects.get(entity=entity, code="1100"),  # Create test database data.
                                    debit=5000, credit=0, line_no=1)  # Assign test setup data.
        JournalLine.objects.create(entry=bad, account=Account.objects.get(entity=entity, code="4100"),  # Create test database data.
                                    debit=0, credit=4000, line_no=2)  # Assign test setup data.
        resp = self.client.post(f"/v1/finance/journals/{bad.id}/post/?entity={ec}")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 422)  # Check the expected test outcome.
        body = resp.json()  # Assign test setup data.
        self.assertFalse(body["success"])  # Check the expected test outcome.
        self.assertEqual(body["error"]["code"], "JOURNAL_UNBALANCED")  # Check the expected test outcome.

    def test_period_close_action_runs_checklist(self):  # Define a test helper or test method.
        entity, periods = self._seed()  # Assign test setup data.
        ec = entity.code  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/periods/{periods[0].id}/close/?entity={ec}",  # Continue structured test data.
            data={"soft": False}, format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(data["period"]["status"], PeriodStatus.CLOSED)  # Check the expected test outcome.
        self.assertTrue(data["checklist"]["passed"])  # Check the expected test outcome.

    def test_period_reopen_and_lock_actions(self):  # Define a test helper or test method.
        entity, periods = self._seed()  # Assign test setup data.
        ec = entity.code  # Assign test setup data.
        pid = periods[0].id  # Assign test setup data.
        closed = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/periods/{pid}/close/?entity={ec}", data={}, format="json")  # Assign test setup data.
        self.assertEqual(closed.status_code, 200, closed.content)  # Check the expected test outcome.
        # Re-open the closed period back to OPEN.
        reopened = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/periods/{pid}/reopen/?entity={ec}", data={}, format="json")  # Assign test setup data.
        self.assertEqual(reopened.status_code, 200, reopened.content)  # Check the expected test outcome.
        self.assertEqual(reopened.json()["data"]["status"], PeriodStatus.OPEN)  # Check the expected test outcome.
        # Close again, then lock it (permanently sealed).
        self.client.post(f"/v1/finance/periods/{pid}/close/?entity={ec}", data={}, format="json")  # Exercise the test HTTP client.
        locked = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/periods/{pid}/lock/?entity={ec}", data={}, format="json")  # Assign test setup data.
        self.assertEqual(locked.status_code, 200, locked.content)  # Check the expected test outcome.
        self.assertEqual(locked.json()["data"]["status"], PeriodStatus.LOCKED)  # Check the expected test outcome.

    def test_trial_balance_exports_in_each_format(self):  # Define a test helper or test method.
        entity, _ = self._seed()  # Assign test setup data.
        ec = entity.code  # Assign test setup data.
        cases = {  # Continue structured test data.
            "csv": "text/csv",  # Continue structured test data.
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # Continue structured test data.
            "pdf": "application/pdf",  # Continue structured test data.
        }  # Close the grouped test expression.
        for fmt, ctype in cases.items():  # Iterate through test data.
            resp = self.client.get(f"/v1/finance/reports/trial-balance/?entity={ec}&export={fmt}")  # Exercise the test HTTP client.
            self.assertEqual(resp.status_code, 200, fmt)  # Check the expected test outcome.
            self.assertEqual(resp["Content-Type"], ctype)  # Check the expected test outcome.
            self.assertIn(f"trial_balance_{ec}.{fmt}", resp["Content-Disposition"])  # Check the expected test outcome.
            self.assertTrue(resp.content)  # Check the expected test outcome.
        # CSV body actually contains the data.
        csv_resp = self.client.get(f"/v1/finance/reports/trial-balance/?entity={ec}&export=csv")  # Exercise the test HTTP client.
        text = csv_resp.content.decode("utf-8")  # Assign test setup data.
        self.assertIn("Trial Balance", text)  # Check the expected test outcome.
        self.assertIn("TOTAL", text)  # Check the expected test outcome.

    def test_statement_exports_available(self):  # Define a test helper or test method.
        entity, _ = self._seed()  # Assign test setup data.
        ec = entity.code  # Assign test setup data.
        for path in ("income-statement", "balance-sheet", "changes-in-equity",  # Iterate through test data.
                     "statutory-pack", "ar-aging"):  # Start the nested test block.
            resp = self.client.get(f"/v1/finance/reports/{path}/?entity={ec}&export=xlsx")  # Exercise the test HTTP client.
            self.assertEqual(resp.status_code, 200, path)  # Check the expected test outcome.
            self.assertTrue(resp.content)  # Check the expected test outcome.

    def test_unknown_export_format_is_rejected(self):  # Define a test helper or test method.
        entity, _ = self._seed()  # Assign test setup data.
        resp = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/reports/trial-balance/?entity={entity.code}&export=docx"  # Assign test setup data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 400)  # Check the expected test outcome.
        self.assertFalse(resp.json()["success"])  # Check the expected test outcome.


class OpsSummaryAndPaginationTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    """Finance-ops list endpoints paginate (page_size 25) and their /summary/
    siblings aggregate over **all** rows so header KPIs stay accurate.
    """

    def setUp(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from rest_framework.test import APIClient  # Import project symbols exercised by these tests.
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment  # Import project symbols exercised by these tests.

        User = get_user_model()  # Assign test setup data.
        self.user = User.objects.create_user(  # Create test database data.
            email="ops-admin@test.com", password="testpass123",  # Continue structured test data.
            user_type="CX_STAFF", status="ACTIVE", first_name="Ops", last_name="Admin",  # Continue structured test data.
        )  # Close the grouped test expression.
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")  # Create test database data.
        PlatformUserRoleAssignment.objects.create(  # Create test database data.
            user=self.user, role=role, assignment_status="ACTIVE")  # Assign test setup data.
        self.client = APIClient()  # Assign test setup data.
        self.client.force_authenticate(user=self.user)  # Exercise the test HTTP client.

    def _claim(self, entity, *, unit_price):  # Define a test helper or test method.
        r = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/expense-claims/?entity={entity.code}",  # Continue structured test data.
            {"claimant_name": "Jane Staff", "claim_date": "2026-01-10", "title": "Trip",  # Continue structured test data.
             "lines": [{"description": "Diesel", "expense_account": "5300",  # Continue structured test data.
                        "quantity": 1, "unit_price": unit_price}]}, format="json")  # Assign test setup data.
        self.assertEqual(r.status_code, 201, r.content)  # Check the expected test outcome.
        return r.json()["data"]["id"]  # Return the prepared test value.

    def test_expense_list_paginates_and_summary_aggregates_all_rows(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self._claim(entity, unit_price=100000)  # Assign test setup data.
        self._claim(entity, unit_price=300000)  # Assign test setup data.

        lst = self.client.get(f"/v1/finance/expense-claims/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(lst.status_code, 200, lst.content)  # Check the expected test outcome.
        body = lst.json()  # Assign test setup data.
        self.assertIn("pagination", body)  # Check the expected test outcome.
        self.assertEqual(body["pagination"]["pageSize"], 25)  # Check the expected test outcome.
        self.assertEqual(body["pagination"]["totalItems"], 2)  # Check the expected test outcome.

        summ = self.client.get(f"/v1/finance/expense-claims/summary/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(summ.status_code, 200, summ.content)  # Check the expected test outcome.
        data = summ.json()["data"]  # Assign test setup data.
        self.assertEqual(data["open"], 2)          # both drafts are open
        self.assertEqual(data["avg"], 200000)      # (100000 + 300000) / 2
        self.assertEqual(data["awaiting"], 0)      # none posted yet

    def test_ops_summary_endpoints_handle_empty_books(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        for path, keys in (  # Iterate through test data.
            ("expense-claims", {"open", "month_total", "avg", "awaiting"}),  # Continue structured test data.
            ("payroll-runs", {"runs", "employees", "net", "to_pay"}),  # Continue structured test data.
            ("fixed-assets", {"cost", "accum", "nbv", "monthly"}),  # Continue structured test data.
            ("tax-filings", {"outstanding", "open", "filed", "paid"}),  # Continue structured test data.
        ):  # Start the nested test block.
            r = self.client.get(f"/v1/finance/{path}/summary/?entity={entity.code}")  # Exercise the test HTTP client.
            self.assertEqual(r.status_code, 200, f"{path}: {r.content}")  # Check the expected test outcome.
            self.assertEqual(set(r.json()["data"].keys()), keys, path)  # Check the expected test outcome.

    # ── Audit trail ───────────────────────────────────────────────────────
    def _audit(self, entity, **kw):  # Define a test helper or test method.
        defaults = dict(  # Continue structured test data.
            entity=entity, actor=self.user,  # Continue structured test data.
            action=FinanceAuditAction.JOURNAL_POSTED,  # Continue structured test data.
            status=FinanceAuditStatus.SUCCESS,  # Continue structured test data.
            target_type="JournalEntry", target_id="1", document_number="JE-1",  # Continue structured test data.
            message="", before={}, after={}, metadata={"secret": "internal-only"},  # Continue structured test data.
        )  # Close the grouped test expression.
        defaults.update(kw)  # Execute the test step.
        return FinanceAuditLog.objects.create(**defaults)  # Return the prepared test value.

    def test_audit_log_lists_and_never_leaks_metadata(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self._audit(entity, before={"status": "DRAFT"}, after={"status": "POSTED"},  # Continue structured test data.
                    document_number="JE-9")  # Assign test setup data.
        resp = self.client.get(f"/v1/finance/audit-logs/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        rows = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(len(rows), 1)  # Check the expected test outcome.
        row = rows[0]  # Assign test setup data.
        self.assertNotIn("metadata", row)                      # internal bag stays server-side
        self.assertEqual(row["before"], {"status": "DRAFT"})  # Check the expected test outcome.
        self.assertEqual(row["after"], {"status": "POSTED"})  # Check the expected test outcome.
        self.assertEqual(row["action_display"], "Journal posted")  # Check the expected test outcome.
        self.assertEqual(row["actor"], self.user.email)  # Check the expected test outcome.

    def test_audit_log_filters(self):  # Define a test helper or test method.
        from django.utils import timezone  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self._audit(entity, action=FinanceAuditAction.JOURNAL_POSTED,  # Continue structured test data.
                    status=FinanceAuditStatus.SUCCESS,  # Continue structured test data.
                    created_at=timezone.make_aware(datetime.datetime(2026, 1, 5, 9, 0)))  # Assign test setup data.
        self._audit(entity, action=FinanceAuditAction.JOURNAL_POST_REJECTED,  # Continue structured test data.
                    status=FinanceAuditStatus.FAILED, target_type="Payment",  # Continue structured test data.
                    created_at=timezone.make_aware(datetime.datetime(2026, 6, 20, 9, 0)))  # Assign test setup data.

        base = f"/v1/finance/audit-logs/?entity={entity.code}"  # Assign test setup data.
        self.assertEqual(len(self.client.get(base).json()["data"]), 2)  # Check the expected test outcome.
        self.assertEqual(len(self.client.get(base + "&status=FAILED").json()["data"]), 1)  # Check the expected test outcome.
        self.assertEqual(len(self.client.get(base + "&action=JOURNAL_POSTED").json()["data"]), 1)  # Check the expected test outcome.
        self.assertEqual(len(self.client.get(base + "&target_type=Payment").json()["data"]), 1)  # Check the expected test outcome.
        self.assertEqual(len(self.client.get(base + f"&actor={self.user.id}").json()["data"]), 2)  # Check the expected test outcome.
        # Inclusive date window keeps only the January row.
        win = self.client.get(base + "&date_from=2026-01-01&date_to=2026-01-31").json()["data"]  # Exercise the test HTTP client.
        self.assertEqual(len(win), 1)  # Check the expected test outcome.
        self.assertEqual(win[0]["action"], "JOURNAL_POSTED")  # Check the expected test outcome.

    def test_audit_log_scoped_to_entity(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        other = LedgerEntity.objects.create(  # Create test database data.
            name="Other Books", code="OTHER", kind=LedgerEntity.Kind.TENANT)  # Assign test setup data.
        self._audit(entity, document_number="MINE")  # Assign test setup data.
        self._audit(other, document_number="THEIRS")  # Assign test setup data.
        rows = self.client.get(f"/v1/finance/audit-logs/?entity={entity.code}").json()["data"]  # Exercise the test HTTP client.
        self.assertEqual([r["document_number"] for r in rows], ["MINE"])  # Check the expected test outcome.

    def test_audit_facets_return_present_options_only(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self._audit(entity, action=FinanceAuditAction.JOURNAL_POSTED, target_type="JournalEntry")  # Assign test setup data.
        self._audit(entity, action=FinanceAuditAction.PAYMENT_POSTED, target_type="Payment")  # Assign test setup data.
        # Two rows share JOURNAL_POSTED — the facet must still be de-duplicated
        # (guards the .distinct()/Meta.ordering gotcha that returned dup codes).
        self._audit(entity, action=FinanceAuditAction.JOURNAL_POSTED, target_type="JournalEntry")  # Assign test setup data.
        data = self.client.get(f"/v1/finance/audit-logs/facets/?entity={entity.code}").json()["data"]  # Exercise the test HTTP client.
        self.assertEqual([a["email"] for a in data["actors"]], [self.user.email])  # Check the expected test outcome.
        self.assertEqual(set(data["target_types"]), {"JournalEntry", "Payment"})  # Check the expected test outcome.
        codes = [a["value"] for a in data["actions"]]  # Assign test setup data.
        self.assertEqual(sorted(codes), ["JOURNAL_POSTED", "PAYMENT_POSTED"])   # no dupes
        self.assertEqual(  # Check the expected test outcome.
            {a["value"]: a["label"] for a in data["actions"]},  # Continue structured test data.
            {"JOURNAL_POSTED": "Journal posted", "PAYMENT_POSTED": "Payment posted"})  # Execute the test step.


class EntityCreatePermissionTests(TestCase):  # Define a test fixture or test case class.
    """Provisioning a new entity must be gated on ``finance.entity.create``.

    A plain authenticated staff user holding no role (hence no grant) must be
    denied — proving the POST is RBAC-gated, not open like the GET-only list was.
    """

    def setUp(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from rest_framework.test import APIClient  # Import project symbols exercised by these tests.

        User = get_user_model()  # Assign test setup data.
        self.user = User.objects.create_user(  # Create test database data.
            email="no-grant@test.com", password="testpass123",  # Continue structured test data.
            user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="No", last_name="Grant",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.client = APIClient()  # Assign test setup data.
        self.client.force_authenticate(user=self.user)  # Exercise the test HTTP client.

    def test_create_denied_without_grant(self):  # Define a test helper or test method.
        resp = self.client.post(  # Exercise the test HTTP client.
            "/v1/finance/entities/", {"code": "NOPE", "name": "Nope"}, format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 403)  # Check the expected test outcome.

    def test_list_still_denied_without_view_grant(self):  # Define a test helper or test method.
        # The GET side is gated on finance.entity.view; same ungranted user is denied.
        resp = self.client.get("/v1/finance/entities/")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 403)  # Check the expected test outcome.

    def test_direct_entry_denied_without_grant(self):  # Define a test helper or test method.
        # Posting a direct entry is gated on finance.directentry.post (CRITICAL).
        resp = self.client.post(  # Exercise the test HTTP client.
            "/v1/finance/direct-entries/?entity=TBOOK",  # Continue structured test data.
            {"lines": [{"account": "1100", "debit": 100}, {"account": "3100", "credit": 100}]},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 403)  # Check the expected test outcome.

    def test_audit_view_denied_without_grant(self):  # Define a test helper or test method.
        # The audit trail (and its facets) are gated on finance.audit.view.
        self.assertEqual(self.client.get("/v1/finance/audit-logs/?entity=TBOOK").status_code, 403)  # Check the expected test outcome.
        self.assertEqual(self.client.get("/v1/finance/audit-logs/facets/?entity=TBOOK").status_code, 403)  # Check the expected test outcome.


class _StubUser:  # Define a test fixture or test case class.
    """A minimal user carrying just the attributes get_queryset reads."""
    def __init__(self, user_type, school=None):  # Define a test helper or test method.
        self.user_type = user_type  # Assign test setup data.
        self.school = school  # Assign test setup data.


class _StubRequest:  # Define a test fixture or test case class.
    """A minimal request exposing user + query_params (and optionally .school)."""
    def __init__(self, user, params=None):  # Define a test helper or test method.
        self.user = user  # Assign test setup data.
        self.query_params = params or {}  # Assign test setup data.


class EntityListScopingTests(TestCase):  # Define a test fixture or test case class.
    """EntityListCreateView.get_queryset is tenancy-scoped for non-platform staff (F1)."""

    def setUp(self):  # Define a test helper or test method.
        self.school = School.objects.create(name="Greenfield", slug="greenfield-f1", code="GRNF1")  # Create test database data.
        self.other = School.objects.create(name="Bluewater", slug="bluewater-f1", code="BLUF1")  # Create test database data.
        self.mine = LedgerEntity.objects.create(  # Create test database data.
            name="Greenfield Books", code="GREENF1",  # Continue structured test data.
            kind=LedgerEntity.Kind.TENANT, source_school=self.school,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.theirs = LedgerEntity.objects.create(  # Create test database data.
            name="Bluewater Books", code="BLUEF1",  # Continue structured test data.
            kind=LedgerEntity.Kind.TENANT, source_school=self.other,  # Continue structured test data.
        )  # Close the grouped test expression.

    def _codes(self, user, params=None):  # Define a test helper or test method.
        from vs_finance.views import EntityListCreateView  # Import project symbols exercised by these tests.

        view = EntityListCreateView()  # Assign test setup data.
        view.request = _StubRequest(user=user, params=params)  # Assign test setup data.
        return set(view.get_queryset().values_list("code", flat=True))  # Return the prepared test value.

    def test_cx_staff_sees_every_entity(self):  # Define a test helper or test method.
        codes = self._codes(_StubUser("CX_STAFF"))  # Assign test setup data.
        self.assertTrue({"GREENF1", "BLUEF1"} <= codes)  # Check the expected test outcome.

    def test_school_user_sees_only_own(self):  # Define a test helper or test method.
        codes = self._codes(_StubUser("SCHOOL_STAFF", school=self.school))  # Assign test setup data.
        self.assertEqual(codes, {"GREENF1"})  # Check the expected test outcome.

    def test_user_without_school_sees_none(self):  # Define a test helper or test method.
        self.assertEqual(self._codes(_StubUser("SCHOOL_STAFF")), set())  # Check the expected test outcome.

    def test_scoping_composes_with_kind_filter(self):  # Define a test helper or test method.
        codes = self._codes(_StubUser("SCHOOL_STAFF", school=self.school),  # Continue structured test data.
                            params={"kind": LedgerEntity.Kind.TENANT})  # Assign test setup data.
        self.assertEqual(codes, {"GREENF1"})  # Check the expected test outcome.


class FinanceDashboardTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The aggregated Finance-overview payload computes every block from the GL."""

    def test_dashboard_payload_reflects_posted_invoice(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.

        from vs_finance.dashboard import finance_dashboard  # Import project symbols exercised by these tests.

        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # Dr AR 100,000 ; Cr Revenue 100,000 (no tax)

        # A journal with a real author guards the actor-label path (the custom User
        # model has no get_full_name; recent_journals must compose first/last/email).
        author = get_user_model().objects.create_user(  # Create test database data.
            email="fin.officer@test.com", password="x",  # Continue structured test data.
            user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Fin", last_name="Officer",  # Continue structured test data.
        )  # Close the grouped test expression.
        je = JournalEntry.objects.create(  # Create test database data.
            entity=entity, date=datetime.date(2026, 1, 20), period=period,  # Continue structured test data.
            narration="manual adj", created_by=author,  # Continue structured test data.
        )  # Close the grouped test expression.
        JournalLine.objects.create(  # Create test database data.
            entry=je, account=Account.objects.get(entity=entity, code="1100"),  # Fetch test database data.
            debit=5000, credit=0, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        JournalLine.objects.create(  # Create test database data.
            entry=je, account=Account.objects.get(entity=entity, code="4100"),  # Fetch test database data.
            debit=0, credit=5000, line_no=2,  # Continue structured test data.
        )  # Close the grouped test expression.

        # A capital injection into 1100 Cash & Bank must surface as cash position —
        # even with no operational BankAccount record (the reported glitch).
        post_journal(self.make_entry(entity, period, [("1100", 8000000, 0), ("3100", 0, 8000000)]))  # Execute the test step.

        d = finance_dashboard(entity)  # Assign test setup data.

        self.assertIn("Fin Officer", [j["created_by"] for j in d["recent_journals"]])  # Check the expected test outcome.

        # Cash position reflects the 1100 posting and reconciles to the cash-flow stmt.
        from vs_finance.reports import cash_flow_statement  # Import project symbols exercised by these tests.
        self.assertEqual(d["kpis"]["cash_position"]["value"]["kobo"], 8000000)  # Check the expected test outcome.
        self.assertEqual(  # Check the expected test outcome.
            d["kpis"]["cash_position"]["value"]["kobo"], cash_flow_statement(entity).closing_cash,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(d["fiscal_year"], "2026")  # Check the expected test outcome.

        # As-of defaults to the present day; pinning a period moves it to period-end.
        self.assertEqual(d["as_of"], datetime.date.today().isoformat())  # Check the expected test outcome.
        pinned = finance_dashboard(entity, period=period)  # Assign test setup data.
        self.assertEqual(pinned["as_of"], period.end_date.isoformat())  # Check the expected test outcome.

        # Top-level blocks all present.
        for key in (  # Iterate through test data.
            "kpis", "revenue_vs_budget", "ar_aging", "trend", "top_overdue",  # Continue structured test data.
            "vendor_due", "approvals", "close_progress", "recent_journals",  # Continue structured test data.
        ):  # Start the nested test block.
            self.assertIn(key, d)  # Check the expected test outcome.

        # KPI envelope shape.
        for kpi in d["kpis"].values():  # Iterate through test data.
            self.assertIn("value", kpi)  # Check the expected test outcome.
            self.assertIn("delta_pct", kpi)  # Check the expected test outcome.
            self.assertIsInstance(kpi["spark"], list)  # Check the expected test outcome.

        # Receivables + net income reflect the posted invoice.
        self.assertEqual(d["kpis"]["receivables"]["value"]["kobo"], 100000)  # Check the expected test outcome.
        self.assertEqual(d["kpis"]["net_income_ytd"]["value"]["kobo"], 100000)  # Check the expected test outcome.
        self.assertEqual(d["ar_aging"]["total"]["kobo"], 100000)  # Check the expected test outcome.

        # The overdue invoice (due 25 Jan, as-of 31 Jan) tops the overdue list.
        self.assertTrue(d["top_overdue"])  # Check the expected test outcome.
        self.assertEqual(d["top_overdue"][0]["reference"], inv.document_number)  # Check the expected test outcome.
        self.assertEqual(d["top_overdue"][0]["amount"]["kobo"], 100000)  # Check the expected test outcome.

        # Trend is a fixed 12-month window; recent journals include the AR entry.
        self.assertEqual(len(d["trend"]["labels"]), 12)  # Check the expected test outcome.
        self.assertEqual(len(d["trend"]["issued"]), 12)  # Check the expected test outcome.
        self.assertTrue(d["recent_journals"])  # Check the expected test outcome.

        # Period close progress runs the checklist for the open period.
        self.assertIsNotNone(d["close_progress"])  # Check the expected test outcome.
        self.assertEqual(d["close_progress"]["total"], len(d["close_progress"]["checks"]))  # Check the expected test outcome.


class InvoiceDetailEndpointTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The invoice detail drawer endpoint returns lines + GL postings."""

    def test_detail_returns_lines_and_postings(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment  # Import project symbols exercised by these tests.
        from vs_finance.views import InvoiceDetailView  # Import project symbols exercised by these tests.

        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, vat)])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.

        u = get_user_model().objects.create_user(  # Create test database data.
            email="inv-detail@test.com", password="x", user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Inv", last_name="Detail")  # Assign test setup data.
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")  # Create test database data.
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")  # Create test database data.

        req = APIRequestFactory().get(f"/v1/finance/invoices/{inv.pk}/", {"entity": entity.code})  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = InvoiceDetailView.as_view()(req, pk=inv.pk)  # Assign test setup data.
        resp.render()  # Execute the test step.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        d = json.loads(resp.content)["data"]  # Assign test setup data.
        self.assertEqual(d["invoice"]["document_number"], inv.document_number)  # Check the expected test outcome.
        self.assertTrue(d["lines"])  # Check the expected test outcome.
        self.assertEqual(d["lines"][0]["account_code"], "4100")  # Check the expected test outcome.
        self.assertTrue(d["gl_postings"])  # Check the expected test outcome.
        self.assertEqual(d["summary"]["total"]["kobo"], inv.total)  # Check the expected test outcome.

    def test_detail_surfaces_credit_note_concession_and_write_off_settlements(self):  # Define a test helper or test method.
        """A credit note, a concession and a write-off must all appear in settlements,
        gl_journals and the summary — not just cash payments."""
        import json  # Import dependency used by this test module.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment  # Import project symbols exercised by these tests.
        from vs_finance.views import InvoiceDetailView  # Import project symbols exercised by these tests.

        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # total 100,000, all AR

        # 30,000 credit note + 20,000 concession, then write off the remaining balance.
        note = CreditNote.objects.create(  # Create test database data.
            entity=entity, customer=customer, kind=CreditNoteKind.CREDIT,  # Continue structured test data.
            note_date=datetime.date(2026, 1, 15), invoice=inv, reason="Goodwill",  # Continue structured test data.
        )  # Close the grouped test expression.
        CreditNoteLine.objects.create(  # Create test database data.
            note=note, revenue_account=Account.objects.get(entity=entity, code="4900"),  # Fetch test database data.
            quantity=1, unit_price=30000, tax_code=None, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_credit_note(note, auto_allocate=True)  # Assign test setup data.
        concession = Concession.objects.create(  # Create test database data.
            entity=entity, customer=customer, invoice=inv, kind="WAIVER",  # Continue structured test data.
            concession_date=datetime.date(2026, 1, 18), amount=20000,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_concession(concession)  # Execute the test step.
        write_off_invoice(inv, write_off_date=datetime.date(2026, 1, 28))  # Assign test setup data.
        inv.refresh_from_db()  # Execute the test step.

        u = get_user_model().objects.create_user(  # Create test database data.
            email="inv-settle@test.com", password="x", user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Inv", last_name="Settle")  # Assign test setup data.
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")  # Create test database data.
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")  # Create test database data.

        req = APIRequestFactory().get(f"/v1/finance/invoices/{inv.pk}/", {"entity": entity.code})  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = InvoiceDetailView.as_view()(req, pk=inv.pk)  # Assign test setup data.
        resp.render()  # Execute the test step.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        d = json.loads(resp.content)["data"]  # Assign test setup data.

        # Summary now splits cash vs non-cash and fully settles the invoice.
        self.assertEqual(d["summary"]["paid"]["kobo"], 0)  # Check the expected test outcome.
        self.assertEqual(d["summary"]["credited"]["kobo"], 100000)  # Check the expected test outcome.
        self.assertEqual(d["summary"]["settled"]["kobo"], 100000)  # Check the expected test outcome.
        self.assertEqual(d["summary"]["balance"]["kobo"], 0)  # Check the expected test outcome.

        # Settlements carry the credit note, the concession and the write-off (no cash).
        types = {s["type"] for s in d["settlements"]}  # Assign test setup data.
        self.assertEqual(types, {"CREDIT_NOTE", "CONCESSION", "WRITE_OFF"})  # Check the expected test outcome.
        self.assertEqual([s["type"] for s in d["settlements"] if s["type"] == "PAYMENT"], [])  # Check the expected test outcome.

        # GL history has every journal: invoice, credit note, concession, write-off.
        doc_types = {g["document_type"] for g in d["gl_journals"]}  # Assign test setup data.
        self.assertEqual(doc_types, {"INVOICE", "CREDIT_NOTE", "CONCESSION", "WRITE_OFF"})  # Check the expected test outcome.

        # Activity timeline mentions all three non-cash events.
        labels = " ".join(a["label"] for a in d["activity"])  # Assign test setup data.
        self.assertIn("Credit note", labels)  # Check the expected test outcome.
        self.assertIn("Waiver", labels)  # Check the expected test outcome.
        self.assertIn("Write-off", labels)  # Check the expected test outcome.


class FinanceDocumentEndpointTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """Printable invoice and receipt document endpoints."""

    def _user(self, email="finance-docs@test.com"):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment  # Import project symbols exercised by these tests.

        u = get_user_model().objects.create_user(  # Create test database data.
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Finance", last_name="Docs",  # Continue structured test data.
        )  # Close the grouped test expression.
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")  # Create test database data.
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")  # Create test database data.
        return u  # Return the prepared test value.

    def _request(self, path, entity, user):  # Define a test helper or test method.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.

        req = APIRequestFactory().get(path, {"entity": entity.code})  # Assign test setup data.
        force_authenticate(req, user=user)  # Assign test setup data.
        return req  # Return the prepared test value.

    def test_invoice_document_renders_html_with_collection_account(self):  # Define a test helper or test method.
        from vs_finance.documents import primary_collection_account  # Import project symbols exercised by these tests.
        from vs_finance.views import InvoiceDocumentView  # Import project symbols exercised by these tests.

        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        BankAccount.objects.create(  # Create test database data.
            entity=entity, name="Operations Account",  # Continue structured test data.
            bank_name="Access Bank",  # Continue structured test data.
            account_number="111",  # Continue structured test data.
            gl_account=Account.objects.get(entity=entity, code="1100"),  # Fetch test database data.
            is_active=True,  # Continue structured test data.
        )  # Close the grouped test expression.
        collection = BankAccount.objects.create(  # Create test database data.
            entity=entity, name="Collections Account",  # Continue structured test data.
            bank_name="GTBank",  # Continue structured test data.
            account_number="222",  # Continue structured test data.
            gl_account=Account.objects.get(entity=entity, code="1100"),  # Fetch test database data.
            is_active=True,  # Continue structured test data.
            is_primary_collection=True,  # Continue structured test data.
        )  # Close the grouped test expression.
        inv = self.make_invoice(  # Continue structured test data.
            entity, customer, lines=[("4100", 1, 100000, None)],  # Continue structured test data.
            due=datetime.date(2026, 1, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        post_invoice(inv)  # Execute the test step.

        self.assertEqual(primary_collection_account(entity), collection)  # Check the expected test outcome.
        req = self._request(f"/v1/finance/invoices/{inv.pk}/document/", entity, self._user())  # Assign test setup data.
        resp = InvoiceDocumentView.as_view()(req, pk=inv.pk)  # Assign test setup data.

        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        self.assertEqual(resp["Content-Type"], "text/html; charset=utf-8")  # Check the expected test outcome.
        html = resp.content.decode()  # Assign test setup data.
        self.assertIn("Tax Invoice", html)  # Check the expected test outcome.
        self.assertIn(inv.document_number, html)  # Check the expected test outcome.
        self.assertIn("Acme Ltd", html)  # Check the expected test outcome.
        self.assertIn("GTBank", html)  # Check the expected test outcome.
        self.assertIn("222", html)  # Check the expected test outcome.

    def test_receipt_document_renders_html(self):  # Define a test helper or test method.
        from vs_finance.views_ar import PaymentReceiptView  # Import project symbols exercised by these tests.

        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        payment = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),  # Continue structured test data.
            amount=40000, deposit_account=Account.objects.get(entity=entity, code="1100"),  # Fetch test database data.
        )  # Close the grouped test expression.
        post_payment(payment)  # Execute the test step.
        payment.refresh_from_db()  # Execute the test step.

        req = self._request(f"/v1/finance/payments/{payment.pk}/receipt/", entity, self._user("receipt-docs@test.com"))  # Assign test setup data.
        resp = PaymentReceiptView.as_view()(req, pk=payment.pk)  # Assign test setup data.

        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        html = resp.content.decode()  # Assign test setup data.
        self.assertIn("Receipt", html)  # Check the expected test outcome.
        self.assertIn(payment.document_number, html)  # Check the expected test outcome.
        self.assertIn(inv.document_number, html)  # Check the expected test outcome.
        self.assertIn("Four hundred naira only", html)  # Check the expected test outcome.

    def test_pdf_endpoint_uses_weasyprint_renderer_when_available(self):  # Define a test helper or test method.
        from unittest.mock import patch  # Import project symbols exercised by these tests.

        from vs_finance.views import InvoiceDocumentPDFView  # Import project symbols exercised by these tests.

        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.

        req = self._request(f"/v1/finance/invoices/{inv.pk}/document.pdf", entity, self._user("invoice-pdf@test.com"))  # Assign test setup data.
        with patch("vs_finance.documents.render_document_pdf", return_value=b"%PDF-1.7 fake") as renderer:  # Enter a test context manager.
            resp = InvoiceDocumentPDFView.as_view()(req, pk=inv.pk)  # Assign test setup data.

        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        self.assertEqual(resp["Content-Type"], "application/pdf")  # Check the expected test outcome.
        self.assertEqual(resp.content, b"%PDF-1.7 fake")  # Check the expected test outcome.
        self.assertIn(inv.document_number, resp["Content-Disposition"])  # Check the expected test outcome.
        self.assertTrue(renderer.called)  # Check the expected test outcome.

    def test_pdf_endpoint_returns_503_when_renderer_unavailable(self):  # Define a test helper or test method.
        from unittest.mock import patch  # Import project symbols exercised by these tests.

        from vs_finance.documents import DocumentRenderUnavailable  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import PaymentReceiptPDFView  # Import project symbols exercised by these tests.

        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        payment = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),  # Continue structured test data.
            amount=40000, deposit_account=Account.objects.get(entity=entity, code="1100"),  # Fetch test database data.
        )  # Close the grouped test expression.
        post_payment(payment)  # Execute the test step.
        payment.refresh_from_db()  # Execute the test step.

        req = self._request(f"/v1/finance/payments/{payment.pk}/receipt.pdf", entity, self._user("receipt-pdf@test.com"))  # Assign test setup data.
        with patch(  # Enter a test context manager.
            "vs_finance.documents.render_document_pdf",  # Continue structured test data.
            side_effect=DocumentRenderUnavailable("missing native libs"),  # Continue structured test data.
        ):  # Start the nested test block.
            resp = PaymentReceiptPDFView.as_view()(req, pk=payment.pk)  # Assign test setup data.
            resp.render()  # Execute the test step.

        self.assertEqual(resp.status_code, 503)  # Check the expected test outcome.
        self.assertEqual(resp.data["detail"], "PDF rendering is unavailable on this server.")  # Check the expected test outcome.

    def test_primary_collection_account_falls_back_to_first_active_account(self):  # Define a test helper or test method.
        from vs_finance.documents import primary_collection_account  # Import project symbols exercised by these tests.

        entity, period, customer, vat = self.build_ar()  # Assign test setup data.
        inactive = BankAccount.objects.create(  # Create test database data.
            entity=entity, name="Inactive",  # Continue structured test data.
            gl_account=Account.objects.get(entity=entity, code="1100"),  # Fetch test database data.
            is_active=False,  # Continue structured test data.
        )  # Close the grouped test expression.
        active = BankAccount.objects.create(  # Create test database data.
            entity=entity, name="Active Collections",  # Continue structured test data.
            gl_account=Account.objects.get(entity=entity, code="1100"),  # Fetch test database data.
            is_active=True,  # Continue structured test data.
        )  # Close the grouped test expression.

        self.assertEqual(primary_collection_account(entity), active)  # Check the expected test outcome.
        inactive.is_primary_collection = True  # Assign test setup data.
        inactive.save(update_fields=["is_primary_collection", "updated_at"])  # Assign test setup data.
        self.assertEqual(primary_collection_account(entity), inactive)  # Check the expected test outcome.


class FinanceMigrationStateTests(TestCase):  # Define a test fixture or test case class.
    def test_bank_account_primary_collection_column_exists(self):  # Define a test helper or test method.
        from django.db import connection  # Import project symbols exercised by these tests.

        with connection.cursor() as cursor:  # Enter a test context manager.
            columns = {  # Continue structured test data.
                col.name  # Execute the test step.
                for col in connection.introspection.get_table_description(  # Iterate through test data.
                    cursor, BankAccount._meta.db_table)  # Execute the test step.
            }  # Close the grouped test expression.
        self.assertIn("is_primary_collection", columns)  # Check the expected test outcome.


class InvoiceCreateEndpointTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """POST /finance/invoices/ raises (and posts) a manual invoice, gated on create."""

    def _super_admin(self, email):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment  # Import project symbols exercised by these tests.
        u = get_user_model().objects.create_user(  # Create test database data.
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Inv", last_name="Maker")  # Assign test setup data.
        role, _ = PlatformRoleTemplate.objects.get_or_create(id="xvs_super_admin", defaults={"name": "Super Admin"})  # Fetch test database data.
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")  # Create test database data.
        return u  # Return the prepared test value.

    def _post(self, entity, user, body):  # Define a test helper or test method.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_finance.views import InvoiceListCreateView as InvoiceListView  # Import project symbols exercised by these tests.
        req = APIRequestFactory().post(  # Continue structured test data.
            f"/v1/finance/invoices/?entity={entity.code}", body, format="json")  # Assign test setup data.
        force_authenticate(req, user=user)  # Assign test setup data.
        resp = InvoiceListView.as_view()(req)  # Assign test setup data.
        resp.render()  # Execute the test step.
        return resp  # Return the prepared test value.

    def test_create_posts_invoice_with_tax(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from vs_finance.constants import DocumentStatus  # Import project symbols exercised by these tests.
        entity, _period, _customer, vat = self.build_ar()  # Assign test setup data.
        u = self._super_admin("inv-create@test.com")  # Assign test setup data.
        resp = self._post(entity, u, {  # Continue structured test data.
            "customer": "CUST1", "invoice_date": "2026-01-10", "due_date": "2026-01-25",  # Continue structured test data.
            "lines": [{"revenue_account": "4100", "description": "Consulting",  # Continue structured test data.
                       "quantity": 2, "unit_price": 50000, "tax_code": "VAT"}],  # Continue structured test data.
        })  # Execute the test step.
        self.assertEqual(resp.status_code, 201)  # Check the expected test outcome.
        d = json.loads(resp.content)["data"]  # Assign test setup data.
        self.assertEqual(d["status"], DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertEqual(d["subtotal"], 100000)  # Check the expected test outcome.
        self.assertEqual(d["tax_total"], 7500)  # Check the expected test outcome.
        self.assertEqual(d["total"], 107500)  # Check the expected test outcome.
        inv = Invoice.objects.get(pk=d["id"])  # Fetch test database data.
        self.assertIsNotNone(inv.journal_id)   # AR journal raised

    def test_create_draft_when_post_false(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from vs_finance.constants import DocumentStatus  # Import project symbols exercised by these tests.
        entity, _p, _c, _vat = self.build_ar()  # Assign test setup data.
        u = self._super_admin("inv-draft@test.com")  # Assign test setup data.
        resp = self._post(entity, u, {  # Continue structured test data.
            "customer": "CUST1", "invoice_date": "2026-01-10", "post": False,  # Continue structured test data.
            "lines": [{"revenue_account": "4100", "quantity": 1, "unit_price": 30000}],  # Continue structured test data.
        })  # Execute the test step.
        self.assertEqual(resp.status_code, 201)  # Check the expected test outcome.
        d = json.loads(resp.content)["data"]  # Assign test setup data.
        self.assertEqual(d["status"], DocumentStatus.DRAFT)  # Check the expected test outcome.
        self.assertEqual(d["total"], 30000)  # Check the expected test outcome.
        self.assertIsNone(Invoice.objects.get(pk=d["id"]).journal_id)  # Check the expected test outcome.

    def test_create_requires_permission(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        entity, _p, _c, _vat = self.build_ar()  # Assign test setup data.
        # A plain active user with no super-admin role lacks finance.invoice.create.
        u = get_user_model().objects.create_user(  # Create test database data.
            email="inv-nobody@test.com", password="x", user_type="CX_STAFF",  # Continue structured test data.
            status="ACTIVE", first_name="No", last_name="Perm")  # Assign test setup data.
        resp = self._post(entity, u, {  # Continue structured test data.
            "customer": "CUST1", "invoice_date": "2026-01-10",  # Continue structured test data.
            "lines": [{"revenue_account": "4100", "quantity": 1, "unit_price": 30000}],  # Continue structured test data.
        })  # Execute the test step.
        self.assertEqual(resp.status_code, 403)  # Check the expected test outcome.
        self.assertEqual(Invoice.objects.filter(entity=entity).count(), 0)  # Check the expected test outcome.

    def test_create_rejects_empty_lines(self):  # Define a test helper or test method.
        entity, _p, _c, _vat = self.build_ar()  # Assign test setup data.
        u = self._super_admin("inv-empty@test.com")  # Assign test setup data.
        resp = self._post(entity, u, {"customer": "CUST1", "invoice_date": "2026-01-10", "lines": []})  # Assign test setup data.
        self.assertEqual(resp.status_code, 400)  # Check the expected test outcome.


class InvoicePayRemindEndpointTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """POST /invoices/<id>/pay/ records a receipt; /remind/ raises a dunning notice."""

    def _super_admin(self, email):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment  # Import project symbols exercised by these tests.
        u = get_user_model().objects.create_user(  # Create test database data.
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Pay", last_name="Tester")  # Assign test setup data.
        role, _ = PlatformRoleTemplate.objects.get_or_create(id="xvs_super_admin", defaults={"name": "Super Admin"})  # Fetch test database data.
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")  # Create test database data.
        return u  # Return the prepared test value.

    def _call(self, view, entity, user, pk, body):  # Define a test helper or test method.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        req = APIRequestFactory().post(f"/v1/finance/invoices/{pk}/x/?entity={entity.code}", body, format="json")  # Assign test setup data.
        force_authenticate(req, user=user)  # Assign test setup data.
        resp = view.as_view()(req, pk=pk)  # Assign test setup data.
        resp.render()  # Execute the test step.
        return resp  # Return the prepared test value.

    def _posted_invoice(self):  # Define a test helper or test method.
        entity, _period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, vat)])  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        return entity, inv  # Return the prepared test value.

    def test_pay_settles_invoice(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from vs_finance.constants import InvoicePaymentStatus  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import InvoicePayView  # Import project symbols exercised by these tests.
        entity, inv = self._posted_invoice()  # Assign test setup data.
        u = self._super_admin("pay-ok@test.com")  # Assign test setup data.
        resp = self._call(InvoicePayView, entity, u, inv.pk, {  # Continue structured test data.
            "amount": inv.total, "payment_date": "2026-01-20",  # Continue structured test data.
            "method": "BANK_TRANSFER", "deposit_account": "1100",  # Continue structured test data.
        })  # Execute the test step.
        self.assertEqual(resp.status_code, 201)  # Check the expected test outcome.
        d = json.loads(resp.content)["data"]  # Assign test setup data.
        self.assertEqual(d["payment_status"], InvoicePaymentStatus.PAID)  # Check the expected test outcome.
        self.assertEqual(d["balance_due"], 0)  # Check the expected test outcome.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.amount_paid, inv.total)  # Check the expected test outcome.

    def test_partial_payment_leaves_balance(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from vs_finance.constants import InvoicePaymentStatus  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import InvoicePayView  # Import project symbols exercised by these tests.
        entity, inv = self._posted_invoice()  # Assign test setup data.
        u = self._super_admin("pay-part@test.com")  # Assign test setup data.
        resp = self._call(InvoicePayView, entity, u, inv.pk, {  # Continue structured test data.
            "amount": 40000, "payment_date": "2026-01-20", "deposit_account": "1100",  # Continue structured test data.
        })  # Execute the test step.
        self.assertEqual(resp.status_code, 201)  # Check the expected test outcome.
        d = json.loads(resp.content)["data"]  # Assign test setup data.
        self.assertEqual(d["payment_status"], InvoicePaymentStatus.PARTIAL)  # Check the expected test outcome.
        self.assertEqual(d["balance_due"], inv.total - 40000)  # Check the expected test outcome.

    def test_pay_requires_permission(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from vs_finance.models import Payment  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import InvoicePayView  # Import project symbols exercised by these tests.
        entity, inv = self._posted_invoice()  # Assign test setup data.
        u = get_user_model().objects.create_user(  # Create test database data.
            email="pay-nobody@test.com", password="x", user_type="CX_STAFF",  # Continue structured test data.
            status="ACTIVE", first_name="No", last_name="Perm")  # Assign test setup data.
        resp = self._call(InvoicePayView, entity, u, inv.pk, {  # Continue structured test data.
            "amount": inv.total, "payment_date": "2026-01-20", "deposit_account": "1100",  # Continue structured test data.
        })  # Execute the test step.
        self.assertEqual(resp.status_code, 403)  # Check the expected test outcome.
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 0)  # Check the expected test outcome.

    def test_remind_raises_and_sends_notice(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from vs_finance.constants import DunningNoticeStatus  # Import project symbols exercised by these tests.
        from vs_finance.models import DunningNotice  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import InvoiceRemindView  # Import project symbols exercised by these tests.
        entity, inv = self._posted_invoice()   # due 2026-01-25 → overdue today
        u = self._super_admin("remind-ok@test.com")  # Assign test setup data.
        resp = self._call(InvoiceRemindView, entity, u, inv.pk, {})  # Assign test setup data.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        d = json.loads(resp.content)["data"]  # Assign test setup data.
        self.assertEqual(d["notice_status"], DunningNoticeStatus.SENT)  # Check the expected test outcome.
        notice = DunningNotice.objects.get(invoice=inv)  # Fetch test database data.
        self.assertEqual(notice.notice_status, DunningNoticeStatus.SENT)  # Check the expected test outcome.
        self.assertGreaterEqual(notice.level, 1)  # Check the expected test outcome.

    def test_remind_is_idempotent_on_level(self):  # Define a test helper or test method.
        from vs_finance.models import DunningNotice  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import InvoiceRemindView  # Import project symbols exercised by these tests.
        entity, inv = self._posted_invoice()  # Assign test setup data.
        u = self._super_admin("remind-twice@test.com")  # Assign test setup data.
        self._call(InvoiceRemindView, entity, u, inv.pk, {})  # Execute the test step.
        self._call(InvoiceRemindView, entity, u, inv.pk, {})  # Execute the test step.
        # Same (invoice, level) → reused, never a duplicate row.
        self.assertEqual(DunningNotice.objects.filter(invoice=inv).count(), 1)  # Check the expected test outcome.


class CustomerEndpointTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """Customer list balance/status, enriched detail/statement, and receipt."""

    def _super_admin(self, email):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment  # Import project symbols exercised by these tests.
        u = get_user_model().objects.create_user(  # Create test database data.
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Cust", last_name="Tester")  # Assign test setup data.
        role, _ = PlatformRoleTemplate.objects.get_or_create(id="xvs_super_admin", defaults={"name": "Super Admin"})  # Fetch test database data.
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")  # Create test database data.
        return u  # Return the prepared test value.

    def _fixture(self):  # Define a test helper or test method.
        entity, _period, customer, vat = self.build_ar()  # Assign test setup data.
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, vat)])  # total 107500
        post_invoice(inv)  # Execute the test step.
        return entity, customer, inv  # Return the prepared test value.

    def test_list_includes_balance_and_status(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import CustomerListCreateView  # Import project symbols exercised by these tests.
        entity, customer, inv = self._fixture()  # Assign test setup data.
        u = self._super_admin("cust-list@test.com")  # Assign test setup data.
        req = APIRequestFactory().get("/v1/finance/customers/", {"entity": entity.code})  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = CustomerListCreateView.as_view()(req); resp.render()  # Assign test setup data.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        row = next(r for r in json.loads(resp.content)["data"] if r["code"] == customer.code)  # Execute the test step.
        self.assertEqual(row["balance"], inv.total)  # Check the expected test outcome.
        self.assertEqual(row["account_status"], "OVERDUE")  # due 2026-01-25 is past

    def test_detail_returns_statement_and_summary(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import CustomerDetailView  # Import project symbols exercised by these tests.
        entity, customer, inv = self._fixture()  # Assign test setup data.
        u = self._super_admin("cust-detail@test.com")  # Assign test setup data.
        req = APIRequestFactory().get(f"/v1/finance/customers/{customer.pk}/", {"entity": entity.code})  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = CustomerDetailView.as_view()(req, pk=str(customer.pk)); resp.render()  # Assign test setup data.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        d = json.loads(resp.content)["data"]  # Assign test setup data.
        self.assertEqual(d["summary"]["current_balance"]["kobo"], inv.total)  # Check the expected test outcome.
        self.assertEqual(d["summary"]["open_invoice_count"], 1)  # Check the expected test outcome.
        self.assertTrue(d["statement"])  # Check the expected test outcome.
        self.assertEqual(d["statement"][-1]["balance"]["kobo"], inv.total)  # Check the expected test outcome.

    def test_receipt_settles_and_allocates(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from vs_finance.constants import InvoicePaymentStatus  # Import project symbols exercised by these tests.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import CustomerReceiptView  # Import project symbols exercised by these tests.
        entity, customer, inv = self._fixture()  # Assign test setup data.
        u = self._super_admin("cust-receipt@test.com")  # Assign test setup data.
        req = APIRequestFactory().post(  # Continue structured test data.
            f"/v1/finance/customers/{customer.pk}/receipt/?entity={entity.code}",  # Continue structured test data.
            {"amount": inv.total, "payment_date": "2026-01-20", "deposit_account": "1100"},  # Continue structured test data.
            format="json")  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = CustomerReceiptView.as_view()(req, pk=str(customer.pk)); resp.render()  # Assign test setup data.
        self.assertEqual(resp.status_code, 201)  # Check the expected test outcome.
        self.assertEqual(json.loads(resp.content)["data"]["allocated"], inv.total)  # Check the expected test outcome.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PAID)  # Check the expected test outcome.

    def test_receipt_requires_permission(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from vs_finance.models import Payment  # Import project symbols exercised by these tests.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import CustomerReceiptView  # Import project symbols exercised by these tests.
        entity, customer, inv = self._fixture()  # Assign test setup data.
        u = get_user_model().objects.create_user(  # Create test database data.
            email="cust-noperm@test.com", password="x", user_type="CX_STAFF",  # Continue structured test data.
            status="ACTIVE", first_name="No", last_name="Perm")  # Assign test setup data.
        req = APIRequestFactory().post(  # Continue structured test data.
            f"/v1/finance/customers/{customer.pk}/receipt/?entity={entity.code}",  # Continue structured test data.
            {"amount": 5000, "payment_date": "2026-01-20", "deposit_account": "1100"},  # Continue structured test data.
            format="json")  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = CustomerReceiptView.as_view()(req, pk=str(customer.pk)); resp.render()  # Assign test setup data.
        self.assertEqual(resp.status_code, 403)  # Check the expected test outcome.
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 0)  # Check the expected test outcome.

    def test_receipt_allocates_oldest_first_partially(self):  # Define a test helper or test method.
        # Owe ₦79 (older) + ₦56; pay ₦90 → ₦79 fully + ₦11, leaving ₦45 on the 2nd.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import CustomerReceiptView  # Import project symbols exercised by these tests.
        entity, _period, customer, _vat = self.build_ar()  # Assign test setup data.
        a = self.make_invoice(entity, customer, lines=[("4100", 1, 7900, None)])  # ₦79, older
        post_invoice(a)  # Execute the test step.
        b = self.make_invoice(entity, customer, lines=[("4100", 1, 5600, None)])  # ₦56
        post_invoice(b)  # Execute the test step.
        u = self._super_admin("cust-alloc@test.com")  # Assign test setup data.
        req = APIRequestFactory().post(  # Continue structured test data.
            f"/v1/finance/customers/{customer.pk}/receipt/?entity={entity.code}",  # Continue structured test data.
            {"amount": 9000, "payment_date": "2026-01-20", "deposit_account": "1100"},  # Continue structured test data.
            format="json")  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = CustomerReceiptView.as_view()(req, pk=str(customer.pk)); resp.render()  # Assign test setup data.
        self.assertEqual(resp.status_code, 201)  # Check the expected test outcome.
        a.refresh_from_db(); b.refresh_from_db()  # Execute the test step.
        self.assertEqual(a.balance_due, 0)       # ₦79 fully settled
        self.assertEqual(b.balance_due, 4500)    # ₦56 − ₦11 = ₦45 remaining


class ReceiptAllocationEndpointTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """Receipts list/detail and explicit (and auto) allocation to open invoices."""

    def _super_admin(self, email):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment  # Import project symbols exercised by these tests.
        u = get_user_model().objects.create_user(  # Create test database data.
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Rcpt", last_name="Tester")  # Assign test setup data.
        role, _ = PlatformRoleTemplate.objects.get_or_create(id="xvs_super_admin", defaults={"name": "Super Admin"})  # Fetch test database data.
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")  # Create test database data.
        return u  # Return the prepared test value.

    def _unallocated_receipt(self, entity, customer, amount):  # Define a test helper or test method.
        import datetime  # Import dependency used by this test module.
        from vs_finance.models import Account, Payment  # Import project symbols exercised by these tests.
        from vs_finance.receivables import post_payment  # Import project symbols exercised by these tests.
        p = Payment.objects.create(  # Create test database data.
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 20),  # Continue structured test data.
            method="BANK_TRANSFER", amount=amount,  # Continue structured test data.
            deposit_account=Account.objects.get(entity=entity, code="1100"))  # Fetch test database data.
        post_payment(p, auto_allocate=False)   # posts Dr bank, Cr AR — left unallocated
        return p  # Return the prepared test value.

    def test_list_returns_unallocated_status(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import PaymentListView  # Import project symbols exercised by these tests.
        entity, _p, customer, _v = self.build_ar()  # Assign test setup data.
        self._unallocated_receipt(entity, customer, 9000)  # Execute the test step.
        u = self._super_admin("rcpt-list@test.com")  # Assign test setup data.
        req = APIRequestFactory().get("/v1/finance/payments/", {"entity": entity.code})  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = PaymentListView.as_view()(req); resp.render()  # Assign test setup data.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        row = json.loads(resp.content)["data"][0]  # Assign test setup data.
        self.assertEqual(row["allocation_status"], "UNALLOCATED")  # Check the expected test outcome.
        self.assertEqual(row["unallocated_amount"], 9000)  # Check the expected test outcome.

    def test_detail_has_open_invoices_and_postings(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import PaymentDetailView  # Import project symbols exercised by these tests.
        entity, _p, customer, _v = self.build_ar()  # Assign test setup data.
        a = self.make_invoice(entity, customer, lines=[("4100", 1, 7900, None)]); post_invoice(a)  # Assign test setup data.
        p = self._unallocated_receipt(entity, customer, 9000)  # Assign test setup data.
        u = self._super_admin("rcpt-detail@test.com")  # Assign test setup data.
        req = APIRequestFactory().get(f"/v1/finance/payments/{p.pk}/", {"entity": entity.code})  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = PaymentDetailView.as_view()(req, pk=p.pk); resp.render()  # Assign test setup data.
        d = json.loads(resp.content)["data"]  # Assign test setup data.
        self.assertTrue(d["open_invoices"])  # Check the expected test outcome.
        self.assertTrue(d["gl_postings"])   # the receipt's Dr bank / Cr AR

    def test_explicit_allocation_splits_across_invoices(self):  # Define a test helper or test method.
        import json  # Import dependency used by this test module.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import PaymentAllocateView  # Import project symbols exercised by these tests.
        entity, _p, customer, _v = self.build_ar()  # Assign test setup data.
        a = self.make_invoice(entity, customer, lines=[("4100", 1, 7900, None)]); post_invoice(a)  # Assign test setup data.
        b = self.make_invoice(entity, customer, lines=[("4100", 1, 5600, None)]); post_invoice(b)  # Assign test setup data.
        p = self._unallocated_receipt(entity, customer, 9000)  # Assign test setup data.
        u = self._super_admin("rcpt-alloc@test.com")  # Assign test setup data.
        body = {"allocations": [{"invoice": a.id, "amount": 7900}, {"invoice": b.id, "amount": 1100}]}  # Assign test setup data.
        req = APIRequestFactory().post(f"/v1/finance/payments/{p.pk}/allocate/?entity={entity.code}", body, format="json")  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = PaymentAllocateView.as_view()(req, pk=p.pk); resp.render()  # Assign test setup data.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        a.refresh_from_db(); b.refresh_from_db(); p.refresh_from_db()  # Execute the test step.
        self.assertEqual(a.balance_due, 0)  # Check the expected test outcome.
        self.assertEqual(b.balance_due, 4500)  # Check the expected test outcome.
        self.assertEqual(p.unallocated_amount, 0)  # Check the expected test outcome.

    def test_allocate_requires_permission(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_finance.views_ar import PaymentAllocateView  # Import project symbols exercised by these tests.
        entity, _p, customer, _v = self.build_ar()  # Assign test setup data.
        a = self.make_invoice(entity, customer, lines=[("4100", 1, 7900, None)]); post_invoice(a)  # Assign test setup data.
        p = self._unallocated_receipt(entity, customer, 9000)  # Assign test setup data.
        u = get_user_model().objects.create_user(  # Create test database data.
            email="rcpt-noperm@test.com", password="x", user_type="CX_STAFF",  # Continue structured test data.
            status="ACTIVE", first_name="No", last_name="Perm")  # Assign test setup data.
        req = APIRequestFactory().post(f"/v1/finance/payments/{p.pk}/allocate/?entity={entity.code}", {"auto_allocate": True}, format="json")  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = PaymentAllocateView.as_view()(req, pk=p.pk); resp.render()  # Assign test setup data.
        self.assertEqual(resp.status_code, 403)  # Check the expected test outcome.
        p.refresh_from_db()  # Execute the test step.
        self.assertEqual(p.unallocated_amount, 9000)  # Check the expected test outcome.


class DimensionAnalyticsTests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    """Analytical dimensions: constrained values, write-through to the GL, and the slice.

    Mirrors :class:`CostCenterPropagationTests` but for the second axis — the
    ``{axis: value}`` map carried on a journal line and the report that buckets by it.
    """

    def _axis(self, entity, *, code="FUND", values=("GRANT-A", "INTERNAL")):  # Define a test helper or test method.
        from vs_finance.models import Dimension  # Import project symbols exercised by these tests.
        return Dimension.objects.create(  # Return the prepared test value.
            entity=entity, code=code, name=code.title(), allowed_values=list(values),  # Continue structured test data.
        )  # Close the grouped test expression.

    def _spend(self, entity, *, amount, cost_center=None, dimensions=None,  # Define a test helper or test method.
               date=datetime.date(2026, 1, 10)):  # Start the nested test block.
        """Post a balanced direct entry: Dr 5500 expense / Cr 1100 bank."""
        from vs_finance.posting import post_direct_entry  # Import project symbols exercised by these tests.
        return post_direct_entry(  # Return the prepared test value.
            entity,  # Continue structured test data.
            lines=[  # Continue structured test data.
                ("5500", amount, 0, cost_center, dimensions or {}),  # Continue structured test data.
                ("1100", 0, amount, None, {}),  # Continue structured test data.
            ],  # Continue structured test data.
            date=date,  # Continue structured test data.
        )  # Close the grouped test expression.

    # --- resolver validation -------------------------------------------------
    def test_resolve_accepts_allowed_value(self):  # Define a test helper or test method.
        from vs_finance.views_ops import _resolve_dimensions  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self._axis(entity)  # Execute the test step.
        self.assertEqual(  # Check the expected test outcome.
            _resolve_dimensions(entity, {"FUND": "GRANT-A"}), {"FUND": "GRANT-A"})  # Execute the test step.

    def test_resolve_blank_yields_empty_map(self):  # Define a test helper or test method.
        from vs_finance.views_ops import _resolve_dimensions  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self.assertEqual(_resolve_dimensions(entity, None), {})  # Check the expected test outcome.
        self.assertEqual(_resolve_dimensions(entity, ""), {})  # Check the expected test outcome.
        self.assertEqual(_resolve_dimensions(entity, {}), {})  # Check the expected test outcome.

    def test_resolve_rejects_unknown_axis(self):  # Define a test helper or test method.
        from rest_framework.exceptions import ValidationError  # Import project symbols exercised by these tests.
        from vs_finance.views_ops import _resolve_dimensions  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self._axis(entity)  # Execute the test step.
        with self.assertRaises(ValidationError):  # Enter a test context manager.
            _resolve_dimensions(entity, {"NOPE": "GRANT-A"})  # Execute the test step.

    def test_resolve_rejects_value_not_in_allowlist(self):  # Define a test helper or test method.
        from rest_framework.exceptions import ValidationError  # Import project symbols exercised by these tests.
        from vs_finance.views_ops import _resolve_dimensions  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self._axis(entity)  # Execute the test step.
        with self.assertRaises(ValidationError):  # Enter a test context manager.
            _resolve_dimensions(entity, {"FUND": "GRANT-Z"})  # Execute the test step.

    def test_resolve_axis_with_no_values_rejects_all(self):  # Define a test helper or test method.
        from rest_framework.exceptions import ValidationError  # Import project symbols exercised by these tests.
        from vs_finance.views_ops import _resolve_dimensions  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self._axis(entity, code="EMPTY", values=())  # Assign test setup data.
        with self.assertRaises(ValidationError):  # Enter a test context manager.
            _resolve_dimensions(entity, {"EMPTY": "anything"})  # Execute the test step.

    def test_resolve_is_tenant_scoped(self):  # Define a test helper or test method.
        from rest_framework.exceptions import ValidationError  # Import project symbols exercised by these tests.
        from vs_finance.views_ops import _resolve_dimensions  # Import project symbols exercised by these tests.
        entity_a, _, _ = self.build_books()  # Assign test setup data.
        # A second tenant with its own FUND axis must not leak into entity A.
        entity_b = LedgerEntity.objects.create(  # Create test database data.
            name="Other Books", code="OBOOK", kind=LedgerEntity.Kind.TENANT)  # Assign test setup data.
        self._axis(entity_b)  # Execute the test step.
        with self.assertRaises(ValidationError):  # Enter a test context manager.
            _resolve_dimensions(entity_a, {"FUND": "GRANT-A"})  # Execute the test step.

    # --- write-through to the GL + reversal ----------------------------------
    def test_direct_entry_carries_dimensions_into_gl_and_reversal(self):  # Define a test helper or test method.
        from vs_finance.posting import reverse_journal  # Import project symbols exercised by these tests.
        from vs_finance.views_ops import _resolve_dimensions  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self._axis(entity)  # Execute the test step.
        dims = _resolve_dimensions(entity, {"FUND": "GRANT-A"})  # Assign test setup data.
        entry = self._spend(entity, amount=100000, dimensions=dims)  # Assign test setup data.

        exp = entry.lines.get(account__code="5500")  # Assign test setup data.
        self.assertEqual(exp.dimensions, {"FUND": "GRANT-A"})  # Check the expected test outcome.

        rev = reverse_journal(entry)  # Assign test setup data.
        self.assertEqual(rev.lines.get(account__code="5500").dimensions, {"FUND": "GRANT-A"})  # Check the expected test outcome.

    # --- the slice report ----------------------------------------------------
    def test_slice_groups_by_dimension_and_cost_centre(self):  # Define a test helper or test method.
        from vs_finance.models import CostCenter  # Import project symbols exercised by these tests.
        from vs_finance.reports import analytics_slice  # Import project symbols exercised by these tests.
        from vs_finance.views_ops import _resolve_dimensions  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self._axis(entity)  # Execute the test step.
        pri = CostCenter.objects.create(entity=entity, code="PRI", name="Primary")  # Create test database data.
        self._spend(entity, amount=100000, cost_center=pri,  # Continue structured test data.
                    dimensions=_resolve_dimensions(entity, {"FUND": "GRANT-A"}))  # Assign test setup data.
        self._spend(entity, amount=40000,  # Continue structured test data.
                    dimensions=_resolve_dimensions(entity, {"FUND": "INTERNAL"}),  # Continue structured test data.
                    date=datetime.date(2026, 1, 11))  # Assign test setup data.

        by_fund = analytics_slice(entity, axis="FUND")  # Assign test setup data.
        self.assertEqual(  # Check the expected test outcome.
            {r.bucket: r.net for r in by_fund.rows if r.code == "5500"},  # Continue structured test data.
            {"GRANT-A": 100000, "INTERNAL": 40000})  # Execute the test step.

        # Only cost-centre-tagged lines appear — the untagged 40,000 spend is excluded
        # (no "Unassigned" catch-all).
        by_cc = analytics_slice(entity, axis="cost_center")  # Assign test setup data.
        self.assertEqual(  # Check the expected test outcome.
            {r.bucket: r.net for r in by_cc.rows if r.code == "5500"},  # Continue structured test data.
            {"PRI": 100000})  # Execute the test step.

    def test_slice_period_scoping(self):  # Define a test helper or test method.
        from vs_finance.reports import analytics_slice  # Import project symbols exercised by these tests.
        from vs_finance.views_ops import _resolve_dimensions  # Import project symbols exercised by these tests.
        entity, _, periods = self.build_books()  # Assign test setup data.
        self._axis(entity)  # Execute the test step.
        self._spend(entity, amount=100000,  # Continue structured test data.
                    dimensions=_resolve_dimensions(entity, {"FUND": "GRANT-A"}),  # Continue structured test data.
                    date=datetime.date(2026, 1, 10))  # Assign test setup data.
        self._spend(entity, amount=40000,  # Continue structured test data.
                    dimensions=_resolve_dimensions(entity, {"FUND": "INTERNAL"}),  # Continue structured test data.
                    date=datetime.date(2026, 2, 10))  # Assign test setup data.

        jan = analytics_slice(entity, axis="FUND", period=periods[0])  # Assign test setup data.
        self.assertEqual(  # Check the expected test outcome.
            {r.bucket: r.net for r in jan.rows if r.code == "5500"}, {"GRANT-A": 100000})  # Execute the test step.

    def test_slice_empty_books_has_no_rows(self):  # Define a test helper or test method.
        from vs_finance.reports import analytics_slice  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        self._axis(entity)  # Execute the test step.
        sl = analytics_slice(entity, axis="FUND")  # Assign test setup data.
        self.assertEqual(sl.rows, [])  # Check the expected test outcome.
        self.assertEqual(sl.total_net, 0)  # Check the expected test outcome.


class DimensionAnalyticsAPITests(_Phase4FixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The dimensions CRUD + analytics-slice REST surface."""

    def setUp(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from rest_framework.test import APIClient  # Import project symbols exercised by these tests.
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment  # Import project symbols exercised by these tests.

        User = get_user_model()  # Assign test setup data.
        self.user = User.objects.create_user(  # Create test database data.
            email="dim-admin@test.com", password="testpass123",  # Continue structured test data.
            user_type="CX_STAFF", status="ACTIVE", first_name="Dim", last_name="Admin",  # Continue structured test data.
        )  # Close the grouped test expression.
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")  # Create test database data.
        PlatformUserRoleAssignment.objects.create(  # Create test database data.
            user=self.user, role=role, assignment_status="ACTIVE")  # Assign test setup data.
        self.client = APIClient()  # Assign test setup data.
        self.client.force_authenticate(user=self.user)  # Exercise the test HTTP client.

    def test_dimension_crud_persists_and_dedupes_values(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        r = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/dimensions/?entity={entity.code}",  # Continue structured test data.
            {"code": "FUND", "name": "Fund",  # Continue structured test data.
             "allowed_values": ["GRANT-A", "GRANT-A", "INTERNAL"]}, format="json")  # Assign test setup data.
        self.assertEqual(r.status_code, 201)  # Check the expected test outcome.
        self.assertEqual(r.json()["data"]["allowed_values"], ["GRANT-A", "INTERNAL"])  # Check the expected test outcome.
        # Re-POST upserts the axis and replaces the value list.
        r2 = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/dimensions/?entity={entity.code}",  # Continue structured test data.
            {"code": "FUND", "name": "Fund", "allowed_values": ["GRANT-B"]}, format="json")  # Assign test setup data.
        self.assertEqual(r2.status_code, 200)  # Check the expected test outcome.
        self.assertEqual(r2.json()["data"]["allowed_values"], ["GRANT-B"])  # Check the expected test outcome.

    def test_dimension_rejects_blank_value(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        r = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/dimensions/?entity={entity.code}",  # Continue structured test data.
            {"code": "FUND", "allowed_values": ["GRANT-A", "  "]}, format="json")  # Assign test setup data.
        self.assertEqual(r.status_code, 400)  # Check the expected test outcome.

    def test_analytics_slice_endpoint_buckets_activity(self):  # Define a test helper or test method.
        from vs_finance.posting import post_direct_entry  # Import project symbols exercised by these tests.
        from vs_finance.views_ops import _resolve_dimensions  # Import project symbols exercised by these tests.
        from vs_finance.models import Dimension  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        Dimension.objects.create(  # Create test database data.
            entity=entity, code="FUND", name="Fund", allowed_values=["GRANT-A"])  # Assign test setup data.
        post_direct_entry(  # Continue structured test data.
            entity,  # Continue structured test data.
            lines=[("5500", 100000, 0, None,  # Continue structured test data.
                    _resolve_dimensions(entity, {"FUND": "GRANT-A"})),  # Continue structured test data.
                   ("1100", 0, 100000, None, {})],  # Continue structured test data.
            date=datetime.date(2026, 1, 10))  # Assign test setup data.

        resp = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/reports/analytics-slice/?entity={entity.code}&axis=FUND")  # Assign test setup data.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(data["axis"], "FUND")  # Check the expected test outcome.
        exp = next(r for r in data["rows"] if r["code"] == "5500")  # Execute the test step.
        self.assertEqual(exp["bucket"], "GRANT-A")  # Check the expected test outcome.
        self.assertEqual(exp["net"]["kobo"], 100000)  # Check the expected test outcome.

    def test_analytics_slice_requires_valid_axis(self):  # Define a test helper or test method.
        entity, _, _ = self.build_books()  # Assign test setup data.
        missing = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/reports/analytics-slice/?entity={entity.code}")  # Assign test setup data.
        self.assertEqual(missing.status_code, 400)  # Check the expected test outcome.
        unknown = self.client.get(  # Exercise the test HTTP client.
            f"/v1/finance/reports/analytics-slice/?entity={entity.code}&axis=NOPE")  # Assign test setup data.
        self.assertEqual(unknown.status_code, 400)  # Check the expected test outcome.

    def test_analytics_slice_denied_without_report_permission(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from rest_framework.test import APIRequestFactory, force_authenticate  # Import project symbols exercised by these tests.
        from vs_finance.views import AnalyticsSliceView  # Import project symbols exercised by these tests.
        entity, _, _ = self.build_books()  # Assign test setup data.
        u = get_user_model().objects.create_user(  # Create test database data.
            email="dim-noperm@test.com", password="x", user_type="CX_STAFF",  # Continue structured test data.
            status="ACTIVE", first_name="No", last_name="Perm")  # Assign test setup data.
        req = APIRequestFactory().get(  # Continue structured test data.
            f"/v1/finance/reports/analytics-slice/?entity={entity.code}&axis=cost_center")  # Assign test setup data.
        force_authenticate(req, user=u)  # Assign test setup data.
        resp = AnalyticsSliceView.as_view()(req); resp.render()  # Assign test setup data.
        self.assertEqual(resp.status_code, 403)  # Check the expected test outcome.


class JournalApprovalWorkflowTests(_GLFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The journal approval slice: opt-in-by-template gating, SoD, and post-on-approve.

    Wires a manual JournalEntry into vs_workflow so that — when a template exists
    for ``finance.journal`` — GL posting happens only inside the engine's
    ``on_approved`` callback. When no template exists, direct posting is unchanged
    (regression guard). Covers the security-first cases from design §11.
    """

    APPROVE_KEY = "finance.journal.approve"  # Assign test setup data.

    def setUp(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from rest_framework.test import APIClient  # Import project symbols exercised by these tests.
        from vs_rbac.models import (  # Import project symbols exercised by these tests.
            PlatformRoleTemplate, PlatformUserRoleAssignment,  # Continue structured test data.
            SchoolRolePermission, SchoolRoleTemplate, SchoolUserRoleAssignment,  # Continue structured test data.
        )  # Close the grouped test expression.

        # The approver permission key must exist for the RBAC grant FK to resolve.
        import io  # Import dependency used by this test module.
        from django.core.management import call_command  # Import project symbols exercised by these tests.
        call_command("seed_finance_permissions", verbosity=0, stdout=io.StringIO())  # Assign test setup data.

        self.User = get_user_model()  # Assign test setup data.
        self.School = School  # Assign test setup data.
        self.SchoolRoleTemplate = SchoolRoleTemplate  # Assign test setup data.
        self.SchoolRolePermission = SchoolRolePermission  # Assign test setup data.
        self.SchoolUserRoleAssignment = SchoolUserRoleAssignment  # Assign test setup data.

        # A school-owned entity, so document.school resolves to a real school and the
        # engine's SCHOOL-scoped approver resolution has a pool to draw from.
        self.school = School.objects.create(name="Greenfield", slug="greenfield-jaw", code="GRNJAW")  # Create test database data.
        seed_currencies()  # Execute the test step.
        self.entity = LedgerEntity.objects.create(  # Create test database data.
            name="Greenfield Books", code="GRNBK", kind=LedgerEntity.Kind.TENANT,  # Continue structured test data.
            source_school=self.school,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_chart_of_accounts(self.entity)  # Execute the test step.
        self.year = FiscalYear.objects.create(  # Create test database data.
            entity=self.entity, year=2026,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        self.period = FiscalPeriod.objects.create(  # Create test database data.
            entity=self.entity, fiscal_year=self.year, period_no=1, name="Jan 2026",  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),  # Continue structured test data.
            status=PeriodStatus.OPEN,  # Continue structured test data.
        )  # Close the grouped test expression.

        # Requester: a CX super admin (bypasses the per-endpoint RBAC gate and sees
        # every entity). SoD still excludes them from approving their own journal.
        self.requester = self.User.objects.create_user(  # Create test database data.
            email="req-jaw@test.com", password="pw", user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Reqi", last_name="Ester",  # Continue structured test data.
        )  # Close the grouped test expression.
        super_role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")  # Create test database data.
        PlatformUserRoleAssignment.objects.create(  # Create test database data.
            user=self.requester, role=super_role, assignment_status="ACTIVE",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.client = APIClient()  # Assign test setup data.
        self.client.force_authenticate(user=self.requester)  # Exercise the test HTTP client.

    # --- fixtures ---------------------------------------------------------- #

    def _make_draft(self, *, debit=50000, period=None):  # Define a test helper or test method.
        """A balanced two-line DRAFT journal (Dr cash / Cr revenue)."""
        entry = JournalEntry.objects.create(  # Create test database data.
            entity=self.entity, date=datetime.date(2026, 1, 15),  # Continue structured test data.
            period=period or self.period, narration="approval test",  # Continue structured test data.
            created_by=self.requester,  # Continue structured test data.
        )  # Close the grouped test expression.
        cash = Account.objects.get(entity=self.entity, code="1100")  # Fetch test database data.
        rev = Account.objects.get(entity=self.entity, code="4100")  # Fetch test database data.
        JournalLine.objects.create(entry=entry, account=cash, debit=debit, credit=0, line_no=1)  # Create test database data.
        JournalLine.objects.create(entry=entry, account=rev, debit=0, credit=debit, line_no=2)  # Create test database data.
        return entry  # Return the prepared test value.

    def _publish_standard_template(self):  # Define a test helper or test method.
        from vs_workflow.services.templates import publish_template  # Import project symbols exercised by these tests.

        return publish_template(  # Return the prepared test value.
            school=self.school, branch=None,  # Continue structured test data.
            document_type="finance.journal", code="standard",  # Continue structured test data.
            name="Standard journal approval",  # Continue structured test data.
            stages_payload=[{  # Continue structured test data.
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",  # Continue structured test data.
                "order": 1, "approver_permission_key": self.APPROVE_KEY,  # Continue structured test data.
                "approver_scope": "SCHOOL", "advance_rule": "ANY",  # Continue structured test data.
                "on_rejection": "RETURN_TO_REQUESTER", "skip_if_no_approvers": False,  # Continue structured test data.
            }],  # Continue structured test data.
        )  # Close the grouped test expression.

    def _make_approver(self, email="apr-jaw@test.com"):  # Define a test helper or test method.
        """A school user holding finance.journal.approve at self.school."""
        user = self.User.objects.create_user(  # Create test database data.
            email=email, password="pw", user_type="SCHOOL_ADMIN", status="ACTIVE",  # Continue structured test data.
            first_name="Apro", last_name="Ver", school=self.school,  # Continue structured test data.
        )  # Close the grouped test expression.
        role, _ = self.SchoolRoleTemplate.objects.get_or_create(  # Fetch test database data.
            id="checker-role", defaults={"school": self.school, "name": "Journal Checker"},  # Continue structured test data.
        )  # Close the grouped test expression.
        self.SchoolRolePermission.objects.get_or_create(  # Fetch test database data.
            role=role, permission_id=self.APPROVE_KEY, defaults={"granted": True},  # Continue structured test data.
        )  # Close the grouped test expression.
        self.SchoolUserRoleAssignment.objects.create(  # Create test database data.
            school=self.school, user=user, role=role, assignment_status="ACTIVE",  # Continue structured test data.
        )  # Close the grouped test expression.
        return user  # Return the prepared test value.

    def _submit(self, entry):  # Define a test helper or test method.
        return self.client.post(  # Return the prepared test value.
            f"/v1/finance/journals/{entry.id}/submit/?entity={self.entity.code}", {}, format="json")  # Assign test setup data.

    def _post(self, entry):  # Define a test helper or test method.
        return self.client.post(  # Return the prepared test value.
            f"/v1/finance/journals/{entry.id}/post/?entity={self.entity.code}", {}, format="json")  # Assign test setup data.

    def _instance_for(self, entry):  # Define a test helper or test method.
        from vs_workflow.models import WorkflowInstance  # Import project symbols exercised by these tests.
        return WorkflowInstance.objects.for_document(entry).first()  # Return the prepared test value.

    # --- 1. Gate off: no template → direct post still works ---------------- #

    def test_gate_off_direct_post_still_works(self):  # Define a test helper or test method.
        from vs_finance.approvals import approval_required  # Import project symbols exercised by these tests.

        entry = self._make_draft()  # Assign test setup data.
        self.assertFalse(approval_required(entry))  # Check the expected test outcome.
        resp = self._post(entry)  # Assign test setup data.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            AccountBalance.objects.filter(  # Query test database data.
                account__entity=self.entity, period=self.period).exists())  # Assign test setup data.

    # --- 2. Gate on: template → direct post refused, submit → PENDING, GL untouched --- #

    def test_gate_on_direct_post_refused(self):  # Define a test helper or test method.
        self._publish_standard_template()  # Execute the test step.
        entry = self._make_draft()  # Assign test setup data.
        resp = self._post(entry)  # Assign test setup data.
        self.assertEqual(resp.status_code, 400, resp.content)  # Check the expected test outcome.
        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.DRAFT)  # Check the expected test outcome.

    def test_gate_on_submit_moves_to_pending_and_leaves_gl_untouched(self):  # Define a test helper or test method.
        self._publish_standard_template()  # Execute the test step.
        self._make_approver()  # keep the stage ACTIVE (do not auto-skip)
        entry = self._make_draft()  # Assign test setup data.
        resp = self._submit(entry)  # Assign test setup data.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.PENDING_APPROVAL)  # Check the expected test outcome.
        # GL is untouched: no POSTED status, no balance movement.
        self.assertFalse(JournalEntry.objects.filter(  # Check the expected test outcome.
            pk=entry.pk, status=DocumentStatus.POSTED).exists())  # Assign test setup data.
        self.assertFalse(AccountBalance.objects.filter(  # Check the expected test outcome.
            account__entity=self.entity, period=self.period).exists())  # Assign test setup data.

    # --- 3. SoD: requester cannot approve their own journal ---------------- #

    def test_requester_cannot_approve_own_journal(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.
        from vs_workflow.exceptions import (  # Import project symbols exercised by these tests.
            NotAnEligibleApproverError, RequesterCannotApproveError,  # Continue structured test data.
        )  # Close the grouped test expression.

        self._publish_standard_template()  # Execute the test step.
        self._make_approver()  # Execute the test step.
        entry = self._make_draft()  # Assign test setup data.
        self._submit(entry)  # Execute the test step.
        instance = self._instance_for(entry)  # Assign test setup data.
        # The requester is never on the eligible snapshot and is hard-blocked either
        # way — both are correct SoD outcomes.
        with self.assertRaises((RequesterCannotApproveError, NotAnEligibleApproverError)):  # Enter a test context manager.
            wf_actions.record_action(instance.id, self.requester, ActionEnum.APPROVED)  # Execute the test step.
        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.PENDING_APPROVAL)  # Check the expected test outcome.

    # --- 4. Happy path: a different approver approves → posts, posted_by == approver --- #

    def test_approval_posts_and_stamps_approver_as_poster(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.

        self._publish_standard_template()  # Execute the test step.
        approver = self._make_approver()  # Assign test setup data.
        entry = self._make_draft()  # Assign test setup data.
        self._submit(entry)  # Execute the test step.
        instance = self._instance_for(entry)  # Assign test setup data.

        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)  # Execute the test step.

        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertEqual(entry.posted_by_id, approver.id)         # Q2: poster == final approver
        self.assertEqual(entry.created_by_id, self.requester.id)  # Q2: maker unchanged
        self.assertTrue(AccountBalance.objects.filter(  # Check the expected test outcome.
            account__entity=self.entity, period=self.period).exists())  # Assign test setup data.

    # --- 5. Reject → DRAFT and Return → DRAFT ------------------------------ #

    def test_reject_returns_journal_to_draft(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.

        # A TERMINAL-on-rejection template so REJECTED ends the instance.
        from vs_workflow.services.templates import publish_template  # Import project symbols exercised by these tests.
        publish_template(  # Continue structured test data.
            school=self.school, branch=None,  # Continue structured test data.
            document_type="finance.journal", code="standard",  # Continue structured test data.
            name="Standard journal approval",  # Continue structured test data.
            stages_payload=[{  # Continue structured test data.
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",  # Continue structured test data.
                "order": 1, "approver_permission_key": self.APPROVE_KEY,  # Continue structured test data.
                "approver_scope": "SCHOOL", "advance_rule": "ANY",  # Continue structured test data.
                "on_rejection": "TERMINAL", "skip_if_no_approvers": False,  # Continue structured test data.
            }])  # Execute the test step.
        approver = self._make_approver()  # Assign test setup data.
        entry = self._make_draft()  # Assign test setup data.
        self._submit(entry)  # Execute the test step.
        instance = self._instance_for(entry)  # Assign test setup data.

        wf_actions.record_action(instance.id, approver, ActionEnum.REJECTED, comment="no")  # Assign test setup data.

        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.DRAFT)  # Check the expected test outcome.
        self.assertFalse(AccountBalance.objects.filter(  # Check the expected test outcome.
            account__entity=self.entity, period=self.period).exists())  # Assign test setup data.

    def test_return_sends_journal_back_to_draft(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.

        self._publish_standard_template()  # on_rejection=RETURN_TO_REQUESTER
        approver = self._make_approver()  # Assign test setup data.
        entry = self._make_draft()  # Assign test setup data.
        self._submit(entry)  # Execute the test step.
        instance = self._instance_for(entry)  # Assign test setup data.

        wf_actions.record_action(instance.id, approver, ActionEnum.RETURNED, comment="fix narration")  # Assign test setup data.

        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.DRAFT)  # Check the expected test outcome.

    # --- 6. Posting failure at approval time (Option A rollback) ----------- #

    def test_posting_failure_at_approval_rolls_back_and_keeps_stage_active(self):  # Define a test helper or test method.
        from vs_workflow.constants import WorkflowInstanceStatus, WorkflowStageStatus  # Import project symbols exercised by these tests.
        from vs_finance.exceptions import PeriodClosedError  # Import project symbols exercised by these tests.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.
        from vs_workflow.models import WorkflowStageAction  # Import project symbols exercised by these tests.

        self._publish_standard_template()  # Execute the test step.
        approver = self._make_approver()  # Assign test setup data.
        entry = self._make_draft()  # Assign test setup data.
        self._submit(entry)  # preflight passes while the period is OPEN
        instance = self._instance_for(entry)  # Assign test setup data.

        # The period closes while the journal sits in the queue → posting must fail.
        self.period.status = PeriodStatus.CLOSED  # Assign test setup data.
        self.period.save(update_fields=["status"])  # Assign test setup data.

        with self.assertRaises(PeriodClosedError):  # Enter a test context manager.
            wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)  # Execute the test step.

        # Option A: the approval action rolled back — journal not POSTED, and the
        # stage is still ACTIVE for a retry once the period reopens.
        entry.refresh_from_db()  # Execute the test step.
        self.assertNotEqual(entry.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertFalse(AccountBalance.objects.filter(  # Check the expected test outcome.
            account__entity=self.entity, period=self.period).exists())  # Assign test setup data.
        self.assertFalse(WorkflowStageAction.objects.filter(  # Check the expected test outcome.
            stage_instance__instance=instance, action=ActionEnum.APPROVED,  # Continue structured test data.
            reversed_at__isnull=True, is_reversal_of__isnull=True).exists())  # Assign test setup data.
        instance.refresh_from_db()  # Execute the test step.
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            instance.stage_instances.filter(status=WorkflowStageStatus.ACTIVE).exists())  # Assign test setup data.

        # Retry succeeds once the period reopens.
        self.period.status = PeriodStatus.OPEN  # Assign test setup data.
        self.period.save(update_fields=["status"])  # Assign test setup data.
        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)  # Execute the test step.
        entry.refresh_from_db()  # Execute the test step.
        self.assertEqual(entry.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertEqual(entry.posted_by_id, approver.id)  # Check the expected test outcome.


class RefundApprovalWorkflowTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The refund approval slice: opt-in-by-template gating, SoD, and payout-on-approve.

    Wires a customer :class:`Refund` into vs_workflow so that — when a template
    exists for ``finance.refund`` — the cash payout happens only inside the engine's
    ``on_approved`` callback (``credit_notes.post_refund``). With no template, direct
    posting is unchanged. Reuses the same RBAC/user/template fixture shape as the
    journal slice; a refund needs a customer holding available credit, seated here by
    posting a standalone over-payment (books to customer-credit 2140).
    """

    APPROVE_KEY = "finance.refund.approve"  # Assign test setup data.

    def setUp(self):  # Define a test helper or test method.
        import io  # Import dependency used by this test module.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from django.core.management import call_command  # Import project symbols exercised by these tests.
        from rest_framework.test import APIClient  # Import project symbols exercised by these tests.
        from vs_rbac.models import (  # Import project symbols exercised by these tests.
            PlatformRoleTemplate, PlatformUserRoleAssignment,  # Continue structured test data.
            SchoolRolePermission, SchoolRoleTemplate, SchoolUserRoleAssignment,  # Continue structured test data.
        )  # Close the grouped test expression.

        # The approver permission key must exist for the RBAC grant FK to resolve.
        call_command("seed_finance_permissions", verbosity=0, stdout=io.StringIO())  # Assign test setup data.

        self.User = get_user_model()  # Assign test setup data.
        self.SchoolRoleTemplate = SchoolRoleTemplate  # Assign test setup data.
        self.SchoolRolePermission = SchoolRolePermission  # Assign test setup data.
        self.SchoolUserRoleAssignment = SchoolUserRoleAssignment  # Assign test setup data.

        # A school-owned entity, so refund.school resolves to a real school and the
        # engine's SCHOOL-scoped approver resolution has a pool to draw from.
        self.school = School.objects.create(name="Riverside", slug="riverside-raw", code="RVRAW")  # Create test database data.
        seed_currencies()  # Execute the test step.
        self.entity = LedgerEntity.objects.create(  # Create test database data.
            name="Riverside Books", code="RVRBK", kind=LedgerEntity.Kind.TENANT,  # Continue structured test data.
            source_school=self.school,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_chart_of_accounts(self.entity)  # Execute the test step.
        self.year = FiscalYear.objects.create(  # Create test database data.
            entity=self.entity, year=2026,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        self.period = FiscalPeriod.objects.create(  # Create test database data.
            entity=self.entity, fiscal_year=self.year, period_no=1, name="Jan 2026",  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),  # Continue structured test data.
            status=PeriodStatus.OPEN,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.bank = Account.objects.get(entity=self.entity, code="1100")  # Fetch test database data.
        self.customer = Customer.objects.create(  # Create test database data.
            entity=self.entity, code="CUSTR", name="Payer Ltd",  # Continue structured test data.
            receivable_account=Account.objects.get(entity=self.entity, code="1200"),  # Fetch test database data.
        )  # Close the grouped test expression.

        # Requester: a CX super admin (bypasses the per-endpoint RBAC gate, sees every
        # entity). SoD still excludes them from approving their own refund.
        self.requester = self.User.objects.create_user(  # Create test database data.
            email="req-raw@test.com", password="pw", user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Reqi", last_name="Ester",  # Continue structured test data.
        )  # Close the grouped test expression.
        super_role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")  # Create test database data.
        PlatformUserRoleAssignment.objects.create(  # Create test database data.
            user=self.requester, role=super_role, assignment_status="ACTIVE",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.client = APIClient()  # Assign test setup data.
        self.client.force_authenticate(user=self.requester)  # Exercise the test HTTP client.

    # --- fixtures ---------------------------------------------------------- #

    def _seat_credit(self, amount):  # Define a test helper or test method.
        """Seat ``amount`` kobo of available customer credit via a standalone payment.

        A receipt with no open invoices books its whole amount to the customer-credit
        liability (2140) — exactly what a refund pays back out.
        """
        pay = Payment.objects.create(  # Create test database data.
            entity=self.entity, customer=self.customer,  # Continue structured test data.
            payment_date=datetime.date(2026, 1, 5), amount=amount, deposit_account=self.bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # Execute the test step.
        self.assertEqual(customer_credit_balance(self.customer), amount)  # Check the expected test outcome.

    def _make_draft_refund(self, *, amount=30000):  # Define a test helper or test method.
        return Refund.objects.create(  # Return the prepared test value.
            entity=self.entity, customer=self.customer,  # Continue structured test data.
            refund_date=datetime.date(2026, 1, 18), amount=amount,  # Continue structured test data.
            deposit_account=self.bank, created_by=self.requester,  # Continue structured test data.
        )  # Close the grouped test expression.

    def _publish_standard_template(self, *, on_rejection="RETURN_TO_REQUESTER"):  # Define a test helper or test method.
        from vs_workflow.services.templates import publish_template  # Import project symbols exercised by these tests.

        return publish_template(  # Return the prepared test value.
            school=self.school, branch=None,  # Continue structured test data.
            document_type="finance.refund", code="standard",  # Continue structured test data.
            name="Standard refund approval",  # Continue structured test data.
            stages_payload=[{  # Continue structured test data.
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",  # Continue structured test data.
                "order": 1, "approver_permission_key": self.APPROVE_KEY,  # Continue structured test data.
                "approver_scope": "SCHOOL", "advance_rule": "ANY",  # Continue structured test data.
                "on_rejection": on_rejection, "skip_if_no_approvers": False,  # Continue structured test data.
            }])  # Execute the test step.

    def _make_approver(self, email="apr-raw@test.com"):  # Define a test helper or test method.
        user = self.User.objects.create_user(  # Create test database data.
            email=email, password="pw", user_type="SCHOOL_ADMIN", status="ACTIVE",  # Continue structured test data.
            first_name="Apro", last_name="Ver", school=self.school,  # Continue structured test data.
        )  # Close the grouped test expression.
        role, _ = self.SchoolRoleTemplate.objects.get_or_create(  # Fetch test database data.
            id="refund-checker-role", defaults={"school": self.school, "name": "Refund Checker"},  # Continue structured test data.
        )  # Close the grouped test expression.
        self.SchoolRolePermission.objects.get_or_create(  # Fetch test database data.
            role=role, permission_id=self.APPROVE_KEY, defaults={"granted": True},  # Continue structured test data.
        )  # Close the grouped test expression.
        self.SchoolUserRoleAssignment.objects.create(  # Create test database data.
            school=self.school, user=user, role=role, assignment_status="ACTIVE",  # Continue structured test data.
        )  # Close the grouped test expression.
        return user  # Return the prepared test value.

    def _submit(self, refund):  # Define a test helper or test method.
        return self.client.post(  # Return the prepared test value.
            f"/v1/finance/refunds/{refund.pk}/submit/?entity={self.entity.code}", {}, format="json")  # Assign test setup data.

    def _post(self, refund):  # Define a test helper or test method.
        return self.client.post(  # Return the prepared test value.
            f"/v1/finance/refunds/{refund.pk}/post/?entity={self.entity.code}", {}, format="json")  # Assign test setup data.

    def _instance_for(self, refund):  # Define a test helper or test method.
        from vs_workflow.models import WorkflowInstance  # Import project symbols exercised by these tests.
        return WorkflowInstance.objects.for_document(refund).first()  # Return the prepared test value.

    # --- 1. Gate off: no template → direct post still works ---------------- #

    def test_gate_off_direct_post_still_works(self):  # Define a test helper or test method.
        from vs_finance.approvals import approval_required  # Import project symbols exercised by these tests.

        self._seat_credit(30000)  # Execute the test step.
        refund = self._make_draft_refund(amount=30000)  # Assign test setup data.
        self.assertFalse(approval_required(refund))  # Check the expected test outcome.
        resp = self._post(refund)  # Assign test setup data.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        refund.refresh_from_db()  # Execute the test step.
        self.assertEqual(refund.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertIsNotNone(refund.journal_id)  # Check the expected test outcome.

    # --- 2. Gate on: direct post refused ----------------------------------- #

    def test_gate_on_direct_post_refused(self):  # Define a test helper or test method.
        self._seat_credit(30000)  # Execute the test step.
        self._publish_standard_template()  # Execute the test step.
        refund = self._make_draft_refund(amount=30000)  # Assign test setup data.
        resp = self._post(refund)  # Assign test setup data.
        self.assertEqual(resp.status_code, 400, resp.content)  # Check the expected test outcome.
        refund.refresh_from_db()  # Execute the test step.
        self.assertEqual(refund.status, DocumentStatus.DRAFT)  # Check the expected test outcome.
        self.assertIsNone(refund.journal_id)  # Check the expected test outcome.

    # --- 3. Gate on: submit → PENDING, no refund journal posted ------------ #

    def test_gate_on_submit_moves_to_pending_and_no_payout(self):  # Define a test helper or test method.
        self._seat_credit(30000)  # Execute the test step.
        self._publish_standard_template()  # Execute the test step.
        self._make_approver()  # keep the stage ACTIVE (do not auto-skip)
        refund = self._make_draft_refund(amount=30000)  # Assign test setup data.
        resp = self._submit(refund)  # Assign test setup data.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        refund.refresh_from_db()  # Execute the test step.
        self.assertEqual(refund.status, DocumentStatus.PENDING_APPROVAL)  # Check the expected test outcome.
        self.assertIsNone(refund.journal_id)  # Check the expected test outcome.
        # The credit is untouched until approval.
        self.assertEqual(customer_credit_balance(self.customer), 30000)  # Check the expected test outcome.

    # --- 4. SoD: requester cannot approve own refund ----------------------- #

    def test_requester_cannot_approve_own_refund(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.
        from vs_workflow.exceptions import (  # Import project symbols exercised by these tests.
            NotAnEligibleApproverError, RequesterCannotApproveError,  # Continue structured test data.
        )  # Close the grouped test expression.

        self._seat_credit(30000)  # Execute the test step.
        self._publish_standard_template()  # Execute the test step.
        self._make_approver()  # Execute the test step.
        refund = self._make_draft_refund(amount=30000)  # Assign test setup data.
        self._submit(refund)  # Execute the test step.
        instance = self._instance_for(refund)  # Assign test setup data.
        with self.assertRaises((RequesterCannotApproveError, NotAnEligibleApproverError)):  # Enter a test context manager.
            wf_actions.record_action(instance.id, self.requester, ActionEnum.APPROVED)  # Execute the test step.
        refund.refresh_from_db()  # Execute the test step.
        self.assertEqual(refund.status, DocumentStatus.PENDING_APPROVAL)  # Check the expected test outcome.

    # --- 5. Happy path: approver approves → post_refund runs, refund POSTED --- #

    def test_approval_pays_out_and_posts_refund(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.

        self._seat_credit(30000)  # Execute the test step.
        self._publish_standard_template()  # Execute the test step.
        approver = self._make_approver()  # Assign test setup data.
        refund = self._make_draft_refund(amount=30000)  # Assign test setup data.
        self._submit(refund)  # Execute the test step.
        instance = self._instance_for(refund)  # Assign test setup data.

        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)  # Execute the test step.

        refund.refresh_from_db()  # Execute the test step.
        self.assertEqual(refund.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertIsNotNone(refund.journal_id)                   # payout journal linked
        self.assertEqual(refund.journal.posted_by_id, approver.id)  # posted by the approver
        self.assertEqual(customer_credit_balance(self.customer), 0)  # credit paid out

    # --- 6. Reject → DRAFT and Return → DRAFT ------------------------------ #

    def test_reject_returns_refund_to_draft(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.

        self._seat_credit(30000)  # Execute the test step.
        self._publish_standard_template(on_rejection="TERMINAL")  # Assign test setup data.
        approver = self._make_approver()  # Assign test setup data.
        refund = self._make_draft_refund(amount=30000)  # Assign test setup data.
        self._submit(refund)  # Execute the test step.
        instance = self._instance_for(refund)  # Assign test setup data.

        wf_actions.record_action(instance.id, approver, ActionEnum.REJECTED, comment="no")  # Assign test setup data.

        refund.refresh_from_db()  # Execute the test step.
        self.assertEqual(refund.status, DocumentStatus.DRAFT)  # Check the expected test outcome.
        self.assertIsNone(refund.journal_id)  # Check the expected test outcome.
        self.assertEqual(customer_credit_balance(self.customer), 30000)  # Check the expected test outcome.

    def test_return_sends_refund_back_to_draft(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.

        self._seat_credit(30000)  # Execute the test step.
        self._publish_standard_template()  # on_rejection=RETURN_TO_REQUESTER
        approver = self._make_approver()  # Assign test setup data.
        refund = self._make_draft_refund(amount=30000)  # Assign test setup data.
        self._submit(refund)  # Execute the test step.
        instance = self._instance_for(refund)  # Assign test setup data.

        wf_actions.record_action(instance.id, approver, ActionEnum.RETURNED, comment="wrong account")  # Assign test setup data.

        refund.refresh_from_db()  # Execute the test step.
        self.assertEqual(refund.status, DocumentStatus.DRAFT)  # Check the expected test outcome.

    # --- 7. Option-A rollback: credit drained after submit ----------------- #

    def test_posting_failure_at_approval_rolls_back_and_keeps_stage_active(self):  # Define a test helper or test method.
        from vs_workflow.constants import (  # Import project symbols exercised by these tests.
            WorkflowInstanceStatus, WorkflowStageAction as ActionEnum, WorkflowStageStatus,  # Continue structured test data.
        )  # Close the grouped test expression.
        from vs_finance.exceptions import PostingError  # Import project symbols exercised by these tests.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.models import WorkflowStageAction  # Import project symbols exercised by these tests.

        # Seat exactly enough credit for one refund; submit passes preflight.
        self._seat_credit(30000)  # Execute the test step.
        self._publish_standard_template()  # Execute the test step.
        approver = self._make_approver()  # Assign test setup data.
        refund = self._make_draft_refund(amount=30000)  # Assign test setup data.
        self._submit(refund)  # Execute the test step.
        instance = self._instance_for(refund)  # Assign test setup data.

        # Drain the customer's available credit while the refund sits in the queue by
        # paying it out through a second, directly-posted refund (no template gate on
        # that path yet — it's the same entity, but we bypass via the service). After
        # this, post_refund on the queued refund must exceed available credit.
        drain = Refund.objects.create(  # Create test database data.
            entity=self.entity, customer=self.customer, refund_date=datetime.date(2026, 1, 6),  # Continue structured test data.
            amount=30000, deposit_account=self.bank, created_by=self.requester,  # Continue structured test data.
        )  # Close the grouped test expression.
        from vs_finance.credit_notes import post_refund  # Import project symbols exercised by these tests.
        post_refund(drain, actor_user=self.requester)  # Assign test setup data.
        self.assertEqual(customer_credit_balance(self.customer), 0)  # Check the expected test outcome.

        with self.assertRaises(PostingError):  # Enter a test context manager.
            wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)  # Execute the test step.

        # Option A: the approval action rolled back — refund not POSTED, no journal,
        # and the stage is still ACTIVE for a retry.
        refund.refresh_from_db()  # Execute the test step.
        self.assertNotEqual(refund.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertIsNone(refund.journal_id)  # Check the expected test outcome.
        self.assertFalse(WorkflowStageAction.objects.filter(  # Check the expected test outcome.
            stage_instance__instance=instance, action=ActionEnum.APPROVED,  # Continue structured test data.
            reversed_at__isnull=True, is_reversal_of__isnull=True).exists())  # Assign test setup data.
        instance.refresh_from_db()  # Execute the test step.
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            instance.stage_instances.filter(status=WorkflowStageStatus.ACTIVE).exists())  # Assign test setup data.

        # Retry succeeds once credit is re-seated.
        self._seat_credit(30000)  # Execute the test step.
        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)  # Execute the test step.
        refund.refresh_from_db()  # Execute the test step.
        self.assertEqual(refund.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertEqual(refund.journal.posted_by_id, approver.id)  # Check the expected test outcome.


class WriteOffRequestApprovalWorkflowTests(_ARFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The bad-debt write-off approval slice: opt-in gating, SoD, write-off-on-approve.

    Wires the first-class :class:`WriteOffRequest` document into vs_workflow so that —
    when a template exists for ``finance.write_off`` — the invoice write-off happens
    only inside ``on_approved`` (``credit_notes.write_off_invoice``, unchanged). With
    no template, the direct-post path (and the invoice-write-off bridge) is unchanged.
    Reuses the same RBAC/user/template fixture shape as the refund slice; needs a
    POSTED invoice with an outstanding balance.
    """

    APPROVE_KEY = "finance.writeoff.approve"  # Assign test setup data.

    def setUp(self):  # Define a test helper or test method.
        import io  # Import dependency used by this test module.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from django.core.management import call_command  # Import project symbols exercised by these tests.
        from rest_framework.test import APIClient  # Import project symbols exercised by these tests.
        from vs_rbac.models import (  # Import project symbols exercised by these tests.
            PlatformRoleTemplate, PlatformUserRoleAssignment,  # Continue structured test data.
            SchoolRolePermission, SchoolRoleTemplate, SchoolUserRoleAssignment,  # Continue structured test data.
        )  # Close the grouped test expression.

        call_command("seed_finance_permissions", verbosity=0, stdout=io.StringIO())  # Assign test setup data.

        self.User = get_user_model()  # Assign test setup data.
        self.SchoolRoleTemplate = SchoolRoleTemplate  # Assign test setup data.
        self.SchoolRolePermission = SchoolRolePermission  # Assign test setup data.
        self.SchoolUserRoleAssignment = SchoolUserRoleAssignment  # Assign test setup data.

        # School-owned entity, so write_off_request.school resolves to a real school.
        self.school = School.objects.create(name="Lakeside", slug="lakeside-woa", code="LKSWO")  # Create test database data.
        seed_currencies()  # Execute the test step.
        self.entity = LedgerEntity.objects.create(  # Create test database data.
            name="Lakeside Books", code="LKSBK", kind=LedgerEntity.Kind.TENANT,  # Continue structured test data.
            source_school=self.school,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_chart_of_accounts(self.entity)  # Execute the test step.
        self.year = FiscalYear.objects.create(  # Create test database data.
            entity=self.entity, year=2026,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        self.period = FiscalPeriod.objects.create(  # Create test database data.
            entity=self.entity, fiscal_year=self.year, period_no=1, name="Jan 2026",  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),  # Continue structured test data.
            status=PeriodStatus.OPEN,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.customer = Customer.objects.create(  # Create test database data.
            entity=self.entity, code="CUSTW", name="Debtor Ltd",  # Continue structured test data.
            receivable_account=Account.objects.get(entity=self.entity, code="1200"),  # Fetch test database data.
        )  # Close the grouped test expression.

        self.requester = self.User.objects.create_user(  # Create test database data.
            email="req-woa@test.com", password="pw", user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Reqi", last_name="Ester",  # Continue structured test data.
        )  # Close the grouped test expression.
        super_role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")  # Create test database data.
        PlatformUserRoleAssignment.objects.create(  # Create test database data.
            user=self.requester, role=super_role, assignment_status="ACTIVE",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.client = APIClient()  # Assign test setup data.
        self.client.force_authenticate(user=self.requester)  # Exercise the test HTTP client.

    # --- fixtures ---------------------------------------------------------- #

    def _posted_invoice(self, *, unit_price=100000):  # Define a test helper or test method.
        """A POSTED invoice with a full outstanding balance (no tax, unpaid)."""
        inv = Invoice.objects.create(  # Create test database data.
            entity=self.entity, customer=self.customer,  # Continue structured test data.
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 25),  # Continue structured test data.
        )  # Close the grouped test expression.
        InvoiceLine.objects.create(  # Create test database data.
            invoice=inv, revenue_account=Account.objects.get(entity=self.entity, code="4100"),  # Fetch test database data.
            quantity=1, unit_price=unit_price, tax_code=None, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_invoice(inv)  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.
        return inv  # Return the prepared test value.

    def _make_request(self, invoice, *, amount=None):  # Define a test helper or test method.
        from vs_finance.models import WriteOffRequest  # Import project symbols exercised by these tests.
        return WriteOffRequest.objects.create(  # Return the prepared test value.
            entity=self.entity, invoice=invoice,  # Continue structured test data.
            amount=amount if amount is not None else invoice.balance_due,  # Continue structured test data.
            write_off_date=datetime.date(2026, 1, 20), reason="uncollectable",  # Continue structured test data.
            created_by=self.requester,  # Continue structured test data.
        )  # Close the grouped test expression.

    def _publish_standard_template(self, *, on_rejection="RETURN_TO_REQUESTER"):  # Define a test helper or test method.
        from vs_workflow.services.templates import publish_template  # Import project symbols exercised by these tests.

        return publish_template(  # Return the prepared test value.
            school=self.school, branch=None,  # Continue structured test data.
            document_type="finance.write_off", code="standard",  # Continue structured test data.
            name="Standard write-off approval",  # Continue structured test data.
            stages_payload=[{  # Continue structured test data.
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",  # Continue structured test data.
                "order": 1, "approver_permission_key": self.APPROVE_KEY,  # Continue structured test data.
                "approver_scope": "SCHOOL", "advance_rule": "ANY",  # Continue structured test data.
                "on_rejection": on_rejection, "skip_if_no_approvers": False,  # Continue structured test data.
            }])  # Execute the test step.

    def _make_approver(self, email="apr-woa@test.com"):  # Define a test helper or test method.
        user = self.User.objects.create_user(  # Create test database data.
            email=email, password="pw", user_type="SCHOOL_ADMIN", status="ACTIVE",  # Continue structured test data.
            first_name="Apro", last_name="Ver", school=self.school,  # Continue structured test data.
        )  # Close the grouped test expression.
        role, _ = self.SchoolRoleTemplate.objects.get_or_create(  # Fetch test database data.
            id="writeoff-checker-role", defaults={"school": self.school, "name": "Write-off Checker"},  # Continue structured test data.
        )  # Close the grouped test expression.
        self.SchoolRolePermission.objects.get_or_create(  # Fetch test database data.
            role=role, permission_id=self.APPROVE_KEY, defaults={"granted": True},  # Continue structured test data.
        )  # Close the grouped test expression.
        self.SchoolUserRoleAssignment.objects.create(  # Create test database data.
            school=self.school, user=user, role=role, assignment_status="ACTIVE",  # Continue structured test data.
        )  # Close the grouped test expression.
        return user  # Return the prepared test value.

    def _submit(self, wor):  # Define a test helper or test method.
        return self.client.post(  # Return the prepared test value.
            f"/v1/finance/write-offs/{wor.pk}/submit/?entity={self.entity.code}", {}, format="json")  # Assign test setup data.

    def _post(self, wor):  # Define a test helper or test method.
        return self.client.post(  # Return the prepared test value.
            f"/v1/finance/write-offs/{wor.pk}/post/?entity={self.entity.code}", {}, format="json")  # Assign test setup data.

    def _instance_for(self, wor):  # Define a test helper or test method.
        from vs_workflow.models import WorkflowInstance  # Import project symbols exercised by these tests.
        return WorkflowInstance.objects.for_document(wor).first()  # Return the prepared test value.

    # --- 1. Gate off: direct post writes the invoice off ------------------- #

    def test_gate_off_direct_post_writes_off(self):  # Define a test helper or test method.
        from vs_finance.approvals import approval_required  # Import project symbols exercised by these tests.

        inv = self._posted_invoice()  # Assign test setup data.
        wor = self._make_request(inv)  # Assign test setup data.
        self.assertFalse(approval_required(wor))  # Check the expected test outcome.
        resp = self._post(wor)  # Assign test setup data.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        wor.refresh_from_db(); inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(wor.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertIsNotNone(wor.journal_id)  # Check the expected test outcome.
        self.assertEqual(inv.amount_credited, 100000)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 0)  # Check the expected test outcome.

    # --- 2. Gate on: direct post refused ----------------------------------- #

    def test_gate_on_direct_post_refused(self):  # Define a test helper or test method.
        self._publish_standard_template()  # Execute the test step.
        inv = self._posted_invoice()  # Assign test setup data.
        wor = self._make_request(inv)  # Assign test setup data.
        resp = self._post(wor)  # Assign test setup data.
        self.assertEqual(resp.status_code, 400, resp.content)  # Check the expected test outcome.
        wor.refresh_from_db(); inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(wor.status, DocumentStatus.DRAFT)  # Check the expected test outcome.
        self.assertIsNone(wor.journal_id)  # Check the expected test outcome.
        self.assertEqual(inv.amount_credited, 0)  # Check the expected test outcome.

    # --- 3. Gate on: submit → PENDING, invoice untouched ------------------- #

    def test_gate_on_submit_moves_to_pending_and_invoice_untouched(self):  # Define a test helper or test method.
        self._publish_standard_template()  # Execute the test step.
        self._make_approver()  # Execute the test step.
        inv = self._posted_invoice()  # Assign test setup data.
        wor = self._make_request(inv)  # Assign test setup data.
        resp = self._submit(wor)  # Assign test setup data.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        wor.refresh_from_db(); inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(wor.status, DocumentStatus.PENDING_APPROVAL)  # Check the expected test outcome.
        self.assertIsNone(wor.journal_id)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 100000)  # Check the expected test outcome.

    # --- 4. SoD: requester cannot approve own request ---------------------- #

    def test_requester_cannot_approve_own_request(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.
        from vs_workflow.exceptions import (  # Import project symbols exercised by these tests.
            NotAnEligibleApproverError, RequesterCannotApproveError,  # Continue structured test data.
        )  # Close the grouped test expression.

        self._publish_standard_template()  # Execute the test step.
        self._make_approver()  # Execute the test step.
        inv = self._posted_invoice()  # Assign test setup data.
        wor = self._make_request(inv)  # Assign test setup data.
        self._submit(wor)  # Execute the test step.
        instance = self._instance_for(wor)  # Assign test setup data.
        with self.assertRaises((RequesterCannotApproveError, NotAnEligibleApproverError)):  # Enter a test context manager.
            wf_actions.record_action(instance.id, self.requester, ActionEnum.APPROVED)  # Execute the test step.
        wor.refresh_from_db()  # Execute the test step.
        self.assertEqual(wor.status, DocumentStatus.PENDING_APPROVAL)  # Check the expected test outcome.

    # --- 5. Happy path: approver approves → invoice written off ------------ #

    def test_approval_writes_off_and_posts_request(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.

        self._publish_standard_template()  # Execute the test step.
        approver = self._make_approver()  # Assign test setup data.
        inv = self._posted_invoice()  # Assign test setup data.
        wor = self._make_request(inv)  # Assign test setup data.
        self._submit(wor)  # Execute the test step.
        instance = self._instance_for(wor)  # Assign test setup data.

        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)  # Execute the test step.

        wor.refresh_from_db(); inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(wor.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertIsNotNone(wor.journal_id)  # Check the expected test outcome.
        self.assertEqual(wor.journal.posted_by_id, approver.id)  # Check the expected test outcome.
        self.assertEqual(inv.amount_credited, 100000)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 0)  # Check the expected test outcome.

    # --- 6. Reject → DRAFT and Return → DRAFT ------------------------------ #

    def test_reject_returns_request_to_draft(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.

        self._publish_standard_template(on_rejection="TERMINAL")  # Assign test setup data.
        approver = self._make_approver()  # Assign test setup data.
        inv = self._posted_invoice()  # Assign test setup data.
        wor = self._make_request(inv)  # Assign test setup data.
        self._submit(wor)  # Execute the test step.
        instance = self._instance_for(wor)  # Assign test setup data.

        wf_actions.record_action(instance.id, approver, ActionEnum.REJECTED, comment="no")  # Assign test setup data.

        wor.refresh_from_db(); inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(wor.status, DocumentStatus.DRAFT)  # Check the expected test outcome.
        self.assertIsNone(wor.journal_id)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 100000)  # Check the expected test outcome.

    def test_return_sends_request_back_to_draft(self):  # Define a test helper or test method.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.constants import WorkflowStageAction as ActionEnum  # Import project symbols exercised by these tests.

        self._publish_standard_template()  # RETURN_TO_REQUESTER
        approver = self._make_approver()  # Assign test setup data.
        inv = self._posted_invoice()  # Assign test setup data.
        wor = self._make_request(inv)  # Assign test setup data.
        self._submit(wor)  # Execute the test step.
        instance = self._instance_for(wor)  # Assign test setup data.

        wf_actions.record_action(instance.id, approver, ActionEnum.RETURNED, comment="wrong amount")  # Assign test setup data.

        wor.refresh_from_db(); inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(wor.status, DocumentStatus.DRAFT)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 100000)  # Check the expected test outcome.

    # --- 7. Option-A rollback: invoice settled after submit ---------------- #

    def test_posting_failure_at_approval_rolls_back_and_keeps_stage_active(self):  # Define a test helper or test method.
        from vs_workflow.constants import (  # Import project symbols exercised by these tests.
            WorkflowInstanceStatus, WorkflowStageAction as ActionEnum, WorkflowStageStatus,  # Continue structured test data.
        )  # Close the grouped test expression.
        from vs_finance.exceptions import PostingError  # Import project symbols exercised by these tests.
        from vs_workflow.services import actions as wf_actions  # Import project symbols exercised by these tests.
        from vs_workflow.models import WorkflowStageAction  # Import project symbols exercised by these tests.

        self._publish_standard_template()  # Execute the test step.
        approver = self._make_approver()  # Assign test setup data.
        inv = self._posted_invoice()  # Assign test setup data.
        wor = self._make_request(inv)  # Assign test setup data.
        self._submit(wor)  # preflight passes while the balance is outstanding
        instance = self._instance_for(wor)  # Assign test setup data.

        # Settle the invoice in full while the request sits in the queue, so
        # write_off_invoice raises "no outstanding balance" at approval.
        bank = Account.objects.get(entity=self.entity, code="1100")  # Fetch test database data.
        pay = Payment.objects.create(  # Create test database data.
            entity=self.entity, customer=self.customer,  # Continue structured test data.
            payment_date=datetime.date(2026, 1, 15), amount=100000, deposit_account=bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay)  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.balance_due, 0)  # Check the expected test outcome.

        with self.assertRaises(PostingError):  # Enter a test context manager.
            wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)  # Execute the test step.

        # Option A: the approval action rolled back — request not POSTED, no journal,
        # invoice untouched by any write-off, stage still ACTIVE for a retry.
        wor.refresh_from_db()  # Execute the test step.
        self.assertNotEqual(wor.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertIsNone(wor.journal_id)  # Check the expected test outcome.
        self.assertFalse(WorkflowStageAction.objects.filter(  # Check the expected test outcome.
            stage_instance__instance=instance, action=ActionEnum.APPROVED,  # Continue structured test data.
            reversed_at__isnull=True, is_reversal_of__isnull=True).exists())  # Assign test setup data.
        instance.refresh_from_db()  # Execute the test step.
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            instance.stage_instances.filter(status=WorkflowStageStatus.ACTIVE).exists())  # Assign test setup data.

    # --- 8. Backward-compat bridge on /invoices/<id>/write-off/ ------------ #

    def test_invoice_write_off_bridge_submits_when_gated(self):  # Define a test helper or test method.
        from vs_finance.models import WriteOffRequest  # Import project symbols exercised by these tests.

        self._publish_standard_template()  # Execute the test step.
        self._make_approver()  # Execute the test step.
        inv = self._posted_invoice()  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/invoices/{inv.pk}/write-off/?entity={self.entity.code}", {}, format="json")  # Assign test setup data.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        # A request was created and submitted; the invoice is NOT yet written off.
        wor = WriteOffRequest.objects.get(invoice=inv)  # Fetch test database data.
        self.assertEqual(wor.status, DocumentStatus.PENDING_APPROVAL)  # Check the expected test outcome.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.balance_due, 100000)  # Check the expected test outcome.

    def test_invoice_write_off_bridge_posts_directly_when_ungated(self):  # Define a test helper or test method.
        from vs_finance.models import WriteOffRequest  # Import project symbols exercised by these tests.

        inv = self._posted_invoice()  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/finance/invoices/{inv.pk}/write-off/?entity={self.entity.code}", {}, format="json")  # Assign test setup data.
        self.assertEqual(resp.status_code, 200, resp.content)  # Check the expected test outcome.
        # No template → posts directly; the invoice is written off as before.
        wor = WriteOffRequest.objects.get(invoice=inv)  # Fetch test database data.
        self.assertEqual(wor.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertIsNotNone(wor.journal_id)  # Check the expected test outcome.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.amount_credited, 100000)  # Check the expected test outcome.
        self.assertEqual(inv.balance_due, 0)  # Check the expected test outcome.


class DunningNotificationTests(_GLFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """Dunning delivery routed entirely through vs_notifications.

    Proves that generating + sending a dunning notice creates a
    ``vs_notifications.Notification`` record (delivery goes through the notification
    system, never from vs_finance directly), that the policy stage's message carries
    the escalation wording, and that the daily scheduler + graceful-degradation paths
    behave. Notifications are school-scoped, so these use a school-owned entity.
    """

    def setUp(self):  # Define a test helper or test method.
        from vs_notifications.services.seed import (  # Import project symbols exercised by these tests.
            seed_event_types, seed_notification_templates, seed_school_settings,  # Continue structured test data.
        )  # Close the grouped test expression.

        # Seed the notification event types + default templates (fresh test DB, so the
        # get_or_create seed picks up the extended overdue template), then the school's
        # channel settings.
        seed_event_types()  # Execute the test step.
        seed_notification_templates()  # Execute the test step.

        self.school = School.objects.create(name="Maplewood", slug="maplewood-dnt", code="MPLDN")  # Create test database data.
        seed_school_settings(self.school)  # Execute the test step.

        seed_currencies()  # Execute the test step.
        self.entity = LedgerEntity.objects.create(  # Create test database data.
            name="Maplewood Books", code="MPLBK", kind=LedgerEntity.Kind.TENANT,  # Continue structured test data.
            source_school=self.school,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_chart_of_accounts(self.entity)  # Execute the test step.
        self.year = FiscalYear.objects.create(  # Create test database data.
            entity=self.entity, year=2026,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        self.period = FiscalPeriod.objects.create(  # Create test database data.
            entity=self.entity, fiscal_year=self.year, period_no=1, name="Jan 2026",  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),  # Continue structured test data.
            status=PeriodStatus.OPEN,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.customer = Customer.objects.create(  # Create test database data.
            entity=self.entity, code="CUSTD", name="Debtor Ltd",  # Continue structured test data.
            receivable_account=Account.objects.get(entity=self.entity, code="1200"),  # Fetch test database data.
            billing_email="debtor@example.com",  # Continue structured test data.
        )  # Close the grouped test expression.

    # --- helpers ----------------------------------------------------------- #

    def _overdue_invoice(self, *, unit_price=100000, due=datetime.date(2026, 1, 10)):  # Define a test helper or test method.
        inv = Invoice.objects.create(  # Create test database data.
            entity=self.entity, customer=self.customer,  # Continue structured test data.
            invoice_date=datetime.date(2026, 1, 1), due_date=due,  # Continue structured test data.
        )  # Close the grouped test expression.
        InvoiceLine.objects.create(  # Create test database data.
            invoice=inv, revenue_account=Account.objects.get(entity=self.entity, code="4100"),  # Fetch test database data.
            quantity=1, unit_price=unit_price, tax_code=None, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_invoice(inv)  # Execute the test step.
        inv.refresh_from_db()  # Execute the test step.
        return inv  # Return the prepared test value.

    def _generate_one(self, *, as_of=datetime.date(2026, 2, 15)):  # Define a test helper or test method.
        ensure_default_policy(self.entity)  # Execute the test step.
        self._overdue_invoice()  # Execute the test step.
        notices = generate_dunning(self.entity, as_of=as_of)  # Assign test setup data.
        self.assertEqual(len(notices), 1)  # Check the expected test outcome.
        return notices[0]  # Return the prepared test value.

    # --- 1. delivery goes through vs_notifications ------------------------- #

    def test_mark_sent_creates_email_notification_and_flips_sent(self):  # Define a test helper or test method.
        from vs_notifications.models import Notification  # Import project symbols exercised by these tests.
        from vs_notifications.constants import ChannelChoices  # Import project symbols exercised by these tests.

        notice = self._generate_one()  # Assign test setup data.
        mark_notice_sent(notice)  # Execute the test step.

        notice.refresh_from_db()  # Execute the test step.
        self.assertEqual(notice.notice_status, "SENT")  # Check the expected test outcome.
        self.assertIsNotNone(notice.sent_at)  # Check the expected test outcome.

        email = Notification.objects.filter(  # Query test database data.
            school=self.school, channel=ChannelChoices.EMAIL,  # Continue structured test data.
            unregistered_email="debtor@example.com",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertTrue(email.exists(), "an EMAIL notification should be created for the customer")  # Check the expected test outcome.

    # --- 2. escalation wording comes from the policy ---------------------- #

    def test_email_body_contains_policy_reminder_message(self):  # Define a test helper or test method.
        from vs_notifications.models import Notification  # Import project symbols exercised by these tests.
        from vs_notifications.constants import ChannelChoices  # Import project symbols exercised by these tests.

        notice = self._generate_one()  # Assign test setup data.
        # The generated notice snapshots the stage message (the policy's wording).
        self.assertTrue(notice.message)  # Check the expected test outcome.
        mark_notice_sent(notice)  # Execute the test step.

        # Scope to the overdue event: post_invoice also fires an invoice_issued EMAIL
        # to the same customer, so filter by event key to get the dunning notice.
        email = Notification.objects.get(  # Fetch test database data.
            school=self.school, channel=ChannelChoices.EMAIL,  # Continue structured test data.
            unregistered_email="debtor@example.com",  # Continue structured test data.
            event_type__key="billing.invoice_overdue",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertIn(notice.message, email.body)  # Check the expected test outcome.

    # --- 3. platform/no-school entity: skipped gracefully ------------------ #

    def test_no_school_entity_still_delivers(self):  # Define a test helper or test method.
        # Recipient-centric notifications: a platform/product book (no source_school)
        # still delivers to the customer's billing_email — school is an optional scope,
        # not a gate. (Tracks the notifications overhaul.)
        from vs_notifications.models import Notification  # Import project symbols exercised by these tests.

        platform = LedgerEntity.objects.create(  # Create test database data.
            name="Platform Books", code="PLTDN", kind=LedgerEntity.Kind.PLATFORM,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_chart_of_accounts(platform)  # Execute the test step.
        FiscalYear.objects.create(  # Create test database data.
            entity=platform, year=2026,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        FiscalPeriod.objects.create(  # Create test database data.
            entity=platform, fiscal_year=FiscalYear.objects.get(entity=platform),  # Fetch test database data.
            period_no=1, name="Jan 2026",  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),  # Continue structured test data.
            status=PeriodStatus.OPEN,  # Continue structured test data.
        )  # Close the grouped test expression.
        cust = Customer.objects.create(  # Create test database data.
            entity=platform, code="PCUST", name="Platform Debtor",  # Continue structured test data.
            receivable_account=Account.objects.get(entity=platform, code="1200"),  # Fetch test database data.
            billing_email="p@example.com",  # Continue structured test data.
        )  # Close the grouped test expression.
        inv = Invoice.objects.create(  # Create test database data.
            entity=platform, customer=cust, invoice_date=datetime.date(2026, 1, 1),  # Continue structured test data.
            due_date=datetime.date(2026, 1, 10),  # Continue structured test data.
        )  # Close the grouped test expression.
        InvoiceLine.objects.create(  # Create test database data.
            invoice=inv, revenue_account=Account.objects.get(entity=platform, code="4100"),  # Fetch test database data.
            quantity=1, unit_price=100000, tax_code=None, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_invoice(inv)  # Execute the test step.
        ensure_default_policy(platform)  # Execute the test step.
        notices = generate_dunning(platform, as_of=datetime.date(2026, 2, 15))  # Assign test setup data.
        self.assertEqual(len(notices), 1)  # Check the expected test outcome.

        before = Notification.objects.count()  # Assign test setup data.
        mark_notice_sent(notices[0])  # must not raise
        notices[0].refresh_from_db()  # Execute the test step.
        # No school → still delivered (recipient-centric), and the notice flips SENT.
        self.assertEqual(notices[0].notice_status, "SENT")  # Check the expected test outcome.
        self.assertGreater(Notification.objects.count(), before)  # Check the expected test outcome.

    # --- 4. customer without billing_email → FAILED notification ---------- #

    def test_missing_billing_email_records_failed_notification(self):  # Define a test helper or test method.
        from vs_notifications.models import Notification  # Import project symbols exercised by these tests.
        from vs_notifications.constants import ChannelChoices, NotificationStatus  # Import project symbols exercised by these tests.

        self.customer.billing_email = ""  # Assign test setup data.
        self.customer.save(update_fields=["billing_email"])  # Assign test setup data.

        notice = self._generate_one()  # Assign test setup data.
        mark_notice_sent(notice)  # must not crash

        failed = Notification.objects.filter(  # Query test database data.
            school=self.school, channel=ChannelChoices.EMAIL,  # Continue structured test data.
            status=NotificationStatus.FAILED, failure_reason="NO_EMAIL_ADDRESS",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertTrue(failed.exists())  # Check the expected test outcome.
        notice.refresh_from_db()  # Execute the test step.
        self.assertEqual(notice.notice_status, "SENT")  # Check the expected test outcome.

    # --- 5. run_daily_dunning end-to-end + skips no-policy entity ---------- #

    def test_run_daily_dunning_generates_dispatches_and_skips_no_policy(self):  # Define a test helper or test method.
        from vs_finance.tasks import run_daily_dunning  # Import project symbols exercised by these tests.
        from vs_notifications.models import Notification  # Import project symbols exercised by these tests.
        from vs_notifications.constants import ChannelChoices  # Import project symbols exercised by these tests.
        from vs_notifications.services.seed import seed_school_settings  # Import project symbols exercised by these tests.

        # This entity has a policy + an overdue invoice.
        ensure_default_policy(self.entity)  # Execute the test step.
        self._overdue_invoice(due=datetime.date(2026, 1, 5))  # Assign test setup data.

        # A second school entity with NO policy — must be skipped, not crash the run.
        other_school = School.objects.create(name="Oak", slug="oak-dnt", code="OAKDN")  # Create test database data.
        seed_school_settings(other_school)  # Execute the test step.
        other = LedgerEntity.objects.create(  # Create test database data.
            name="Oak Books", code="OAKBK", kind=LedgerEntity.Kind.TENANT,  # Continue structured test data.
            source_school=other_school,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_chart_of_accounts(other)  # Execute the test step.

        result = run_daily_dunning()  # Assign test setup data.

        self.assertGreaterEqual(result["generated"], 1)  # Check the expected test outcome.
        self.assertGreaterEqual(result["sent"], 1)  # Check the expected test outcome.
        self.assertGreaterEqual(result["skipped"], 1)  # the no-policy entity
        self.assertTrue(Notification.objects.filter(  # Check the expected test outcome.
            school=self.school, channel=ChannelChoices.EMAIL,  # Continue structured test data.
            unregistered_email="debtor@example.com").exists())  # Assign test setup data.

    # --- 6. idempotency: second mark_notice_sent is a no-op --------------- #

    def test_second_mark_sent_does_not_duplicate_notification(self):  # Define a test helper or test method.
        from vs_notifications.models import Notification  # Import project symbols exercised by these tests.

        notice = self._generate_one()  # Assign test setup data.
        mark_notice_sent(notice)  # Execute the test step.
        count_after_first = Notification.objects.count()  # Assign test setup data.

        mark_notice_sent(notice)  # already SENT → no-op
        self.assertEqual(Notification.objects.count(), count_after_first)  # Check the expected test outcome.


class InvoiceNotificationTests(_GLFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """Invoice + receipt notifications routed through vs_notifications (best-effort).

    Fee/manual invoices email the customer on issue; opening-balance invoices stay
    silent; every receipt emails a confirmation. Delivery is recipient-centric (works
    with or without a school) and must NEVER break the underlying money posting.
    """

    def setUp(self):  # Define a test helper or test method.
        from vs_notifications.services.seed import (  # Import project symbols exercised by these tests.
            seed_event_types, seed_notification_templates, seed_school_settings,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_event_types()  # Execute the test step.
        seed_notification_templates()  # Execute the test step.
        self.school = School.objects.create(name="Birchwood", slug="birchwood-int", code="BRCIN")  # Create test database data.
        seed_school_settings(self.school)  # Execute the test step.
        seed_currencies()  # Execute the test step.
        self.entity = LedgerEntity.objects.create(  # Create test database data.
            name="Birchwood Books", code="BRCBK", kind=LedgerEntity.Kind.TENANT,  # Continue structured test data.
            source_school=self.school,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_chart_of_accounts(self.entity)  # Execute the test step.
        self.year = FiscalYear.objects.create(  # Create test database data.
            entity=self.entity, year=2026,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        self.period = FiscalPeriod.objects.create(  # Create test database data.
            entity=self.entity, fiscal_year=self.year, period_no=1, name="Jan 2026",  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),  # Continue structured test data.
            status=PeriodStatus.OPEN,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.bank = Account.objects.get(entity=self.entity, code="1100")  # Fetch test database data.
        self.customer = Customer.objects.create(  # Create test database data.
            entity=self.entity, code="CUSTI", name="Payer Ltd",  # Continue structured test data.
            receivable_account=Account.objects.get(entity=self.entity, code="1200"),  # Fetch test database data.
            billing_email="payer@example.com",  # Continue structured test data.
        )  # Close the grouped test expression.

    def _make_invoice(self, *, unit_price=100000, source="MANUAL"):  # Define a test helper or test method.
        inv = Invoice.objects.create(  # Create test database data.
            entity=self.entity, customer=self.customer,  # Continue structured test data.
            invoice_date=datetime.date(2026, 1, 5), due_date=datetime.date(2026, 1, 20),  # Continue structured test data.
            source=source,  # Continue structured test data.
        )  # Close the grouped test expression.
        InvoiceLine.objects.create(  # Create test database data.
            invoice=inv, revenue_account=Account.objects.get(entity=self.entity, code="4100"),  # Fetch test database data.
            quantity=1, unit_price=unit_price, tax_code=None, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        return inv  # Return the prepared test value.

    def _issued(self):  # Define a test helper or test method.
        from vs_notifications.models import Notification  # Import project symbols exercised by these tests.
        return Notification.objects.filter(event_type__key="billing.invoice_issued")  # Return the prepared test value.

    def _received(self):  # Define a test helper or test method.
        from vs_notifications.models import Notification  # Import project symbols exercised by these tests.
        return Notification.objects.filter(event_type__key="billing.payment_received")  # Return the prepared test value.

    def test_posting_manual_invoice_notifies_customer(self):  # Define a test helper or test method.
        from vs_notifications.constants import ChannelChoices  # Import project symbols exercised by these tests.

        inv = self._make_invoice()  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        self.assertTrue(self._issued().filter(  # Check the expected test outcome.
            channel=ChannelChoices.EMAIL, unregistered_email="payer@example.com").exists())  # Assign test setup data.

    def test_opening_balance_invoice_stays_silent(self):  # Define a test helper or test method.
        from vs_finance.receivables import post_opening_balance  # Import project symbols exercised by these tests.

        self.customer.opening_balance = 500000  # Assign test setup data.
        self.customer.save(update_fields=["opening_balance"])  # Assign test setup data.
        post_opening_balance(self.customer, date=datetime.date(2026, 1, 5))  # Assign test setup data.
        # Opening balances are migration artefacts — no invoice_issued email.
        self.assertFalse(self._issued().exists())  # Check the expected test outcome.

    def test_posting_receipt_notifies_customer(self):  # Define a test helper or test method.
        from vs_notifications.constants import ChannelChoices  # Import project symbols exercised by these tests.

        inv = self._make_invoice()  # Assign test setup data.
        post_invoice(inv)  # Execute the test step.
        pay = Payment.objects.create(  # Create test database data.
            entity=self.entity, customer=self.customer,  # Continue structured test data.
            payment_date=datetime.date(2026, 1, 10), amount=100000, deposit_account=self.bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay, allocations=[(inv, 100000)])  # Assign test setup data.
        self.assertTrue(self._received().filter(  # Check the expected test outcome.
            channel=ChannelChoices.EMAIL, unregistered_email="payer@example.com").exists())  # Assign test setup data.

    def test_notification_failure_does_not_break_posting(self):  # Define a test helper or test method.
        from vs_notifications.models import NotificationEventType  # Import project symbols exercised by these tests.

        # Deactivate the event so send_notification raises inside the best-effort
        # wrapper; the invoice must still post cleanly (money is never held hostage
        # to a notification problem).
        NotificationEventType.objects.filter(key="billing.invoice_issued").update(is_active=False)  # Query test database data.
        inv = self._make_invoice()  # Assign test setup data.
        post_invoice(inv)  # must not raise
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertTrue(AccountBalance.objects.filter(  # Check the expected test outcome.
            account__entity=self.entity, period=self.period).exists())  # Assign test setup data.
        self.assertFalse(self._issued().exists())  # Check the expected test outcome.

    def test_gateway_style_receipt_notifies(self):  # Define a test helper or test method.
        # A standalone receipt (as the gateway books it) fires payment_received too.
        pay = Payment.objects.create(  # Create test database data.
            entity=self.entity, customer=self.customer,  # Continue structured test data.
            payment_date=datetime.date(2026, 1, 12), amount=50000, deposit_account=self.bank,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_payment(pay, auto_allocate=False)  # Assign test setup data.
        self.assertTrue(self._received().exists())  # Check the expected test outcome.

    def test_no_school_entity_posts_and_delivers(self):  # Define a test helper or test method.
        # Recipient-centric: a platform book (no school) still notifies, and posting
        # is unaffected.
        platform = LedgerEntity.objects.create(  # Create test database data.
            name="Platform Books", code="PLTIN", kind=LedgerEntity.Kind.PLATFORM,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_chart_of_accounts(platform)  # Execute the test step.
        FiscalYear.objects.create(  # Create test database data.
            entity=platform, year=2026,  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        FiscalPeriod.objects.create(  # Create test database data.
            entity=platform, fiscal_year=FiscalYear.objects.get(entity=platform),  # Fetch test database data.
            period_no=1, name="Jan 2026",  # Continue structured test data.
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),  # Continue structured test data.
            status=PeriodStatus.OPEN,  # Continue structured test data.
        )  # Close the grouped test expression.
        cust = Customer.objects.create(  # Create test database data.
            entity=platform, code="PLC", name="Platform Payer",  # Continue structured test data.
            receivable_account=Account.objects.get(entity=platform, code="1200"),  # Fetch test database data.
            billing_email="pp@example.com",  # Continue structured test data.
        )  # Close the grouped test expression.
        inv = Invoice.objects.create(  # Create test database data.
            entity=platform, customer=cust, invoice_date=datetime.date(2026, 1, 5),  # Continue structured test data.
            due_date=datetime.date(2026, 1, 20), source="MANUAL",  # Continue structured test data.
        )  # Close the grouped test expression.
        InvoiceLine.objects.create(  # Create test database data.
            invoice=inv, revenue_account=Account.objects.get(entity=platform, code="4100"),  # Fetch test database data.
            quantity=1, unit_price=100000, tax_code=None, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_invoice(inv)  # must not raise
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.status, DocumentStatus.POSTED)  # Check the expected test outcome.
        self.assertTrue(self._issued().filter(unregistered_email="pp@example.com").exists())  # Check the expected test outcome.
