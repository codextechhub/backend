"""Phase-0 foundation + Phase-1 GL tests: money, guards, entities, numbering, and
the double-entry ledger (chart of accounts, posting, reversal, trial balance)."""
from __future__ import annotations

import datetime
from decimal import Decimal

from django.db.models import Sum
from django.test import TestCase, override_settings

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
from vs_finance.receivables import allocate_payment, customer_credit_balance, post_invoice, post_payment
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
    budget_monthly_matrix,
    budget_vs_actual,
    cash_flow_statement,
    customer_statement,
    income_statement,
    income_statement_compare,
    reconcile_ar,
    statement_of_changes_in_equity,
    statutory_pack,
    trial_balance,
)
from vs_finance.banking import (
    auto_reconcile,
    import_statement_lines,
    match_line,
    post_bank_adjustment,
)
from vs_finance.expenses import (
    post_expense_claim,
    settle_expense_claim,
    void_expense_claim,
)
from vs_finance.petty_cash import (
    establish_fund,
    fund_status,
    gl_cash_on_hand,
    post_voucher,
    replenish_fund,
    void_voucher,
)
from vs_finance.tax_filing import (
    file_filing,
    outstanding_obligations,
    pay_filing,
    prepare_filing,
    unfile_filing,
)
from vs_finance.constants import TaxFilingStatus, TaxObligationType
from vs_finance.payroll import cancel_payroll_run, pay_payroll, post_payroll
from vs_finance.budgets import add_budget_line, approve_budget
from vs_finance.assets import (
    acquire_asset, build_depreciation_schedule, dispose_asset, post_depreciation,
    run_period_depreciation,
)
from vs_finance.close import (
    close_checklist,
    close_period,
    lock_period,
    reopen_period,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies, seed_tax_obligations
from vs_schools.models import Branch, School


# Group tests for Money Tests.
class MoneyTests(TestCase):
    # Verify to kobo from string is exact behavior.
    def test_to_kobo_from_string_is_exact(self):
        self.assertEqual(to_kobo("1250.50"), 125050)

    # Verify to kobo handles float boundary without drift behavior.
    def test_to_kobo_handles_float_boundary_without_drift(self):
        # The classic 0.1 + 0.2 trap: must land on 30 kobo, not 29 or 30.0000001.
        self.assertEqual(to_kobo(Decimal("0.1") + Decimal("0.2")), 30)
        self.assertEqual(to_kobo(0.1 + 0.2), 30)

    # Verify round trip behavior.
    def test_round_trip(self):
        self.assertEqual(to_naira(125050), Decimal("1250.50"))
        self.assertEqual(to_kobo(to_naira(99)), 99)

    # Verify half up rounding behavior.
    def test_half_up_rounding(self):
        self.assertEqual(to_kobo("0.005"), 1)  # rounds up at the half

    # Verify format behavior.
    def test_format(self):
        self.assertEqual(format_naira(125050), "₦1,250.50")

    # Verify to naira rejects non int behavior.
    def test_to_naira_rejects_non_int(self):
        with self.assertRaises(TypeError):
            to_naira(12.5)  # type: ignore[arg-type]


# Group tests for Posting Guard Tests.
class PostingGuardTests(TestCase):
    # Group tests for Period.
    class _Period:
        # Initialize this object with its required state.
        def __init__(self, status):
            self.status = status

        # Support the str workflow.
        def __str__(self):
            return f"2026-01 [{self.status}]"

    # Verify open period allows posting behavior.
    def test_open_period_allows_posting(self):
        ensure_period_open(self._Period(PeriodStatus.OPEN))  # no raise

    # Verify closed and locked block posting behavior.
    def test_closed_and_locked_block_posting(self):
        for status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
            with self.assertRaises(PeriodClosedError):
                ensure_period_open(self._Period(status))

    # Verify soft closed blocked by default allowed when privileged behavior.
    def test_soft_closed_blocked_by_default_allowed_when_privileged(self):
        with self.assertRaises(PeriodClosedError):
            ensure_period_open(self._Period(PeriodStatus.SOFT_CLOSED))
        ensure_period_open(self._Period(PeriodStatus.SOFT_CLOSED), allow_restricted=True)

    # Verify missing period fails closed behavior.
    def test_missing_period_fails_closed(self):
        with self.assertRaises(PeriodClosedError):
            ensure_period_open(None)

    # Verify balanced check behavior.
    def test_balanced_check(self):
        ensure_balanced(125050, 125050)  # no raise
        with self.assertRaises(UnbalancedJournalError):
            ensure_balanced(125050, 125000)


# Group tests for Ledger Entity Tests.
class LedgerEntityTests(TestCase):
    # Verify platform entity seeded with no school behavior.
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

    # Verify one school can own multiple entities behavior.
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


# Group tests for Numbering Tests.
class NumberingTests(TestCase):
    # Prepare or verify the setUp test path.
    def setUp(self):
        self.school = School.objects.create(name="Test Org", slug="test-org")
        self.branch = Branch.objects.create(school=self.school, name="HQ", _type="Main")
        self.entity = LedgerEntity.objects.create(
            name="Test Org Books", code="LEKKI",
            kind=LedgerEntity.Kind.TENANT, source_school=self.school,
        )
        # Use the platform entity seeded by migration 0002 (code CODEX).
        self.platform = LedgerEntity.objects.platform()

    # Verify format and increment with branch behavior.
    def test_format_and_increment_with_branch(self):
        n1 = next_document_number(
            entity=self.entity, branch=self.branch, doc_type=DocType.INVOICE, fiscal_year=2026,
        )
        n2 = next_document_number(
            entity=self.entity, branch=self.branch, doc_type=DocType.INVOICE, fiscal_year=2026,
        )
        self.assertEqual(n1, f"CFX-LEKKI-B{self.branch.code:02d}-INV-2026-00001")
        self.assertEqual(n2, f"CFX-LEKKI-B{self.branch.code:02d}-INV-2026-00002")

    # Verify entity level doc omits branch segment behavior.
    def test_entity_level_doc_omits_branch_segment(self):
        n = next_document_number(
            entity=self.platform, branch=None, doc_type=DocType.PAYMENT, fiscal_year=2026,
        )
        self.assertEqual(n, "CFX-CODEX-PAY-2026-00001")

    # Verify scopes are independent behavior.
    def test_scopes_are_independent(self):
        inv = next_document_number(
            entity=self.entity, branch=self.branch, doc_type=DocType.INVOICE, fiscal_year=2026,
        )
        po = next_document_number(
            entity=self.entity, branch=self.branch, doc_type=DocType.PURCHASE_ORDER, fiscal_year=2026,
        )
        self.assertTrue(inv.endswith("INV-2026-00001"))
        self.assertTrue(po.endswith("PO-2026-00001"))

    # Verify two entities keep independent series behavior.
    def test_two_entities_keep_independent_series(self):
        a = next_document_number(
            entity=self.entity, branch=None, doc_type=DocType.JOURNAL, fiscal_year=2026,
        )
        b = next_document_number(
            entity=self.platform, branch=None, doc_type=DocType.JOURNAL, fiscal_year=2026,
        )
        self.assertEqual(a, "CFX-LEKKI-JNL-2026-00001")
        self.assertEqual(b, "CFX-CODEX-JNL-2026-00001")


# Group tests for G L Fixture Mixin.
class _GLFixtureMixin:
    """Builds an entity with a seeded chart, a fiscal year and one open period."""

    # Prepare or verify the build ledger test path.
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

    # Prepare or verify the make entry test path.
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


# Group tests for Chart Of Accounts Tests.
class ChartOfAccountsTests(_GLFixtureMixin, TestCase):
    # Verify seed creates five roots and links parents behavior.
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

    # Verify normal balance derived and contra flips behavior.
    def test_normal_balance_derived_and_contra_flips(self):
        entity, _ = self.build_ledger()
        cash = Account.objects.get(entity=entity, code="1100")
        self.assertEqual(cash.normal_balance, NormalBalance.DEBIT)
        accum_dep = Account.objects.get(entity=entity, code="1900")
        self.assertEqual(accum_dep.normal_balance, NormalBalance.CREDIT)
        revenue = Account.objects.get(entity=entity, code="4100")
        self.assertEqual(revenue.normal_balance, NormalBalance.CREDIT)

    # Verify seed is idempotent behavior.
    def test_seed_is_idempotent(self):
        entity, _ = self.build_ledger()
        before = Account.objects.filter(entity=entity).count()
        seed_chart_of_accounts(entity)
        self.assertEqual(Account.objects.filter(entity=entity).count(), before)


# Group tests for Posting Tests.
class PostingTests(_GLFixtureMixin, TestCase):
    # Verify balanced post updates balances and stamps posted behavior.
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

    # Verify unbalanced entry is rejected behavior.
    def test_unbalanced_entry_is_rejected(self):
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 50000, 0), ("4100", 0, 40000)])
        with self.assertRaises(UnbalancedJournalError):
            post_journal(entry)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.DRAFT)
        self.assertFalse(AccountBalance.objects.filter(period=period).exists())

    # Verify closed period blocks posting behavior.
    def test_closed_period_blocks_posting(self):
        entity, period = self.build_ledger(period_status=PeriodStatus.CLOSED)
        entry = self.make_entry(entity, period, [("1100", 10000, 0), ("4100", 0, 10000)])
        with self.assertRaises(PeriodClosedError):
            post_journal(entry)

    # Verify inactive account blocks posting behavior.
    def test_inactive_account_blocks_posting(self):
        entity, period = self.build_ledger()
        Account.objects.filter(entity=entity, code="4100").update(is_active=False)
        entry = self.make_entry(entity, period, [("1100", 10000, 0), ("4100", 0, 10000)])
        with self.assertRaises(InactiveAccountError):
            post_journal(entry)

    # Verify cannot double post behavior.
    def test_cannot_double_post(self):
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 10000, 0), ("4100", 0, 10000)])
        post_journal(entry)
        with self.assertRaises(PostingError):
            post_journal(entry)

    # Verify reversal nets balances to zero behavior.
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

    # Verify cannot reverse twice behavior.
    def test_cannot_reverse_twice(self):
        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 30000, 0), ("4100", 0, 30000)])
        post_journal(entry)
        reverse_journal(entry)
        with self.assertRaises(PostingError):
            reverse_journal(entry)

    # Verify reverse into open period when original closed behavior.
    def test_reverse_into_open_period_when_original_closed(self):
        # Prior-period correction: the original journal's period has since closed, so
        # the reversal is booked into a still-open period given an explicit date. Also
        # guards the fix where the reversal's period follows the date rather than being
        # pinned to the original's (now-closed) period.
        entity, jan = self.build_ledger()
        feb = FiscalPeriod.objects.create(
            entity=entity, fiscal_year=jan.fiscal_year, period_no=2, name="Feb 2026",
            start_date=datetime.date(2026, 2, 1), end_date=datetime.date(2026, 2, 28),
            status=PeriodStatus.OPEN,
        )
        entry = self.make_entry(entity, jan, [("1100", 30000, 0), ("4100", 0, 30000)])
        post_journal(entry)
        jan.status = PeriodStatus.CLOSED           # Jan closes after the journal posted
        jan.save(update_fields=["status"])

        reversal = reverse_journal(entry, date=datetime.date(2026, 2, 15))
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.REVERSED)
        self.assertEqual(reversal.status, DocumentStatus.POSTED)
        self.assertEqual(reversal.period_id, feb.id)          # booked into the open period
        self.assertEqual(reversal.date, datetime.date(2026, 2, 15))


# Group tests for Trial Balance Tests.
class TrialBalanceTests(_GLFixtureMixin, TestCase):
    # Verify trial balance balances behavior.
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

    # Verify empty ledger trivially balances behavior.
    def test_empty_ledger_trivially_balances(self):
        entity, _ = self.build_ledger()
        tb = trial_balance(entity)
        self.assertTrue(tb.is_balanced)
        self.assertEqual(tb.rows, [])

    # Verify period scope is cumulative and all periods is not double counted behavior.
    def test_period_scope_is_cumulative_and_all_periods_is_not_double_counted(self):
        """A period-scoped TB is the running balance *through* that period; the
        all-periods TB is the cumulative all-time balance — never a sum that
        double-counts across periods."""
        entity, jan = self.build_ledger()
        feb = FiscalPeriod.objects.create(
            entity=entity, fiscal_year=jan.fiscal_year, period_no=2, name="Feb 2026",
            start_date=datetime.date(2026, 2, 1), end_date=datetime.date(2026, 2, 28),
            status=PeriodStatus.OPEN,
        )
        post_journal(self.make_entry(entity, jan, [("1100", 100000, 0), ("4100", 0, 100000)],
                                     date=datetime.date(2026, 1, 15)))
        post_journal(self.make_entry(entity, feb, [("1100", 40000, 0), ("4100", 0, 40000)],
                                     date=datetime.date(2026, 2, 15)))

        cash = lambda tb: next(r for r in tb.rows if r.code == "1100").debit
        self.assertEqual(cash(trial_balance(entity, period=jan)), 100000)   # through Jan
        self.assertEqual(cash(trial_balance(entity, period=feb)), 140000)   # cumulative through Feb
        self.assertEqual(cash(trial_balance(entity)), 140000)              # all-time, not 240000
        self.assertTrue(trial_balance(entity).is_balanced)


# Group tests for Finance Audit Tests.
class FinanceAuditTests(_GLFixtureMixin, TestCase):
    # Verify post writes authoritative audit row behavior.
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

    # Verify reversal writes reversed audit row behavior.
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

    # Verify rejected post records failure durably behavior.
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

    # Verify audit log is append only behavior.
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

    # Verify audit log immutable at db level behavior.
    def test_audit_log_immutable_at_db_level(self):
        # Queryset .update()/.delete() bypass the Python model hooks, but the DB
        # triggers (Postgres) must still block them. A normal INSERT keeps working.
        from django.db import Error, transaction

        entity, period = self.build_ledger()
        entry = self.make_entry(entity, period, [("1100", 1000, 0), ("4100", 0, 1000)])
        post_journal(entry)  # writes an audit row via a normal INSERT
        qs = FinanceAuditLog.objects.filter(target_id=str(entry.pk))
        self.assertTrue(qs.exists())

        with self.assertRaises(Error):
            with transaction.atomic():
                qs.update(message="tampered")
        with self.assertRaises(Error):
            with transaction.atomic():
                qs.delete()
        # The row is untouched, and inserts still succeed.
        log = qs.first()
        self.assertNotEqual(log.message, "tampered")
        reversal = reverse_journal(entry)
        self.assertTrue(
            FinanceAuditLog.objects.filter(
                action=FinanceAuditAction.JOURNAL_POSTED, target_id=str(reversal.pk),
            ).exists()
        )


# Group tests for A R Fixture Mixin.
class _ARFixtureMixin(_GLFixtureMixin):
    """A ledger plus a customer wired to the AR control account and a VAT tax code."""

    # Prepare or verify the build ar test path.
    def build_ar(self, *, period_status=PeriodStatus.OPEN):
        entity, period = self.build_ledger(period_status=period_status)
        ar_control = Account.objects.get(entity=entity, code="1200")
        vat_output = Account.objects.get(entity=entity, code="2200")
        customer = Customer.objects.create(
            entity=entity, code="CUST1", name="Acme Ltd",
            receivable_account=ar_control,
        )
        vat = TaxCode.objects.create(
            entity=entity, code="VAT", name="VAT 7.5%", rate_bps=750,
            collected_account=vat_output,
        )
        return entity, period, customer, vat

    # Build or verify the make invoice test path.
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


# Group tests for Invoice Posting Tests.
class InvoicePostingTests(_ARFixtureMixin, TestCase):
    # Verify invoice posts balanced ar journal with tax behavior.
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

    # Verify invoice in closed period is rejected behavior.
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


# Group tests for Payment Allocation Tests.
class PaymentAllocationTests(_ARFixtureMixin, TestCase):
    # Verify partial then full payment moves status and aging behavior.
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

    # Verify overpayment leaves unallocated credit behavior.
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


# Group tests for A R Reconciliation Tests.
class ARReconciliationTests(_ARFixtureMixin, TestCase):
    # Verify aging buckets and control reconciles behavior.
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

    # Verify reconciles after partial payment behavior.
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


# Group tests for Credit Note Tests.
class CreditNoteTests(_ARFixtureMixin, TestCase):
    # Verify credit note posts reverses ar and applies to invoice behavior.
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

    # Verify debit note increases ar and cannot be allocated behavior.
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

    # Support the post debit note workflow.
    def _post_debit_note(self, entity, customer, *, amount, date, tax=None):
        """Helper: create + post a single-line DEBIT note, return it refreshed."""
        note = CreditNote.objects.create(
            entity=entity, customer=customer, kind=CreditNoteKind.DEBIT,
            note_date=date, reason="Supplementary charge",
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=Account.objects.get(entity=entity, code="4100"),
            quantity=1, unit_price=amount, tax_code=tax, line_no=1,
        )
        post_credit_note(note)
        note.refresh_from_db()
        return note

    # Verify receipt settles standalone debit note behavior.
    def test_receipt_settles_standalone_debit_note(self):
        # The reported bug: a debit note with no invoice, then a larger receipt. The
        # receipt must settle the debit note (not leave it dangling) and book only the
        # true excess as customer credit.
        entity, period, customer, _ = self.build_ar()
        bank = Account.objects.get(entity=entity, code="1100")
        note = self._post_debit_note(
            entity, customer, amount=20000, date=datetime.date(2026, 1, 10))

        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=40000, deposit_account=bank,
        )
        post_payment(pay)  # auto-allocates → should settle the debit note first
        note.refresh_from_db()
        pay.refresh_from_db()

        # Debit note fully settled by the receipt.
        self.assertEqual(note.amount_paid, 20000)
        self.assertEqual(note.balance_due, 0)
        self.assertEqual(note.settlement_status, InvoicePaymentStatus.PAID)
        # Only the true excess is unallocated credit.
        self.assertEqual(pay.allocated_amount, 20000)
        self.assertEqual(pay.unallocated_amount, 20000)
        self.assertEqual(customer_credit_balance(customer), 20000)
        # GL: DN debited AR 20k; the applied receipt credits AR 20k (nets to zero);
        # the 20k excess lands in customer credit (2140).
        ar_bal = AccountBalance.objects.get(account__code="1200", period=period)
        cc_bal = AccountBalance.objects.get(account__code="2140", period=period)
        self.assertEqual(ar_bal.debit_total, 20000)
        self.assertEqual(ar_bal.credit_total, 20000)
        self.assertEqual(cc_bal.credit_total, 20000)

    # Verify explicit receipt allocation to debit note behavior.
    def test_explicit_receipt_allocation_to_debit_note(self):
        # An explicit allocation plan can target a debit note directly.
        entity, period, customer, _ = self.build_ar()
        bank = Account.objects.get(entity=entity, code="1100")
        note = self._post_debit_note(
            entity, customer, amount=20000, date=datetime.date(2026, 1, 10))

        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=15000, deposit_account=bank,
        )
        post_payment(pay, auto_allocate=False, allocations=[(note, 15000)])
        note.refresh_from_db()
        pay.refresh_from_db()
        self.assertEqual(note.amount_paid, 15000)
        self.assertEqual(note.balance_due, 5000)
        self.assertEqual(note.settlement_status, InvoicePaymentStatus.PARTIAL)
        self.assertEqual(pay.allocated_amount, 15000)

    # Verify stored credit settles debit note behavior.
    def test_stored_credit_settles_debit_note(self):
        # A receipt posted before the debit note leaves stored credit; allocating it
        # later drains the credit onto the open debit note.
        entity, period, customer, _ = self.build_ar()
        bank = Account.objects.get(entity=entity, code="1100")
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 5),
            amount=20000, deposit_account=bank,
        )
        post_payment(pay)  # no open items → 20k stored credit
        self.assertEqual(customer_credit_balance(customer), 20000)

        note = self._post_debit_note(
            entity, customer, amount=20000, date=datetime.date(2026, 1, 10))
        # A fresh debit note offsets the refundable credit until settled.
        self.assertEqual(customer_credit_balance(customer), 0)

        allocate_payment(pay)  # drain stored credit onto the debit note
        note.refresh_from_db()
        self.assertEqual(note.balance_due, 0)
        self.assertEqual(note.settlement_status, InvoicePaymentStatus.PAID)
        cc_bal = AccountBalance.objects.get(account__code="2140", period=period)
        self.assertEqual(cc_bal.credit_total, 20000)  # booked on receipt
        self.assertEqual(cc_bal.debit_total, 20000)   # reclassed onto the DN → net 0

    # Verify receipt allocates across invoice and debit note oldest first behavior.
    def test_receipt_allocates_across_invoice_and_debit_note_oldest_first(self):
        # Mixed open items settle oldest-first regardless of document type.
        entity, period, customer, _ = self.build_ar()
        bank = Account.objects.get(entity=entity, code="1100")
        note = self._post_debit_note(
            entity, customer, amount=30000, date=datetime.date(2026, 1, 8))
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        inv.invoice_date = datetime.date(2026, 1, 20)
        inv.due_date = datetime.date(2026, 1, 20)
        inv.save(update_fields=["invoice_date", "due_date"])
        post_invoice(inv)

        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 25),
            amount=40000, deposit_account=bank,
        )
        post_payment(pay)  # oldest-first → DN (Jan 8) fully, then 10k onto the invoice
        note.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(note.balance_due, 0)
        self.assertEqual(inv.amount_paid, 10000)
        self.assertEqual(inv.balance_due, 40000)

    # Verify credit note revenue line carries cost centre to gl behavior.
    def test_credit_note_revenue_line_carries_cost_centre_to_gl(self):
        from .models import CostCenter

        entity, period, customer, _ = self.build_ar()
        pri = CostCenter.objects.create(entity=entity, code="PRI", name="Primary")
        note = CreditNote.objects.create(
            entity=entity, customer=customer, kind=CreditNoteKind.CREDIT,
            note_date=datetime.date(2026, 1, 15), reason="Returned goods",
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=Account.objects.get(entity=entity, code="4900"),
            quantity=1, unit_price=40000, tax_code=None, cost_center=pri, line_no=1,
        )
        post_credit_note(note)
        # The revenue/returns GL line (Dr 4900) carries the cost centre.
        returns_line = note.journal.lines.get(account__code="4900")
        self.assertEqual(returns_line.cost_center.code, "PRI")
        self.assertEqual(returns_line.debit, 40000)

    # Verify overpayment books excess as customer credit behavior.
    def test_overpayment_books_excess_as_customer_credit(self):
        # A receipt larger than the invoice settles AR and books the excess as a
        # customer-credit liability (2140) — AR never carries a credit balance.
        entity, period, customer, vat = self.build_ar()
        bank = Account.objects.get(entity=entity, code="1100")
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        post_invoice(inv)
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=150000, deposit_account=bank,
        )
        post_payment(pay)  # auto-allocates oldest-first
        inv.refresh_from_db()
        pay.refresh_from_db()
        self.assertEqual(inv.balance_due, 0)
        self.assertEqual(pay.allocated_amount, 100000)
        self.assertEqual(pay.unallocated_amount, 50000)
        ar_bal = AccountBalance.objects.get(account__code="1200", period=period)
        cc_bal = AccountBalance.objects.get(account__code="2140", period=period)
        self.assertEqual(ar_bal.debit_total, 100000)   # invoice
        self.assertEqual(ar_bal.credit_total, 100000)  # applied portion of the receipt
        self.assertEqual(cc_bal.credit_total, 50000)   # excess → customer-credit liability
        self.assertEqual(customer_credit_balance(customer), 50000)

    # Verify apply stored credit reclasses to ar behavior.
    def test_apply_stored_credit_reclasses_to_ar(self):
        # Stored customer credit applied to a later invoice moves 2140 → AR.
        entity, period, customer, vat = self.build_ar()
        bank = Account.objects.get(entity=entity, code="1100")
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=50000, deposit_account=bank,
        )
        post_payment(pay)  # no invoices yet → all 50,000 → 2140
        self.assertEqual(customer_credit_balance(customer), 50000)
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(inv)
        allocate_payment(pay)  # apply the stored credit to the new invoice
        inv.refresh_from_db()
        self.assertEqual(inv.balance_due, 0)
        self.assertEqual(customer_credit_balance(customer), 0)
        cc_bal = AccountBalance.objects.get(account__code="2140", period=period)
        self.assertEqual(cc_bal.credit_total, 50000)   # booked on receipt
        self.assertEqual(cc_bal.debit_total, 50000)    # reclassed out on apply → net 0

    # Verify refund draws down customer credit behavior.
    def test_refund_draws_down_customer_credit(self):
        # A refund pays out a credit balance: Dr 2140 (customer credit), Cr bank.
        entity, period, customer, vat = self.build_ar()
        bank = Account.objects.get(entity=entity, code="1100")
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=30000, deposit_account=bank,
        )
        post_payment(pay)  # → 30,000 customer credit
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
        cc_bal = AccountBalance.objects.get(account__code="2140", period=period)
        bank_bal = AccountBalance.objects.get(account__code="1100", period=period)
        self.assertEqual(cc_bal.debit_total, 30000)    # refund draws down the liability
        self.assertEqual(bank_bal.credit_total, 30000)  # cash out
        self.assertEqual(customer_credit_balance(customer), 0)

    # Verify refund capped at available credit behavior.
    def test_refund_capped_at_available_credit(self):
        entity, period, customer, vat = self.build_ar()
        bank = Account.objects.get(entity=entity, code="1100")
        refund = Refund.objects.create(
            entity=entity, customer=customer, refund_date=datetime.date(2026, 1, 18),
            amount=30000, deposit_account=bank,
        )
        with self.assertRaises(PostingError):
            post_refund(refund)  # no credit available

    # Verify write off clears balance as bad debt behavior.
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

    # Verify write off rejected when nothing outstanding behavior.
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


# Group tests for Concession Tests.
class ConcessionTests(_ARFixtureMixin, TestCase):
    # Verify discount reduces invoice and posts to allowances behavior.
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

    # Verify concession rejected when amount exceeds balance behavior.
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


# Group tests for Payment Plan Tests.
class PaymentPlanTests(_ARFixtureMixin, TestCase):
    # Verify split amount is integer exact behavior.
    def test_split_amount_is_integer_exact(self):
        parts = split_amount(100000, 3)
        self.assertEqual(parts, [33333, 33333, 33334])
        self.assertEqual(sum(parts), 100000)

    # Verify plan builds dated installments and tracks settlement behavior.
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

    # Verify receipt auto refreshes linked plan behavior.
    def test_receipt_auto_refreshes_linked_plan(self):
        # A receipt advances the plan on its own — no manual refresh_plan_progress call.
        entity, period, customer, _ = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        post_invoice(inv)
        plan = PaymentPlan.objects.create(
            entity=entity, customer=customer, invoice=inv,
            start_date=datetime.date(2026, 1, 10), frequency="MONTHLY",
            installment_count=4, total_amount=inv.balance_due,
        )
        build_installments(plan)
        activate_payment_plan(plan)

        bank = Account.objects.get(entity=entity, code="1100")
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 12),
            amount=50000, deposit_account=bank,
        )
        post_payment(pay)  # deliberately NO refresh_plan_progress(plan) here
        plan.refresh_from_db()
        statuses = [i.status for i in plan.installments.order_by("seq_no")]
        self.assertEqual(statuses, ["PAID", "PAID", "PENDING", "PENDING"])
        self.assertEqual(plan.settled_total, 50000)
        self.assertEqual(plan.plan_status, "ACTIVE")

    # Verify pre plan waiver does not pre settle installments behavior.
    def test_pre_plan_waiver_does_not_pre_settle_installments(self):
        """A waiver applied before the plan reduces the spread total but must NOT count
        as an installment payment — the first installment stays fully PENDING."""
        entity, period, customer, _ = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 3225000, None)])
        post_invoice(inv)  # ₦3,225,000 outstanding

        # 10% waiver → 322,500 credited, balance 2,902,500.
        waiver = Concession.objects.create(
            entity=entity, customer=customer, invoice=inv, kind="WAIVER",
            concession_date=datetime.date(2026, 1, 12), amount=322500,
        )
        post_concession(waiver)
        inv.refresh_from_db()
        self.assertEqual(inv.balance_due, 2902500)

        # Spread the outstanding balance over 3 monthly installments of 967,500.
        plan = PaymentPlan.objects.create(
            entity=entity, customer=customer, invoice=inv,
            start_date=datetime.date(2026, 1, 15), frequency="MONTHLY",
            installment_count=3, total_amount=inv.balance_due,
        )
        build_installments(plan)
        self.assertEqual([i.amount for i in plan.installments.order_by("seq_no")],
                         [967500, 967500, 967500])
        activate_payment_plan(plan)
        plan.refresh_from_db()

        # The waiver is the plan's baseline — nothing is pre-settled.
        self.assertEqual(plan.baseline_settled, 322500)
        installs = list(plan.installments.order_by("seq_no"))
        self.assertEqual([i.status for i in installs], ["PENDING", "PENDING", "PENDING"])
        self.assertEqual(installs[0].amount_settled, 0)
        self.assertEqual(installs[0].balance, 967500)

        # A real ₦967,500 payment then fully settles installment #1 only.
        bank = Account.objects.get(entity=entity, code="1100")
        pay = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 16),
            amount=967500, deposit_account=bank,
        )
        post_payment(pay)  # auto-refreshes the linked plan
        plan.refresh_from_db()
        self.assertEqual([i.status for i in plan.installments.order_by("seq_no")],
                         ["PAID", "PENDING", "PENDING"])

        # Paying the remaining two installments completes the plan.
        pay2 = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 22),
            amount=1935000, deposit_account=bank,
        )
        post_payment(pay2)
        plan.refresh_from_db()
        self.assertEqual(plan.plan_status, "COMPLETED")
        self.assertTrue(all(i.status == "PAID" for i in plan.installments.all()))

    # Verify build rejects mismatched explicit amounts behavior.
    def test_build_rejects_mismatched_explicit_amounts(self):
        entity, period, customer, vat = self.build_ar()
        plan = PaymentPlan.objects.create(
            entity=entity, customer=customer,
            start_date=datetime.date(2026, 1, 10), frequency="WEEKLY",
            installment_count=2, total_amount=100000,
        )
        with self.assertRaises(PostingError):
            build_installments(plan, amounts=[40000, 40000])  # sums to 80,000 ≠ 100,000

    # Verify activate requires a built schedule behavior.
    def test_activate_requires_a_built_schedule(self):
        entity, period, customer, vat = self.build_ar()
        plan = PaymentPlan.objects.create(
            entity=entity, customer=customer,
            start_date=datetime.date(2026, 1, 10), frequency="MONTHLY",
            installment_count=3, total_amount=90000,
        )
        with self.assertRaises(PostingError):
            activate_payment_plan(plan)

    # Verify cancel marks plan cancelled behavior.
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


# Group tests for Customer Statement Tests.
class CustomerStatementTests(_ARFixtureMixin, TestCase):
    # Verify statement runs balance and buckets open invoices behavior.
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

    # Verify start date folds prior movements into opening balance behavior.
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


# Group tests for Dunning Tests.
class DunningTests(_ARFixtureMixin, TestCase):
    # Verify ensure default policy is idempotent with a ladder behavior.
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

    # Verify generate advances one rung lowest unissued first behavior.
    def test_generate_advances_one_rung_lowest_unissued_first(self):
        entity, period, customer, vat = self.build_ar()
        ensure_default_policy(entity)
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)],
                                due=datetime.date(2026, 1, 25))
        post_invoice(inv)  # 100,000 outstanding, due 25 Jan

        # 35 days late qualifies for all three rungs, but a run advances ONE step —
        # the lowest rung not yet issued (L1), not straight to the final notice.
        notices = generate_dunning(entity, as_of=datetime.date(2026, 3, 1))
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertEqual(notice.level, 1)            # lowest unissued qualifying rung
        self.assertEqual(notice.notice_status, "PENDING")
        self.assertEqual(notice.amount_due, 100000)
        self.assertEqual(notice.days_overdue, 35)
        self.assertTrue(notice.document_number.startswith("CFX-TBOOK-DUN-"))
        self.assertTrue(
            FinanceAuditLog.objects.filter(action="DUNNING_RUN_GENERATED").exists()
        )

    # Verify generate escalates one rung per run date behavior.
    def test_generate_escalates_one_rung_per_run_date(self):
        entity, period, customer, vat = self.build_ar()
        ensure_default_policy(entity)
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)],
                                due=datetime.date(2026, 1, 25))
        post_invoice(inv)
        # Runs on three successive dates climb one rung each (never skipping).
        for d, lvl in ((datetime.date(2026, 3, 1), 1),
                       (datetime.date(2026, 3, 2), 2),
                       (datetime.date(2026, 3, 3), 3)):
            self.assertEqual([n.level for n in generate_dunning(entity, as_of=d)], [lvl])
        # Nothing left to escalate after the final rung.
        self.assertEqual(generate_dunning(entity, as_of=datetime.date(2026, 3, 4)), [])
        self.assertEqual(
            sorted(DunningNotice.objects.filter(invoice=inv).values_list("level", flat=True)),
            [1, 2, 3],
        )

    # Verify generate is idempotent per run date behavior.
    def test_generate_is_idempotent_per_run_date(self):
        entity, period, customer, vat = self.build_ar()
        ensure_default_policy(entity)
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 80000, None)],
                                due=datetime.date(2026, 1, 25))
        post_invoice(inv)

        # Two runs on the SAME date advance only one rung total (re-runs are no-ops).
        first = generate_dunning(entity, as_of=datetime.date(2026, 2, 20))
        second = generate_dunning(entity, as_of=datetime.date(2026, 2, 20))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].level, 1)  # lowest unissued rung first
        self.assertEqual(len(second), 0)     # same run date → no further escalation
        self.assertEqual(DunningNotice.objects.filter(invoice=inv).count(), 1)

    # Verify not yet due invoice is skipped behavior.
    def test_not_yet_due_invoice_is_skipped(self):
        entity, period, customer, vat = self.build_ar()
        ensure_default_policy(entity)
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)],
                                due=datetime.date(2026, 1, 25))
        post_invoice(inv)
        notices = generate_dunning(entity, as_of=datetime.date(2026, 1, 20))
        self.assertEqual(notices, [])

    # Verify settled invoice marks notice resolved and no new one behavior.
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

    # Verify mark sent then cancel lifecycle behavior.
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


# Group tests for Phase4 Fixture Mixin.
class _Phase4FixtureMixin(_GLFixtureMixin):
    """A ledger with a full year of monthly periods and a bank account on 1100."""

    # Prepare or verify the build books test path.
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

    # Prepare or verify the make bank test path.
    def make_bank(self, entity, *, gl_code="1100"):
        return BankAccount.objects.create(
            entity=entity, name="GTBank Operations",
            gl_account=Account.objects.get(entity=entity, code=gl_code),
        )


# Group tests for Bank Reconciliation Tests.
class BankReconciliationTests(_Phase4FixtureMixin, TestCase):
    # Verify import is idempotent on external id behavior.
    def test_import_is_idempotent_on_external_id(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        rows = [
            {"txn_date": datetime.date(2026, 1, 5), "amount": 50000, "external_id": "A1"},
            {"txn_date": datetime.date(2026, 1, 6), "amount": -2000, "external_id": "A2"},
        ]
        _, created, _ = import_statement_lines(bank, rows)
        self.assertEqual(len(created), 2)
        # Re-import the same export: nothing new.
        _, again, _ = import_statement_lines(bank, rows)
        self.assertEqual(again, [])
        self.assertEqual(BankStatementLine.objects.filter(bank_account=bank).count(), 2)

    # Verify reimport without external id is held back as suspected behavior.
    def test_reimport_without_external_id_is_held_back_as_suspected(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        rows = [{"txn_date": datetime.date(2026, 1, 5), "amount": -1500,
                 "description": "Monthly fee"}]
        _, created, suspected = import_statement_lines(bank, rows)
        self.assertEqual(len(created), 1)
        self.assertEqual(suspected, [])
        # Re-import the same content (no external_id): held back, not duplicated.
        _, created2, suspected2 = import_statement_lines(bank, rows)
        self.assertEqual(created2, [])
        self.assertEqual(len(suspected2), 1)
        self.assertEqual(BankStatementLine.objects.filter(bank_account=bank).count(), 1)
        # force=True imports it anyway (a genuine repeat charge).
        _, created3, _ = import_statement_lines(bank, rows, force=True)
        self.assertEqual(len(created3), 1)
        self.assertEqual(BankStatementLine.objects.filter(bank_account=bank).count(), 2)

    # Verify identical lines in one fresh batch are both kept behavior.
    def test_identical_lines_in_one_fresh_batch_are_both_kept(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        # Two genuinely identical same-day charges in one upload → both imported.
        rows = [
            {"txn_date": datetime.date(2026, 1, 5), "amount": -1500, "description": "Fee"},
            {"txn_date": datetime.date(2026, 1, 5), "amount": -1500, "description": "Fee"},
        ]
        _, created, suspected = import_statement_lines(bank, rows)
        self.assertEqual(len(created), 2)
        self.assertEqual(suspected, [])

    # Verify auto reconcile leaves ambiguous ties unmatched behavior.
    def test_auto_reconcile_leaves_ambiguous_ties_unmatched(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        # Two GL cash inflows of +50,000 on the same date — a statement line of +50,000
        # has two equally-good candidates, so auto-match must leave it for a human.
        for _ in range(2):
            post_journal(self.make_entry(
                entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],
                date=datetime.date(2026, 1, 15)))
        import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 16), "amount": 50000, "external_id": "S1"}])
        matched = auto_reconcile(bank, tolerance_days=4)
        self.assertEqual(matched, [])
        self.assertEqual(
            BankStatementLine.objects.get(external_id="S1").status, BankLineStatus.UNMATCHED)

    # Verify group match pairs many gl lines to one statement line behavior.
    def test_group_match_pairs_many_gl_lines_to_one_statement_line(self):
        from vs_finance.banking import group_match, unmatch_line, _unmatched_gl_lines
        from vs_finance.exceptions import BankReconciliationError

        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        # Two receipts of 30,000 and 20,000 land as one 50,000 bank settlement line.
        e1 = self.make_entry(entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)],
                             date=datetime.date(2026, 1, 15))
        e2 = self.make_entry(entity, periods[0], [("1100", 20000, 0), ("4100", 0, 20000)],
                             date=datetime.date(2026, 1, 15))
        post_journal(e1)
        post_journal(e2)
        gl1 = e1.lines.get(account__code="1100")
        gl2 = e2.lines.get(account__code="1100")
        _, lines, _ = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 16), "amount": 50000}])
        sline = lines[0]

        # Wrong total is rejected; the correct pair matches.
        with self.assertRaises(BankReconciliationError):
            group_match(sline, [gl1])  # needs ≥2
        group_match(sline, [gl1, gl2])
        sline.refresh_from_db()
        self.assertEqual(sline.status, BankLineStatus.MATCHED)
        self.assertEqual(sline.line_matches.count(), 2)
        # Both GL lines drop out of the unmatched "book" side.
        self.assertNotIn(gl1.id, {l.id for l in _unmatched_gl_lines(bank)})
        self.assertNotIn(gl2.id, {l.id for l in _unmatched_gl_lines(bank)})

        # Unmatch drops the group links and frees the GL lines again.
        unmatch_line(sline)
        sline.refresh_from_db()
        self.assertEqual(sline.status, BankLineStatus.UNMATCHED)
        self.assertEqual(sline.line_matches.count(), 0)
        self.assertIn(gl1.id, {l.id for l in _unmatched_gl_lines(bank)})

    # Verify split match pairs one gl line to many statement lines behavior.
    def test_split_match_pairs_one_gl_line_to_many_statement_lines(self):
        from vs_finance.banking import split_match, unmatch_line, _unmatched_gl_lines
        from vs_finance.exceptions import BankReconciliationError

        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        # One 50,000 ledger movement the bank reported as two lines (30k + 20k).
        entry = self.make_entry(entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],
                                date=datetime.date(2026, 1, 15))
        post_journal(entry)
        gl = entry.lines.get(account__code="1100")
        _, lines, _ = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 16), "amount": 30000, "external_id": "A"},
            {"txn_date": datetime.date(2026, 1, 16), "amount": 20000, "external_id": "B"}])
        a, b = lines

        with self.assertRaises(BankReconciliationError):
            split_match(gl, [a])  # needs ≥2
        split_match(gl, [a, b])
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.status, BankLineStatus.MATCHED)
        self.assertEqual(b.status, BankLineStatus.MATCHED)
        self.assertEqual(list(_unmatched_gl_lines(bank)), [])  # the GL line is matched

        # Unmatching one split line frees just it; the GL line stays matched to the other.
        unmatch_line(a)
        a.refresh_from_db()
        self.assertEqual(a.status, BankLineStatus.UNMATCHED)
        self.assertEqual(list(_unmatched_gl_lines(bank)), [])  # gl still linked to B

    # Verify split match rejects mismatched total behavior.
    def test_split_match_rejects_mismatched_total(self):
        from vs_finance.banking import split_match
        from vs_finance.exceptions import BankReconciliationError

        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        entry = self.make_entry(entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)])
        post_journal(entry)
        gl = entry.lines.get(account__code="1100")
        _, lines, _ = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 16), "amount": 30000},
            {"txn_date": datetime.date(2026, 1, 16), "amount": 25000}])
        with self.assertRaises(BankReconciliationError):
            split_match(gl, lines)

    # Verify ignore line excludes it from unmatched behavior.
    def test_ignore_line_excludes_it_from_unmatched(self):
        from vs_finance.banking import set_line_ignored
        from vs_finance.exceptions import BankReconciliationError
        from vs_finance.constants import BankLineStatus

        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        _, lines, _ = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 5), "amount": 10000, "description": "Opening"}])
        line = lines[0]
        set_line_ignored(line)
        line.refresh_from_db()
        self.assertEqual(line.status, BankLineStatus.IGNORED)
        # Ignored lines don't count as unmatched.
        self.assertEqual(
            bank.statement_lines.filter(status=BankLineStatus.UNMATCHED).count(), 0)
        # Revert; a matched line can't be ignored.
        set_line_ignored(line, ignored=False)
        line.refresh_from_db()
        self.assertEqual(line.status, BankLineStatus.UNMATCHED)

    # Verify auto reconcile group sums gl lines to one bank line behavior.
    def test_auto_reconcile_group_sums_gl_lines_to_one_bank_line(self):
        from vs_finance.banking import auto_reconcile, _unmatched_gl_lines

        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        # Two receipts (30k + 20k) land as one 50,000 bank settlement line — no single
        # GL line equals 50,000, but their sum does.
        e1 = self.make_entry(entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)],
                             date=datetime.date(2026, 1, 15))
        e2 = self.make_entry(entity, periods[0], [("1100", 20000, 0), ("4100", 0, 20000)],
                             date=datetime.date(2026, 1, 15))
        post_journal(e1)
        post_journal(e2)
        _, lines, _ = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 16), "amount": 50000, "external_id": "S1"}])
        matched = auto_reconcile(bank, tolerance_days=4)
        self.assertEqual([m.external_id for m in matched], ["S1"])
        sline = BankStatementLine.objects.get(external_id="S1")
        self.assertEqual(sline.status, BankLineStatus.MATCHED)
        self.assertEqual(sline.line_matches.count(), 2)
        self.assertEqual(list(_unmatched_gl_lines(bank)), [])  # both GL lines consumed
        # group=False disables the second pass — nothing groups.
        _, l2, _ = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 17), "amount": 50000, "external_id": "S2"}])
        e3 = self.make_entry(entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)],
                             date=datetime.date(2026, 1, 17))
        e4 = self.make_entry(entity, periods[0], [("1100", 20000, 0), ("4100", 0, 20000)],
                             date=datetime.date(2026, 1, 17))
        post_journal(e3)
        post_journal(e4)
        self.assertEqual(auto_reconcile(bank, tolerance_days=4, group=False), [])

    # Verify group match rejects mismatched total behavior.
    def test_group_match_rejects_mismatched_total(self):
        from vs_finance.banking import group_match
        from vs_finance.exceptions import BankReconciliationError

        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        e1 = self.make_entry(entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)])
        e2 = self.make_entry(entity, periods[0], [("1100", 20000, 0), ("4100", 0, 20000)])
        post_journal(e1)
        post_journal(e2)
        _, lines, _ = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 16), "amount": 60000}])
        with self.assertRaises(BankReconciliationError):
            group_match(lines[0], [e1.lines.get(account__code="1100"),
                                   e2.lines.get(account__code="1100")])

    # Verify auto reconcile matches by amount and date behavior.
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

    # Verify manual match rejects amount mismatch behavior.
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
        ])[1][0]
        with self.assertRaises(BankReconciliationError):
            match_line(line, gl_line)
        # Correct amount matches cleanly.
        line.amount = 30000
        line.save(update_fields=["amount"])
        match_line(line, gl_line)
        line.refresh_from_db()
        self.assertEqual(line.status, BankLineStatus.MATCHED)

    # Verify post bank adjustment books charge and matches behavior.
    def test_post_bank_adjustment_books_charge_and_matches(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        line = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 20), "amount": -1500,
             "description": "Monthly fee"},
        ])[1][0]
        entry = post_bank_adjustment(line)
        line.refresh_from_db()
        self.assertEqual(line.status, BankLineStatus.MATCHED)
        self.assertEqual(line.adjusting_journal_id, entry.id)
        # Outflow: Dr 5500 Bank Charges, Cr 1100 cash.
        charge = entry.lines.get(account__code="5500")
        cash = entry.lines.get(account__code="1100")
        self.assertEqual(charge.debit, 1500)
        self.assertEqual(cash.credit, 1500)

    # Verify adjustment rejects already matched line behavior.
    def test_adjustment_rejects_already_matched_line(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        line = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 20), "amount": -1500},
        ])[1][0]
        post_bank_adjustment(line)
        with self.assertRaises(BankReconciliationError):
            post_bank_adjustment(line)

    # Verify import groups lines under a statement behavior.
    def test_import_groups_lines_under_a_statement(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        statement, lines, _ = import_statement_lines(
            bank, [
                {"txn_date": datetime.date(2026, 1, 5), "amount": 50000},
                {"txn_date": datetime.date(2026, 1, 6), "amount": -2000},
            ], period_label="Jan 2026", opening_balance=10000)
        self.assertIsNotNone(statement)
        self.assertEqual(statement.period_label, "Jan 2026")
        self.assertEqual(statement.opening_balance, 10000)
        # Closing derived = opening + Σ amounts = 10,000 + 48,000.
        self.assertEqual(statement.closing_balance, 58000)
        self.assertEqual(statement.line_count, 2)
        self.assertTrue(all(l.statement_id == statement.id for l in lines))

    # Verify auto reconcile records a reconciliation and closes statement behavior.
    def test_auto_reconcile_records_a_reconciliation_and_closes_statement(self):
        from vs_finance.models import BankReconciliation
        from vs_finance.constants import BankStatementStatus

        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],
            date=datetime.date(2026, 1, 15)))
        statement, _, _ = import_statement_lines(bank, [
            {"txn_date": datetime.date(2026, 1, 16), "amount": 50000}])
        auto_reconcile(bank, tolerance_days=4)

        recon = BankReconciliation.objects.filter(bank_account=bank).first()
        self.assertIsNotNone(recon)
        self.assertEqual(recon.matched_count, 1)
        self.assertEqual(recon.book_balance, 50000)
        statement.refresh_from_db()
        self.assertEqual(statement.status, BankStatementStatus.RECONCILED)


# Group tests for Expense Claim Tests.
class ExpenseClaimTests(_Phase4FixtureMixin, TestCase):
    # Support the make claim workflow.
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

    # Verify post raises liability with input vat behavior.
    def test_post_raises_liability_with_input_vat(self):
        entity, _, _ = self.build_books()
        vat = TaxCode.objects.create(
            entity=entity, code="VAT", name="VAT 7.5%", rate_bps=750,
            paid_account=Account.objects.get(entity=entity, code="1300"),
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

    # Verify settle partial then full behavior.
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

    # Verify cannot post empty claim behavior.
    def test_cannot_post_empty_claim(self):
        entity, _, _ = self.build_books()
        claim = ExpenseClaim.objects.create(
            entity=entity, claimant_name="Nobody",
            claim_date=datetime.date(2026, 1, 10),
        )
        with self.assertRaises(ExpenseClaimError):
            post_expense_claim(claim)

    # Verify void reverses journal and cancels unreimbursed claim behavior.
    def test_void_reverses_journal_and_cancels_unreimbursed_claim(self):
        entity, _, _ = self.build_books()
        claim = self._make_claim(entity, lines=[("5500", 1, 100000, None)])
        post_expense_claim(claim)
        journal = claim.journal
        void_expense_claim(claim)
        claim.refresh_from_db()
        journal.refresh_from_db()
        self.assertEqual(claim.status, DocumentStatus.CANCELLED)
        self.assertEqual(journal.status, DocumentStatus.REVERSED)
        # The reversal backs the liability and expense out to zero.
        self.assertTrue(
            FinanceAuditLog.objects.filter(action="EXPENSE_CLAIM_VOIDED").exists())

    # Verify void refused once reimbursed behavior.
    def test_void_refused_once_reimbursed(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        claim = self._make_claim(entity, lines=[("5500", 1, 100000, None)])
        post_expense_claim(claim)
        settle_expense_claim(claim, bank_account=bank, pay_date=datetime.date(2026, 1, 15),
                             amount=40000)
        with self.assertRaises(ExpenseClaimError):
            void_expense_claim(claim)  # cash already left → must reverse reimbursement first

    # Verify void refused on draft behavior.
    def test_void_refused_on_draft(self):
        entity, _, _ = self.build_books()
        claim = self._make_claim(entity, lines=[("5500", 1, 100000, None)])
        with self.assertRaises(ExpenseClaimError):
            void_expense_claim(claim)  # a draft is rejected, not voided


# Group tests for Cost Center Propagation Tests.
class CostCenterPropagationTests(_Phase4FixtureMixin, _ARFixtureMixin, TestCase):
    """Cost centres set on document lines must survive into the General Ledger.

    Regression for the gap where every sub-ledger posting aggregated lines by account
    only and dropped the cost centre. P&L lines (revenue/expense) now split by
    (account, cost centre); balance-sheet control and tax lines stay aggregated.
    """

    # Verify invoice revenue splits by cost centre in gl behavior.
    def test_invoice_revenue_splits_by_cost_centre_in_gl(self):
        from .models import CostCenter

        entity, period, customer, _ = self.build_ar()
        pri = CostCenter.objects.create(entity=entity, code="PRI", name="Primary")
        sec = CostCenter.objects.create(entity=entity, code="SEC", name="Secondary")
        inv = Invoice.objects.create(
            entity=entity, customer=customer,
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 25),
        )
        rev = Account.objects.get(entity=entity, code="4100")
        # Same revenue account, two cost centres → two GL lines, not one merged line.
        InvoiceLine.objects.create(invoice=inv, revenue_account=rev, quantity=1,
                                   unit_price=100000, cost_center=pri, line_no=1)
        InvoiceLine.objects.create(invoice=inv, revenue_account=rev, quantity=1,
                                   unit_price=50000, cost_center=sec, line_no=2)
        post_invoice(inv)
        inv.refresh_from_db()

        rev_lines = inv.journal.lines.filter(account__code="4100")
        by_cc = {ln.cost_center.code: ln.credit for ln in rev_lines}
        self.assertEqual(by_cc, {"PRI": 100000, "SEC": 50000})
        # AR control line stays unallocated (balance-sheet account).
        ar_line = inv.journal.lines.get(account__code="1200")
        self.assertIsNone(ar_line.cost_center_id)
        debit, credit = inv.journal.totals()
        self.assertEqual(debit, credit)

    # Verify expense claim expense line carries cost centre to gl behavior.
    def test_expense_claim_expense_line_carries_cost_centre_to_gl(self):
        from .models import CostCenter

        entity, _, _ = self.build_books()
        pri = CostCenter.objects.create(entity=entity, code="PRI", name="Primary")
        claim = ExpenseClaim.objects.create(
            entity=entity, claimant_name="Jane Staff",
            claim_date=datetime.date(2026, 1, 10), title="Trip",
        )
        ExpenseClaimLine.objects.create(
            claim=claim, expense_account=Account.objects.get(entity=entity, code="5500"),
            quantity=1, unit_price=100000, cost_center=pri, line_no=1,
        )
        post_expense_claim(claim)
        claim.refresh_from_db()
        exp_line = claim.journal.lines.get(account__code="5500")
        self.assertEqual(exp_line.cost_center.code, "PRI")
        self.assertEqual(exp_line.debit, 100000)


# Group tests for Petty Cash Tests.
class PettyCashTests(_Phase4FixtureMixin, TestCase):
    # Support the make fund workflow.
    def _make_fund(self, entity, *, name="Front Desk", float_amount=5000000, gl_code="1110"):
        return PettyCashFund.objects.create(
            entity=entity, name=name, custodian_name="Tunde Custodian",
            gl_account=Account.objects.get(entity=entity, code=gl_code),
            float_amount=float_amount,
        )

    # Support the make voucher workflow.
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

    # Verify establish moves cash from bank to tin behavior.
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

    # Verify establish rejects non positive behavior.
    def test_establish_rejects_non_positive(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = self._make_fund(entity)
        with self.assertRaises(PettyCashError):
            establish_fund(fund, bank_account=bank, amount=0, date=datetime.date(2026, 1, 1))

    # Verify voucher posts expense and lowers balance behavior.
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

    # Verify overdraw guard uses live gl and resyncs mirror behavior.
    def test_overdraw_guard_uses_live_gl_and_resyncs_mirror(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = self._make_fund(entity, float_amount=50000)
        establish_fund(fund, bank_account=bank, amount=50000, date=datetime.date(2026, 1, 1))
        # Corrupt the denormalised mirror to look flush; the GL still holds only 50,000.
        PettyCashFund.objects.filter(pk=fund.pk).update(current_balance=999999)
        over = self._make_voucher(fund, lines=[("5500", 1, 60000, None)])
        with self.assertRaises(PettyCashOverdrawError):
            post_voucher(over)  # guard reads the live GL (50,000), not the drifted mirror
        # A within-limit voucher posts and re-syncs the mirror to the true GL balance.
        ok = self._make_voucher(fund, lines=[("5500", 1, 40000, None)])
        post_voucher(ok)
        fund.refresh_from_db()
        self.assertEqual(fund.current_balance, 10000)
        self.assertEqual(gl_cash_on_hand(fund), 10000)

    # Verify void voucher reverses journal and returns cash behavior.
    def test_void_voucher_reverses_journal_and_returns_cash(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = self._make_fund(entity, float_amount=5000000)
        establish_fund(fund, bank_account=bank, amount=5000000, date=datetime.date(2026, 1, 1))
        voucher = self._make_voucher(fund, lines=[("5500", 1, 100000, None)])
        post_voucher(voucher)
        journal = voucher.journal
        fund.refresh_from_db()
        self.assertEqual(fund.current_balance, 4900000)
        void_voucher(voucher)
        voucher.refresh_from_db(); fund.refresh_from_db(); journal.refresh_from_db()
        self.assertEqual(voucher.status, DocumentStatus.CANCELLED)
        self.assertEqual(journal.status, DocumentStatus.REVERSED)
        self.assertEqual(fund.current_balance, 5000000)  # cash back in the tin
        self.assertEqual(gl_cash_on_hand(fund), 5000000)
        self.assertTrue(
            FinanceAuditLog.objects.filter(action="PETTY_CASH_VOUCHER_VOIDED").exists())

    # Verify void refused on draft voucher behavior.
    def test_void_refused_on_draft_voucher(self):
        entity, _, _ = self.build_books()
        fund = self._make_fund(entity)
        draft = self._make_voucher(fund, lines=[("5500", 1, 10000, None)])
        with self.assertRaises(PettyCashError):
            void_voucher(draft)

    # Verify voucher overdraw is blocked and audited behavior.
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

    # Verify replenish restores float by default behavior.
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

    # Verify replenish with nothing to top up is rejected behavior.
    def test_replenish_with_nothing_to_top_up_is_rejected(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = self._make_fund(entity, float_amount=5000000)
        establish_fund(fund, bank_account=bank, amount=5000000, date=datetime.date(2026, 1, 1))
        with self.assertRaises(PettyCashError):
            replenish_fund(fund, bank_account=bank, date=datetime.date(2026, 1, 31))

    # Verify fund status flags low balance behavior.
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


# Group tests for Tax Filing Tests.
class TaxFilingTests(_Phase4FixtureMixin, TestCase):
    # Support the vat obligation workflow.
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

    # Support the wht obligation workflow.
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

    # Support the accrue output vat workflow.
    def _accrue_output_vat(self, entity, period, *, net, vat, date=datetime.date(2026, 1, 10)):
        # A sale: Dr cash, Cr revenue, Cr output VAT.
        post_journal(self.make_entry(
            entity, period,
            [("1100", net + vat, 0), ("4100", 0, net), ("2200", 0, vat)],
            date=date,
        ))

    # Support the accrue input vat workflow.
    def _accrue_input_vat(self, entity, period, *, net, vat, date=datetime.date(2026, 1, 12)):
        # A purchase: Dr expense, Dr input VAT, Cr cash.
        post_journal(self.make_entry(
            entity, period,
            [("5300", net, 0), ("1300", vat, 0), ("1100", 0, net + vat)],
            date=date,
        ))

    # Support the accrue wht workflow.
    def _accrue_wht(self, entity, period, *, amount, date=datetime.date(2026, 1, 12)):
        # A vendor payment withholding: Dr expense, Cr WHT payable, Cr cash.
        post_journal(self.make_entry(
            entity, period,
            [("5300", amount * 10, 0), ("2300", 0, amount), ("1100", 0, amount * 9)],
            date=date,
        ))

    # Verify prepare defaults due date from filing day behavior.
    def test_prepare_defaults_due_date_from_filing_day(self):
        # filing_day defaults to 21 → day 21 of the month after period_end.
        entity, _, _ = self.build_books()
        ob = self._vat_obligation(entity)
        filing = prepare_filing(
            ob, period_start=datetime.date(2026, 6, 1),
            period_end=datetime.date(2026, 6, 30))
        self.assertEqual(filing.due_date, datetime.date(2026, 7, 21))

    # Verify prepare clamps due day to short following month behavior.
    def test_prepare_clamps_due_day_to_short_following_month(self):
        # period_end March 31 → April (30 days); filing_day 31 clamps to Apr 30.
        entity, _, _ = self.build_books()
        ob = self._vat_obligation(entity)
        ob.filing_day = 31
        ob.save(update_fields=["filing_day"])
        filing = prepare_filing(
            ob, period_start=datetime.date(2026, 3, 1),
            period_end=datetime.date(2026, 3, 31))
        self.assertEqual(filing.due_date, datetime.date(2026, 4, 30))

    # Verify prepare respects explicit due date behavior.
    def test_prepare_respects_explicit_due_date(self):
        entity, _, _ = self.build_books()
        ob = self._vat_obligation(entity)
        filing = prepare_filing(
            ob, period_start=datetime.date(2026, 6, 1),
            period_end=datetime.date(2026, 6, 30),
            due_date=datetime.date(2026, 7, 5))
        self.assertEqual(filing.due_date, datetime.date(2026, 7, 5))

    # Verify prepare vat nets input against output behavior.
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

    # Verify prepare is idempotent for same period behavior.
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

    # Verify overlapping period is rejected behavior.
    def test_overlapping_period_is_rejected(self):
        entity, _, periods = self.build_books()
        ob = self._wht_obligation(entity)
        self._accrue_wht(entity, periods[0], amount=50000)
        prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                       period_end=datetime.date(2026, 1, 31))
        # A different-but-overlapping window (mid-Jan into Feb) clashes.
        with self.assertRaises(TaxFilingError):
            prepare_filing(ob, period_start=datetime.date(2026, 1, 15),
                           period_end=datetime.date(2026, 2, 15))

    # Verify adjacent non overlapping period is accepted behavior.
    def test_adjacent_non_overlapping_period_is_accepted(self):
        entity, _, periods = self.build_books()
        ob = self._wht_obligation(entity)
        self._accrue_wht(entity, periods[0], amount=50000)
        jan = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                             period_end=datetime.date(2026, 1, 31))
        feb = prepare_filing(ob, period_start=datetime.date(2026, 2, 1),
                             period_end=datetime.date(2026, 2, 28))
        self.assertNotEqual(jan.pk, feb.pk)
        self.assertEqual(
            TaxFiling.objects.filter(entity=entity, obligation=ob).count(), 2)

    # Verify file nets input vat then pay clears liability behavior.
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

    # Verify wht filing no recoverable pays full behavior.
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
            if code == "2300":  # Branch test setup or assertions.
                self.assertEqual((agg["c"] or 0) - (agg["d"] or 0), 0)  # payable cleared
        # The bank-side remittance leg credited cash by 50,000.
        remit = JournalLine.objects.get(
            account__code="2300", entry__status=DocumentStatus.POSTED, debit=50000,
        )
        self.assertEqual(remit.entry.lines.get(account__code="1100").credit, 50000)

    # Verify partial remittance behavior.
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

    # Verify file with penalty books expense and raises due behavior.
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

    # Verify pay before file is rejected and audited behavior.
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

    # Verify unfile reverses netting journal and reverts to draft behavior.
    def test_unfile_reverses_netting_journal_and_reverts_to_draft(self):
        entity, _, periods = self.build_books()
        ob = self._vat_obligation(entity)
        self._accrue_output_vat(entity, periods[0], net=1000000, vat=75000)
        self._accrue_input_vat(entity, periods[0], net=400000, vat=30000)
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                                period_end=datetime.date(2026, 1, 31))
        file_filing(filing, filed_date=datetime.date(2026, 2, 5), filing_reference="VAT-202601")
        filing.refresh_from_db()
        netting = filing.filing_journal
        self.assertIsNotNone(netting)

        unfile_filing(filing)
        filing.refresh_from_db()
        self.assertEqual(filing.filing_status, TaxFilingStatus.DRAFT)
        self.assertIsNone(filing.filing_journal)
        self.assertEqual(filing.filing_reference, "")
        self.assertIsNone(filing.filed_at)
        # The netting journal is reversed (audit-correct undo), not edited.
        netting.refresh_from_db()
        self.assertEqual(netting.status, DocumentStatus.REVERSED)
        self.assertTrue(
            FinanceAuditLog.objects.filter(
                entity=entity, action=FinanceAuditAction.TAX_FILING_UNFILED,
            ).exists()
        )

    # Verify unfile refused once any payment made behavior.
    def test_unfile_refused_once_any_payment_made(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        ob = self._wht_obligation(entity)
        self._accrue_wht(entity, periods[0], amount=50000)
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                                period_end=datetime.date(2026, 1, 31))
        file_filing(filing, filed_date=datetime.date(2026, 2, 5))
        pay_filing(filing, bank_account=bank, pay_date=datetime.date(2026, 2, 10), amount=20000)
        filing.refresh_from_db()
        with self.assertRaises(TaxFilingError):
            unfile_filing(filing)
        filing.refresh_from_db()
        self.assertEqual(filing.filing_status, TaxFilingStatus.FILED)

    # Verify unfile refused on draft behavior.
    def test_unfile_refused_on_draft(self):
        entity, _, periods = self.build_books()
        ob = self._wht_obligation(entity)
        self._accrue_wht(entity, periods[0], amount=50000)
        filing = prepare_filing(ob, period_start=datetime.date(2026, 1, 1),
                                period_end=datetime.date(2026, 1, 31))
        with self.assertRaises(TaxFilingError):
            unfile_filing(filing)

    # Verify outstanding obligations reports net behavior.
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

    # Verify seed creates four nigerian obligations behavior.
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


# Group tests for Payroll Tests.
class PayrollTests(_Phase4FixtureMixin, TestCase):
    # Support the make run workflow.
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

    # Verify accrual posts balanced with statutory liabilities behavior.
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

    # Verify accrual splits gross salary by cost centre behavior.
    def test_accrual_splits_gross_salary_by_cost_centre(self):
        from .models import CostCenter

        entity, _, _ = self.build_books()
        pri = CostCenter.objects.create(entity=entity, code="PRI", name="Primary")
        sec = CostCenter.objects.create(entity=entity, code="SEC", name="Secondary")
        run = PayrollRun.objects.create(
            entity=entity, pay_date=datetime.date(2026, 1, 28), period_label="Jan 2026",
        )
        PayrollLine.objects.create(run=run, employee_name="Ada", gross_amount=300000,
                                   paye_amount=30000, pension_amount=15000, cost_center=pri, line_no=1)
        PayrollLine.objects.create(run=run, employee_name="Bola", gross_amount=200000,
                                   paye_amount=20000, pension_amount=10000, cost_center=sec, line_no=2)
        post_payroll(run)
        run.refresh_from_db()
        # Gross salary expense (5200) splits by cost centre; liabilities stay aggregated.
        salary_lines = run.journal.lines.filter(account__code="5200")
        by_cc = {ln.cost_center.code: ln.debit for ln in salary_lines}
        self.assertEqual(by_cc, {"PRI": 300000, "SEC": 200000})
        self.assertIsNone(run.journal.lines.get(account__code="2330").cost_center_id)
        debit, credit = run.journal.totals()
        self.assertEqual(debit, credit)

    # Verify disburse clears net payable behavior.
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

    # Verify negative net is rejected behavior.
    def test_negative_net_is_rejected(self):
        entity, _, _ = self.build_books()
        run = self._make_run(entity, lines=[("Greedy", 100000, 80000, 30000)])  # net -10,000
        with self.assertRaises(PayrollError):
            post_payroll(run)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.DRAFT)

    # Verify cannot pay unposted run behavior.
    def test_cannot_pay_unposted_run(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        run = self._make_run(entity, lines=[("Ada", 300000, 30000, 15000)])
        with self.assertRaises(PayrollError):
            pay_payroll(run, bank_account=bank)

    # Verify cancel draft run marks cancelled behavior.
    def test_cancel_draft_run_marks_cancelled(self):
        entity, _, _ = self.build_books()
        run = self._make_run(entity, lines=[("Ada", 300000, 30000, 15000)])
        cancel_payroll_run(run)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.CANCELLED)
        self.assertIsNone(run.journal_id)  # nothing was posted

    # Verify void posted run reverses accrual behavior.
    def test_void_posted_run_reverses_accrual(self):
        entity, _, _ = self.build_books()
        run = self._make_run(entity, lines=[("Ada", 300000, 30000, 15000)])
        post_payroll(run)
        run.refresh_from_db()
        journal = run.journal
        cancel_payroll_run(run)
        run.refresh_from_db(); journal.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.CANCELLED)
        self.assertEqual(journal.status, DocumentStatus.REVERSED)  # accrual backed out
        self.assertTrue(
            FinanceAuditLog.objects.filter(action="PAYROLL_CANCELLED").exists())

    # Verify void refused once paid behavior.
    def test_void_refused_once_paid(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        run = self._make_run(entity, lines=[("Ada", 300000, 30000, 15000)])
        post_payroll(run)
        pay_payroll(run, bank_account=bank)
        with self.assertRaises(PayrollError):
            cancel_payroll_run(run)  # net wages already left the bank


# Group tests for Budget Tests.
class BudgetTests(_Phase4FixtureMixin, TestCase):
    # Verify approve locks lines against edits behavior.
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

    # Verify period no must be in range behavior.
    def test_period_no_must_be_in_range(self):
        entity, year, _ = self.build_books()
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")
        salaries = Account.objects.get(entity=entity, code="5200")
        with self.assertRaises(BudgetError):
            add_budget_line(budget, account=salaries, period_no=13, amount=10000)

    # Verify delete draft budget removes lines and writes audit behavior.
    def test_delete_draft_budget_removes_lines_and_writes_audit(self):
        from vs_finance.budgets import delete_budget
        from vs_finance.models import BudgetLine

        entity, year, _ = self.build_books()
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")
        salaries = Account.objects.get(entity=entity, code="5200")
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)
        bid = budget.id
        delete_budget(budget)
        self.assertFalse(Budget.objects.filter(id=bid).exists())
        self.assertFalse(BudgetLine.objects.filter(budget_id=bid).exists())
        self.assertTrue(FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.BUDGET_DELETED, target_id=str(bid)).exists())

    # Verify delete approved budget refuses behavior.
    def test_delete_approved_budget_refuses(self):
        from vs_finance.budgets import delete_budget

        entity, year, _ = self.build_books()
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")
        approve_budget(budget)
        with self.assertRaises(BudgetError):
            delete_budget(budget)

    # Verify budget vs actual variance behavior.
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

    # Verify budget vs actual scoped to period behavior.
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

    # Verify budget monthly matrix builds per account cells behavior.
    def test_budget_monthly_matrix_builds_per_account_cells(self):
        entity, year, periods = self.build_books()
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")
        salaries = Account.objects.get(entity=entity, code="5200")
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)
        add_budget_line(budget, account=salaries, period_no=2, amount=60000)
        # Actual: 50,000 in Jan (period 1), 70,000 in Feb (period 2).
        post_journal(self.make_entry(
            entity, periods[0], [("5200", 50000, 0), ("1100", 0, 50000)],
            date=datetime.date(2026, 1, 15)))
        post_journal(self.make_entry(
            entity, periods[1], [("5200", 70000, 0), ("1100", 0, 70000)],
            date=datetime.date(2026, 2, 15)))
        matrix = budget_monthly_matrix(budget)
        self.assertEqual(len(matrix.periods), 12)
        row = next(r for r in matrix.rows if r.code == "5200")
        self.assertEqual(len(row.cells), 12)
        self.assertEqual(row.budget_total, 120000)
        self.assertEqual(row.actual_total, 120000)
        c1 = next(c for c in row.cells if c["period_no"] == 1)
        c2 = next(c for c in row.cells if c["period_no"] == 2)
        self.assertEqual((c1["budget"], c1["actual"]), (60000, 50000))
        self.assertEqual((c2["budget"], c2["actual"]), (60000, 70000))


# Group tests for Fixed Asset Tests.
class FixedAssetTests(_Phase4FixtureMixin, TestCase):
    # Support the make asset workflow.
    def _make_asset(self, entity, *, cost=1100000, salvage=0, life=11,
                    acq=datetime.date(2026, 1, 1)):
        return FixedAsset.objects.create(
            entity=entity, name="Server rack", acquisition_date=acq,
            cost=cost, salvage_value=salvage, useful_life_months=life,
        )

    # Verify acquire capitalises and builds schedule behavior.
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

    # Verify declining balance schedule front loads and lands on salvage behavior.
    def test_declining_balance_schedule_front_loads_and_lands_on_salvage(self):
        from vs_finance.constants import DepreciationMethod
        entity, _, _ = self.build_books()
        asset = self._make_asset(entity, cost=1200000, salvage=200000, life=12)
        asset.method = DepreciationMethod.DECLINING_BALANCE
        asset.save(update_fields=["method"])
        build_depreciation_schedule(asset)
        amounts = [r.amount for r in asset.schedule.all()]
        self.assertEqual(len(amounts), 12)
        # Sums to the depreciable base exactly (cost − salvage).
        self.assertEqual(sum(amounts), 1000000)
        # Front-loaded: first DB charge (2/12 of 1,200,000 = 200,000) beats straight-line.
        self.assertEqual(amounts[0], 200000)
        self.assertGreater(amounts[0], amounts[-1])
        # Never drives book value below salvage (every charge non-negative, monotone bv).
        bv = asset.cost
        for a in amounts:
            self.assertGreaterEqual(a, 0)
            bv -= a
        self.assertEqual(bv, asset.salvage_value)

    # Verify schedule remainder lands on last period behavior.
    def test_schedule_remainder_lands_on_last_period(self):
        entity, _, _ = self.build_books()
        asset = self._make_asset(entity, cost=1000000, salvage=0, life=3)
        build_depreciation_schedule(asset)
        amounts = [r.amount for r in asset.schedule.all()]
        # 1,000,000 / 3 = 333,333 r1 → last row carries the extra kobo.
        self.assertEqual(amounts, [333333, 333333, 333334])
        self.assertEqual(sum(amounts), 1000000)

    # Verify post depreciation runs and completes behavior.
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

    # Verify run period depreciation posts one compound journal behavior.
    def test_run_period_depreciation_posts_one_compound_journal(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        a1 = self._make_asset(entity, cost=1100000, salvage=0, life=11)
        a2 = self._make_asset(entity, cost=2200000, salvage=0, life=11)
        acquire_asset(a1, bank_account=bank)
        acquire_asset(a2, bank_account=bank)
        # Run everything due to Feb 2026: one charge each (100,000 + 200,000).
        result = run_period_depreciation(entity, up_to_date=datetime.date(2026, 2, 28))
        self.assertEqual(result["asset_count"], 2)
        self.assertEqual(result["total"], 300000)
        # One compound journal: Dr 5400 = 300,000, Cr 1900 = 300,000.
        from vs_finance.models import JournalEntry
        entry = JournalEntry.objects.get(id=result["journal_id"])
        self.assertEqual(entry.lines.get(account__code="5400").debit, 300000)
        self.assertEqual(entry.lines.get(account__code="1900").credit, 300000)
        a1.refresh_from_db()
        self.assertEqual(a1.accumulated_depreciation, 100000)

    # Verify run period depreciation spanning two periods posts two journals behavior.
    def test_run_period_depreciation_spanning_two_periods_posts_two_journals(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)
        acquire_asset(asset, bank_account=bank)
        # Charges due Feb 1 and Mar 1 (100,000 each). Run up to Mar 31.
        result = run_period_depreciation(entity, up_to_date=datetime.date(2026, 3, 31))
        self.assertEqual(result["period_count"], 2)
        self.assertEqual(len(result["journal_ids"]), 2)
        self.assertEqual(result["journal_id"], result["journal_ids"][0])
        self.assertEqual(result["total"], 200000)
        from vs_finance.models import JournalEntry
        entries = [JournalEntry.objects.get(id=j) for j in result["journal_ids"]]
        # Each journal is dated inside its own period and totals 100,000.
        for entry in entries:
            self.assertEqual(entry.lines.get(account__code="5400").debit, 100000)
            self.assertEqual(entry.lines.get(account__code="1900").credit, 100000)
            self.assertTrue(entry.period.start_date <= entry.date <= entry.period.end_date)
            self.assertLessEqual(entry.date, datetime.date(2026, 3, 31))
        # Chronological: first journal is February's.
        self.assertEqual(entries[0].period.period_no, 2)
        self.assertEqual(entries[1].period.period_no, 3)

    # Verify run period depreciation without fiscal period raises typed error behavior.
    def test_run_period_depreciation_without_fiscal_period_raises_typed_error(self):
        # Schedule charges extending past the last seeded period (FY2026 only) must
        # surface a DepreciationError naming the date, not an AttributeError/500.
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        # Acquired June 2026, 11 monthly charges → Jul 2026 … May 2027; no FY2027 exists.
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11,
                                 acq=datetime.date(2026, 6, 1))
        acquire_asset(asset, bank_account=bank)
        with self.assertRaises(DepreciationError) as ctx:
            run_period_depreciation(entity, up_to_date=datetime.date(2027, 5, 31))
        self.assertIn("No fiscal period covers", str(ctx.exception))
        self.assertIn("2027", str(ctx.exception))

    # Verify run period depreciation single period returns one journal behavior.
    def test_run_period_depreciation_single_period_returns_one_journal(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)
        acquire_asset(asset, bank_account=bank)
        result = run_period_depreciation(entity, up_to_date=datetime.date(2026, 2, 28))
        self.assertEqual(result["period_count"], 1)
        self.assertEqual(result["journal_ids"], [result["journal_id"]])
        self.assertEqual(result["total"], 100000)

    # Verify dispose asset books proceeds and gain loss behavior.
    def test_dispose_asset_books_proceeds_and_gain_loss(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)
        acquire_asset(asset, bank_account=bank)
        post_depreciation(asset, up_to_date=datetime.date(2026, 3, 31))
        asset.refresh_from_db()
        nbv = asset.net_book_value  # 900,000
        # Sell for 950,000 → 50,000 gain; gain to 4100 income. Dispose on Mar 31 so no
        # depreciation charge is yet due-but-unposted (the Apr 1 charge is future-dated).
        entry = dispose_asset(
            asset, disposal_date=datetime.date(2026, 3, 31), proceeds=950000,
            bank_account=bank, gain_loss_account=Account.objects.get(entity=entity, code="4100"))
        asset.refresh_from_db()
        self.assertEqual(asset.asset_status, AssetStatus.DISPOSED)
        # Dr 1900 accum (200,000) + Dr cash 950,000; Cr 1500 cost 1,100,000; Cr 4100 gain 50,000.
        self.assertEqual(entry.lines.get(account__code="1900").debit, 200000)
        self.assertEqual(entry.lines.get(account__code="1500").credit, 1100000)
        self.assertEqual(entry.lines.get(account__code="4100").credit, 950000 - nbv)

    # Verify post depreciation on draft asset is rejected behavior.
    def test_post_depreciation_on_draft_asset_is_rejected(self):
        entity, _, _ = self.build_books()
        asset = self._make_asset(entity)  # DRAFT — never acquired
        self.assertEqual(asset.asset_status, AssetStatus.DRAFT)
        with self.assertRaises(DepreciationError):
            post_depreciation(asset, up_to_date=datetime.date(2026, 12, 31))

    # Verify cannot rebuild schedule after posting behavior.
    def test_cannot_rebuild_schedule_after_posting(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        asset = self._make_asset(entity)
        acquire_asset(asset, bank_account=bank)
        post_depreciation(asset, up_to_date=datetime.date(2026, 2, 28))
        with self.assertRaises(DepreciationError):
            build_depreciation_schedule(asset)

    # Verify dispose blocked when due depreciation unposted behavior.
    def test_dispose_blocked_when_due_depreciation_unposted(self):
        # A charge due Feb 1 2026 is still unposted; disposing Mar 1 must refuse.
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)
        acquire_asset(asset, bank_account=bank)
        with self.assertRaises(DepreciationError) as ctx:
            dispose_asset(asset, disposal_date=datetime.date(2026, 3, 1),
                          proceeds=0, bank_account=bank)
        self.assertIn("unposted", str(ctx.exception).lower())
        asset.refresh_from_db()
        self.assertEqual(asset.asset_status, AssetStatus.ACTIVE)  # nothing disposed

    # Verify dispose succeeds after posting due depreciation behavior.
    def test_dispose_succeeds_after_posting_due_depreciation(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)
        acquire_asset(asset, bank_account=bank)
        # Post the two charges due up to the disposal date (Feb 1 + Mar 1), then dispose.
        post_depreciation(asset, up_to_date=datetime.date(2026, 3, 1))
        loss = Account.objects.get(entity=entity, code="5300")
        dispose_asset(asset, disposal_date=datetime.date(2026, 3, 1), proceeds=0,
                      bank_account=bank, gain_loss_account=loss)
        asset.refresh_from_db()
        self.assertEqual(asset.asset_status, AssetStatus.DISPOSED)

    # Verify dispose ignores future dated unposted charges behavior.
    def test_dispose_ignores_future_dated_unposted_charges(self):
        # Disposing on the acquisition date: every charge (Feb+) is future-dated and may
        # be orphaned (life cut short), so the disposal is allowed.
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        asset = self._make_asset(entity, cost=1100000, salvage=0, life=11)
        acquire_asset(asset, bank_account=bank)
        loss = Account.objects.get(entity=entity, code="5300")
        dispose_asset(asset, disposal_date=datetime.date(2026, 1, 1), proceeds=0,
                      bank_account=bank, gain_loss_account=loss)
        asset.refresh_from_db()
        self.assertEqual(asset.asset_status, AssetStatus.DISPOSED)


# Group tests for Period Close Tests.
class PeriodCloseTests(_Phase4FixtureMixin, TestCase):
    # Verify checklist passes on clean ledger behavior.
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

    # Verify close reopen and lock cycle behavior.
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

    # Verify reopen closed period returns to open behavior.
    def test_reopen_closed_period_returns_to_open(self):
        entity, _, periods = self.build_books()
        jan = periods[0]
        close_period(entity, jan)
        jan.refresh_from_db()
        self.assertEqual(jan.status, PeriodStatus.CLOSED)
        reopen_period(entity, jan)
        jan.refresh_from_db()
        self.assertEqual(jan.status, PeriodStatus.OPEN)

    # Verify lock closed period seals it behavior.
    def test_lock_closed_period_seals_it(self):
        entity, _, periods = self.build_books()
        jan = periods[0]
        close_period(entity, jan)
        lock_period(entity, jan)
        jan.refresh_from_db()
        self.assertEqual(jan.status, PeriodStatus.LOCKED)

    # Verify lock refuses non closed period behavior.
    def test_lock_refuses_non_closed_period(self):
        entity, _, periods = self.build_books()
        jan = periods[0]  # still OPEN
        with self.assertRaises(PeriodCloseError):
            lock_period(entity, jan)
        jan.refresh_from_db()
        self.assertEqual(jan.status, PeriodStatus.OPEN)

    # Verify reopen refuses locked period behavior.
    def test_reopen_refuses_locked_period(self):
        entity, _, periods = self.build_books()
        jan = periods[0]
        close_period(entity, jan)
        lock_period(entity, jan)
        with self.assertRaises(PeriodCloseError):
            reopen_period(entity, jan)

    # Verify soft close allows depreciation auto posting behavior.
    def test_soft_close_allows_depreciation_auto_posting(self):
        entity, _, periods = self.build_books()
        period, _ = close_period(entity, periods[0], soft=True)
        self.assertEqual(period.status, PeriodStatus.SOFT_CLOSED)

    # Verify blocking failure requires force behavior.
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

    # Verify extra checks are injected behavior.
    def test_extra_checks_are_injected(self):
        entity, _, periods = self.build_books()
        calls = []

        # Prepare or verify the failing check test path.
        def failing_check():
            calls.append(True)
            return ("ap_reconciled", False, "sub-ledger 100 vs control 0")

        with self.assertRaises(PeriodCloseError):
            close_period(entity, periods[0], extra_checks=[failing_check])
        self.assertTrue(calls)  # the injected check actually ran


# Group tests for Financial Statement Tests.
class FinancialStatementTests(_Phase4FixtureMixin, TestCase):
    """The three primary statements over one coherent set of transactions.

    A tiny but complete first month:
      * owner injects 1,000,000 capital (financing inflow)
      * buys 400,000 of equipment for cash (investing outflow)
      * earns 300,000 cash revenue (operating inflow)
      * pays 120,000 cash salaries (operating outflow)
    """

    # Support the seed activity workflow.
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

    # Verify income statement nets revenue less expense behavior.
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

    # Verify income statement aggregates all periods when unscoped behavior.
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

    # Verify balance sheet balances with unclosed net income behavior.
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

    # Verify cash flow reconciles and classifies behavior.
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

    # Verify balance sheet sections group by ifrs and balance behavior.
    def test_balance_sheet_sections_group_by_ifrs_and_balance(self):
        from .reports import balance_sheet_sections
        entity, _, periods = self.build_books()
        self._seed_activity(entity, periods[0])

        bs = balance_sheet_sections(entity)
        self.assertTrue(bs.is_balanced)
        self.assertEqual(bs.total_assets, 1180000)
        self.assertEqual(bs.total_liabilities, 0)
        self.assertEqual(bs.total_equity, 1180000)
        self.assertEqual(bs.current_year_earnings, 180000)

        by_key = {s.key: s for s in bs.sections}
        self.assertEqual(
            set(by_key),
            {"non_current_assets", "current_assets", "equity",
             "non_current_liabilities", "current_liabilities"})
        # Cash (1100) → current assets 780,000; PP&E (1500) → non-current 400,000.
        self.assertEqual(by_key["current_assets"].total, 780000)
        self.assertEqual(by_key["non_current_assets"].total, 400000)
        # The unclosed net income shows as its own equity line, not folded away.
        self.assertIn(
            "Current year earnings", [g.label for g in by_key["equity"].groups])

    # Verify balance sheet nets contra asset and balances behavior.
    def test_balance_sheet_nets_contra_asset_and_balances(self):
        # Accumulated depreciation is a contra-asset (credit balance). It must REDUCE
        # PP&E and keep the sheet balanced — not be added to assets.
        from .reports import balance_sheet_sections
        entity, _, periods = self.build_books()
        p = periods[0]
        post_journal(self.make_entry(entity, p, [("1100", 1000000, 0), ("3100", 0, 1000000)]))  # capital
        post_journal(self.make_entry(entity, p, [("1500", 400000, 0), ("1100", 0, 400000)]))     # buy equipment
        post_journal(self.make_entry(entity, p, [("5400", 100000, 0), ("1900", 0, 100000)]))      # depreciation

        bs = balance_sheet_sections(entity)
        self.assertTrue(bs.is_balanced)
        self.assertEqual(bs.total_assets, 900000)   # 600k cash + 300k net PP&E
        by_key = {s.key: s for s in bs.sections}
        ppe = next(g for g in by_key["non_current_assets"].groups if g.line == "PPE")
        self.assertEqual(ppe.amount, 300000)         # 400k cost − 100k accumulated dep
        self.assertEqual(by_key["non_current_assets"].total, 300000)

    # Verify cash flow ignores non cash journals behavior.
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

    # Verify cash flow breaks activities into line items behavior.
    def test_cash_flow_breaks_activities_into_line_items(self):
        entity, _, periods = self.build_books()
        self._seed_activity(entity, periods[0])

        cf = cash_flow_statement(entity)
        # Operating splits into the revenue (source) and salaries (use) counter-accounts.
        op = {ln.code: ln.amount for ln in cf.activity_lines["operating"]}
        self.assertEqual(op, {"4100": 300000, "5200": -120000})
        inv = {ln.code: ln.amount for ln in cf.activity_lines["investing"]}
        self.assertEqual(inv, {"1500": -400000})   # equipment purchase (cash out)
        fin = {ln.code: ln.amount for ln in cf.activity_lines["financing"]}
        self.assertEqual(fin, {"3100": 1000000})   # owner capital (cash in)
        # Line items foot to their activity subtotal.
        self.assertEqual(sum(op.values()), cf.by_activity["operating"])


# Group tests for Income Statement Compare Tests.
class IncomeStatementCompareTests(_Phase4FixtureMixin, TestCase):
    """The P&L with Budget + Prior-year comparison columns (income_statement_compare)."""

    # Support the activity workflow.
    def _activity(self, entity, period, *, revenue, expense):
        post_journal(self.make_entry(
            entity, period, [("1100", revenue, 0), ("4100", 0, revenue)]))  # cash revenue
        post_journal(self.make_entry(
            entity, period, [("5200", expense, 0), ("1100", 0, expense)]))  # cash expense

    # Verify no comparison without budget or prior year behavior.
    def test_no_comparison_without_budget_or_prior_year(self):
        entity, _, periods = self.build_books()
        self._activity(entity, periods[0], revenue=300000, expense=120000)

        rep = income_statement_compare(entity, period=periods[0])
        self.assertFalse(rep.has_budget)
        self.assertFalse(rep.has_prior_year)
        inc = {r.code: r for r in rep.income_rows}
        exp = {r.code: r for r in rep.expense_rows}
        self.assertEqual(inc["4100"].amount, 300000)
        self.assertIsNone(inc["4100"].budget)
        self.assertIsNone(inc["4100"].prior_year)
        self.assertEqual(exp["5200"].amount, 120000)
        self.assertEqual(rep.net_totals.amount, 180000)
        self.assertIsNone(rep.net_totals.variance)

    # Verify budget and prior year columns populate with favourable variance behavior.
    def test_budget_and_prior_year_columns_populate_with_favourable_variance(self):
        from .constants import BudgetStatus
        from .models import Account, Budget, BudgetLine, FiscalPeriod, FiscalYear

        entity, year, periods = self.build_books()
        # Current-year actuals.
        self._activity(entity, periods[0], revenue=300000, expense=120000)

        # A prior fiscal year (2025) with its own activity.
        prior_year = FiscalYear.objects.create(
            entity=entity, year=2025,
            start_date=datetime.date(2025, 1, 1), end_date=datetime.date(2025, 12, 31))
        prior_period = FiscalPeriod.objects.create(
            entity=entity, fiscal_year=prior_year, period_no=1, name="2025-01",
            start_date=datetime.date(2025, 1, 1), end_date=datetime.date(2025, 1, 31))
        self._activity(entity, prior_period, revenue=200000, expense=80000)

        # An approved budget for the current year.
        budget = Budget.objects.create(
            entity=entity, fiscal_year=year, name="Plan", status=BudgetStatus.APPROVED)
        BudgetLine.objects.create(
            budget=budget, account=Account.objects.get(entity=entity, code="4100"),
            period_no=1, amount=250000)
        BudgetLine.objects.create(
            budget=budget, account=Account.objects.get(entity=entity, code="5200"),
            period_no=1, amount=150000)

        rep = income_statement_compare(entity)  # YTD → current FY = 2026 (latest)
        self.assertTrue(rep.has_budget)
        self.assertTrue(rep.has_prior_year)
        self.assertEqual(rep.fiscal_year, 2026)
        self.assertEqual(rep.prior_fiscal_year, 2025)

        inc = {r.code: r for r in rep.income_rows}["4100"]
        self.assertEqual(inc.amount, 300000)
        self.assertEqual(inc.budget, 250000)
        self.assertEqual(inc.variance, 50000)      # revenue: actual − budget (favourable)
        self.assertEqual(inc.prior_year, 200000)

        exp = {r.code: r for r in rep.expense_rows}["5200"]
        self.assertEqual(exp.amount, 120000)
        self.assertEqual(exp.budget, 150000)
        self.assertEqual(exp.variance, 30000)      # expense: budget − actual (favourable)
        self.assertEqual(exp.prior_year, 80000)

        self.assertEqual(rep.net_totals.amount, 180000)
        self.assertEqual(rep.net_totals.budget, 100000)
        self.assertEqual(rep.net_totals.variance, 80000)
        self.assertEqual(rep.net_totals.prior_year, 120000)


# Group tests for Changes In Equity Tests.
class ChangesInEquityTests(_Phase4FixtureMixin, TestCase):
    """The statement of changes in equity over a two-month, two-component scenario."""

    # Support the col workflow.
    def _col(self, soce, key):
        return next(c for c in soce.columns if c.key == key)

    # Verify single period splits capital from profit behavior.
    def test_single_period_splits_capital_from_profit(self):
        entity, _, periods = self.build_books()
        # Jan: 1,000,000 capital + 180,000 net income (300k rev − 120k salaries).
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 1000000, 0), ("3100", 0, 1000000)],
            date=datetime.date(2026, 1, 5),
        ))
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 300000, 0), ("4100", 0, 300000)],
            date=datetime.date(2026, 1, 10),
        ))
        post_journal(self.make_entry(
            entity, periods[0], [("5200", 120000, 0), ("1100", 0, 120000)],
            date=datetime.date(2026, 1, 20),
        ))

        soce = statement_of_changes_in_equity(entity, period=periods[0])
        cap = self._col(soce, "3100")
        self.assertEqual(cap.opening, 0)
        self.assertEqual(cap.contributions, 1000000)
        self.assertEqual(cap.closing, 1000000)
        re = self._col(soce, "retained_earnings")
        self.assertEqual(re.opening, 0)
        self.assertEqual(re.profit, 180000)
        self.assertEqual(re.closing, 180000)
        self.assertEqual(soce.total_opening, 0)
        self.assertEqual(soce.total_profit, 180000)
        self.assertEqual(soce.total_contributions, 1000000)
        self.assertEqual(soce.total_closing, 1180000)
        self.assertTrue(soce.is_reconciled)

    # Verify period carries opening and books distribution behavior.
    def test_period_carries_opening_and_books_distribution(self):
        entity, _, periods = self.build_books()
        # January.
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 1000000, 0), ("3100", 0, 1000000)],
            date=datetime.date(2026, 1, 5),
        ))
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 300000, 0), ("4100", 0, 300000)],
            date=datetime.date(2026, 1, 10),
        ))
        post_journal(self.make_entry(
            entity, periods[0], [("5200", 120000, 0), ("1100", 0, 120000)],
            date=datetime.date(2026, 1, 20),
        ))
        # February: 500k more capital, a 50k dividend (Dr retained earnings/Cr cash),
        # and 120k net income (200k rev − 80k expense).
        post_journal(self.make_entry(
            entity, periods[1], [("1100", 500000, 0), ("3100", 0, 500000)],
            date=datetime.date(2026, 2, 4),
        ))
        post_journal(self.make_entry(
            entity, periods[1], [("3200", 50000, 0), ("1100", 0, 50000)],
            date=datetime.date(2026, 2, 6),
        ))
        post_journal(self.make_entry(
            entity, periods[1], [("1100", 200000, 0), ("4100", 0, 200000)],
            date=datetime.date(2026, 2, 12),
        ))
        post_journal(self.make_entry(
            entity, periods[1], [("5300", 80000, 0), ("1100", 0, 80000)],
            date=datetime.date(2026, 2, 18),
        ))

        soce = statement_of_changes_in_equity(entity, period=periods[1])
        cap = self._col(soce, "3100")
        self.assertEqual(cap.opening, 1000000)        # carried from January
        self.assertEqual(cap.contributions, 500000)
        self.assertEqual(cap.closing, 1500000)
        dist = self._col(soce, "3200")
        self.assertEqual(dist.opening, 0)
        self.assertEqual(dist.contributions, -50000)  # dividend is a distribution
        self.assertEqual(dist.closing, -50000)
        re = self._col(soce, "retained_earnings")
        self.assertEqual(re.opening, 180000)          # January's unclosed profit
        self.assertEqual(re.profit, 120000)
        self.assertEqual(re.closing, 300000)
        self.assertEqual(soce.total_opening, 1180000)
        self.assertEqual(soce.total_contributions, 450000)
        self.assertEqual(soce.total_profit, 120000)
        self.assertEqual(soce.total_closing, 1750000)
        self.assertTrue(soce.is_reconciled)

    # Verify unscoped reconciles to balance sheet equity behavior.
    def test_unscoped_reconciles_to_balance_sheet_equity(self):
        entity, _, periods = self.build_books()
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 1000000, 0), ("3100", 0, 1000000)],
            date=datetime.date(2026, 1, 5),
        ))
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 300000, 0), ("4100", 0, 300000)],
            date=datetime.date(2026, 1, 10),
        ))
        soce = statement_of_changes_in_equity(entity)
        # Life-to-date: everything is a movement from a zero opening.
        self.assertEqual(soce.total_opening, 0)
        self.assertEqual(soce.total_closing, balance_sheet(entity).total_equity)
        self.assertTrue(soce.is_reconciled)


# Group tests for Statutory Pack Tests.
class StatutoryPackTests(_Phase4FixtureMixin, TestCase):
    """The IFRS-for-SMEs statutory pack regroups the chart onto presentation lines."""

    # Support the seed activity workflow.
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

    # Support the group workflow.
    def _group(self, section, line):
        return next((g for g in section.groups if g.line == line), None)

    # Support the section workflow.
    def _section(self, pack, key):
        return next(s for s in pack.sofp_sections if s.key == key)

    # Verify sofp regroups chart onto ifrs lines behavior.
    def test_sofp_regroups_chart_onto_ifrs_lines(self):
        entity, _, periods = self.build_books()
        self._seed_activity(entity, periods[0])

        pack = statutory_pack(entity)
        nca = self._section(pack, "non_current_assets")
        self.assertEqual(self._group(nca, "PPE").amount, 400000)
        ca = self._section(pack, "current_assets")
        self.assertEqual(self._group(ca, "CASH").amount, 780000)  # 1000k-400k+300k-120k
        eq = self._section(pack, "equity")
        self.assertEqual(self._group(eq, "SHARE_CAPITAL").amount, 1000000)
        # Unclosed P&L is folded into the retained-earnings equity line.
        self.assertEqual(self._group(eq, "RETAINED_EARNINGS").amount, 180000)

        self.assertEqual(pack.total_assets, 1180000)
        self.assertEqual(pack.total_equity, 1180000)
        self.assertEqual(pack.total_liabilities, 0)
        self.assertTrue(pack.is_balanced)
        self.assertEqual(pack.difference, 0)

    # Verify income statement maps to ifrs lines behavior.
    def test_income_statement_maps_to_ifrs_lines(self):
        entity, _, periods = self.build_books()
        self._seed_activity(entity, periods[0])

        pack = statutory_pack(entity)
        lines = {g.line: g.amount for g in pack.income_lines}
        self.assertEqual(lines["REVENUE"], 300000)
        self.assertEqual(lines["ADMIN_EXPENSES"], 120000)  # salaries map here
        self.assertEqual(pack.total_income, 300000)
        self.assertEqual(pack.total_expense, 120000)
        self.assertEqual(pack.net_income, 180000)

    # Verify companion statements ride along and reconcile behavior.
    def test_companion_statements_ride_along_and_reconcile(self):
        entity, _, periods = self.build_books()
        self.make_bank(entity)
        self._seed_activity(entity, periods[0])

        pack = statutory_pack(entity)
        self.assertTrue(pack.cash_flow.is_reconciled)
        self.assertEqual(pack.cash_flow.closing_cash, 780000)
        self.assertTrue(pack.changes_in_equity.is_reconciled)
        self.assertEqual(pack.changes_in_equity.total_closing, 1180000)
        self.assertTrue(pack.trial_balance.is_balanced)

    # Verify unmapped account falls back to type default behavior.
    def test_unmapped_account_falls_back_to_type_default(self):
        entity, _, periods = self.build_books()
        # A custom asset account with no explicit IFRS line.
        Account.objects.create(
            entity=entity, code="1250", name="Prepayments",
            account_type=AccountType.ASSET, is_postable=True,
        )
        post_journal(self.make_entry(
            entity, periods[0], [("1250", 90000, 0), ("4100", 0, 90000)],
        ))
        pack = statutory_pack(entity)
        ca = self._section(pack, "current_assets")
        self.assertEqual(self._group(ca, "OTHER_CURRENT_ASSETS").amount, 90000)


# Group tests for Finance A P I Tests.
class FinanceAPITests(_Phase4FixtureMixin, TestCase):
    """The /v1/finance/ REST surface: entity scoping, reports, documents, actions.

    Authenticated as a Vision super admin, which bypasses the per-endpoint RBAC gate
    (so these tests exercise routing/serialisation, not the RBAC matrix itself).
    """

    # Prepare or verify the setUp test path.
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

    # Support the create claim workflow.
    def _create_claim(self, entity):
        return self.client.post(
            f"/v1/finance/expense-claims/?entity={entity.code}",
            {"claimant_name": "Jane Staff", "claim_date": "2026-01-10", "title": "Trip",
             "lines": [{"description": "Diesel", "expense_account": "5300",
                        "quantity": 1, "unit_price": 100000}]}, format="json")

    # Verify expense claim reject only from draft behavior.
    def test_expense_claim_reject_only_from_draft(self):
        entity, _, _ = self.build_books()
        created = self._create_claim(entity)
        self.assertEqual(created.status_code, 201, created.content)
        cid = created.json()["data"]["id"]
        rej = self.client.post(f"/v1/finance/expense-claims/{cid}/reject/?entity={entity.code}", {}, format="json")
        self.assertEqual(rej.status_code, 200, rej.content)
        self.assertEqual(rej.json()["data"]["status"], "CANCELLED")
        # A cancelled claim can't be rejected again.
        again = self.client.post(f"/v1/finance/expense-claims/{cid}/reject/?entity={entity.code}", {}, format="json")
        self.assertEqual(again.status_code, 400, again.content)

    # Verify expense line receipt upload and remove behavior.
    def test_expense_line_receipt_upload_and_remove(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        entity, _, _ = self.build_books()
        created = self._create_claim(entity)
        cid = created.json()["data"]["id"]
        line_id = created.json()["data"]["lines"][0]["id"]
        self.assertIsNone(created.json()["data"]["lines"][0]["receipt_url"])

        up = self.client.post(
            f"/v1/finance/expense-claims/{cid}/lines/{line_id}/receipt/?entity={entity.code}",
            {"file": SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 fake", content_type="application/pdf")},
            format="multipart")
        self.assertEqual(up.status_code, 201, up.content)
        line = up.json()["data"]["lines"][0]
        self.assertTrue(line["receipt_name"].startswith("receipt"))
        self.assertTrue(line["receipt_url"])

        rm = self.client.delete(f"/v1/finance/expense-claims/{cid}/lines/{line_id}/receipt/?entity={entity.code}")
        self.assertEqual(rm.status_code, 200, rm.content)
        self.assertIsNone(rm.json()["data"]["lines"][0]["receipt_url"])

    # Verify petty cash register and spent week behavior.
    def test_petty_cash_register_and_spent_week(self):
        entity, _, _ = self.build_books()
        bank = self.make_bank(entity)
        fund = PettyCashFund.objects.create(
            entity=entity, name="Front Desk", custodian_name="Lola",
            gl_account=Account.objects.get(entity=entity, code="1110"), float_amount=5000000)
        establish_fund(fund, bank_account=bank, amount=5000000, date=datetime.date.today())
        v = PettyCashVoucher.objects.create(
            entity=entity, fund=fund, voucher_date=datetime.date.today(), payee="Shop")
        PettyCashVoucherLine.objects.create(
            voucher=v, expense_account=Account.objects.get(entity=entity, code="5300"),
            quantity=1, unit_price=120000, line_no=1)
        post_voucher(v)

        resp = self.client.get(f"/v1/finance/petty-cash-funds/{fund.id}/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["spent_this_week"], 120000)
        reg = data["register"]
        # Newest first: the spend (out), then the establish top-up (in).
        self.assertEqual(reg[0]["out"], 120000)
        self.assertEqual(reg[0]["category"], Account.objects.get(entity=entity, code="5300").name)
        self.assertEqual(reg[0]["balance"], 4880000)  # 5,000,000 − 120,000
        self.assertEqual(reg[-1]["in"], 5000000)
        self.assertEqual(reg[-1]["category"], "Top-up")

    # Verify customer opening balance backdated to opening date behavior.
    def test_customer_opening_balance_backdated_to_opening_date(self):
        # A historical opening_date inside an open period backdates the opening invoice
        # and its journal (F4).
        from vs_finance.constants import InvoiceSource
        from vs_finance.models import Customer, Invoice

        entity, _, _ = self.build_books()
        resp = self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "OPENC", "name": "Backdated Co", "opening_balance": 5000000,
             "opening_date": "2026-03-15"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        cust = Customer.objects.get(entity=entity, code="OPENC")
        inv = Invoice.objects.get(entity=entity, customer=cust, source=InvoiceSource.OPENING)
        self.assertEqual(inv.invoice_date, datetime.date(2026, 3, 15))
        self.assertEqual(inv.journal.date, datetime.date(2026, 3, 15))

    # Verify customer opening balance credits equity not revenue behavior.
    def test_customer_opening_balance_credits_equity_not_revenue(self):
        # Regression: an opening balance is prior-period value, so it must credit
        # equity (Retained Earnings 3200), never current-period revenue (4100) —
        # otherwise every migrated-in customer overstates the income statement.
        from vs_finance.constants import InvoiceSource
        from vs_finance.models import Customer, Invoice

        entity, _, _ = self.build_books()
        resp = self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "OPENEQ", "name": "Opening Equity Co", "opening_balance": 5000000},
            format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        cust = Customer.objects.get(entity=entity, code="OPENEQ")
        inv = Invoice.objects.get(entity=entity, customer=cust, source=InvoiceSource.OPENING)
        credit_codes = {ln.account.code for ln in inv.journal.lines.all() if ln.credit > 0}
        self.assertIn("3200", credit_codes)        # Retained Earnings (equity)
        self.assertNotIn("4100", credit_codes)     # not Operating Revenue (income)

    # Verify employee salary roster generates a run behavior.
    def test_employee_salary_roster_generates_a_run(self):
        entity, _, _ = self.build_books()
        for nm, g, p, pe in [("Ada Obi", 50000000, 7500000, 4000000),
                             ("Bola Lawal", 30000000, 4500000, 2400000)]:
            r = self.client.post(
                f"/v1/finance/employee-salaries/?entity={entity.code}",
                {"name": nm, "gross_amount": g, "paye_amount": p, "pension_amount": pe},
                format="json")
            self.assertEqual(r.status_code, 201, r.content)
        # Roster lists both, net is gross − paye − pension.
        roster = self.client.get(f"/v1/finance/employee-salaries/?entity={entity.code}").json()["data"]
        self.assertEqual([s["name"] for s in roster], ["Ada Obi", "Bola Lawal"])
        self.assertEqual(roster[0]["net_amount"], 50000000 - 7500000 - 4000000)

        gen = self.client.post(
            f"/v1/finance/payroll-runs/generate/?entity={entity.code}",
            {"pay_date": "2026-01-25", "period_label": "Jan 2026"}, format="json")
        self.assertEqual(gen.status_code, 201, gen.content)
        data = gen.json()["data"]
        self.assertEqual(len(data["lines"]), 2)
        self.assertEqual(data["gross_total"], 80000000)
        self.assertEqual(data["net_total"], 80000000 - 12000000 - 6400000)
        self.assertEqual(data["run_status"], "DRAFT")

    # Verify salary structure derives paye pension and net from gross behavior.
    def test_salary_structure_derives_paye_pension_and_net_from_gross(self):
        entity, _, _ = self.build_books()
        # A structure: Basic 40% of gross, Housing 30%, Transport 30% (earnings);
        # PAYE 7% of gross, Pension 8% of basic (deductions).
        struct = self.client.post(
            f"/v1/finance/salary-structures/?entity={entity.code}",
            {"name": "Senior staff", "components": [
                {"name": "Basic", "kind": "EARNING", "calc_method": "PERCENT_OF_GROSS",
                 "rate_bps": 4000, "is_basic": True},
                {"name": "Housing", "kind": "EARNING", "calc_method": "PERCENT_OF_GROSS",
                 "rate_bps": 3000},
                {"name": "Transport", "kind": "EARNING", "calc_method": "PERCENT_OF_GROSS",
                 "rate_bps": 3000},
                {"name": "PAYE", "kind": "DEDUCTION", "calc_method": "PERCENT_OF_GROSS",
                 "rate_bps": 700, "statutory_type": "PAYE"},
                {"name": "Pension", "kind": "DEDUCTION", "calc_method": "PERCENT_OF_BASIC",
                 "rate_bps": 800, "statutory_type": "PENSION"},
            ]}, format="json")
        self.assertEqual(struct.status_code, 201, struct.content)
        sid = struct.json()["data"]["id"]

        # A deduction tagged NONE is rejected (keeps the journal balanced).
        bad = self.client.post(
            f"/v1/finance/salary-structures/?entity={entity.code}",
            {"name": "Bad", "components": [
                {"name": "Loan", "kind": "DEDUCTION", "calc_method": "FIXED", "amount": 100},
            ]}, format="json")
        self.assertEqual(bad.status_code, 400, bad.content)

        # Assign it to an employee on a ₦500,000 gross; PAYE/pension/net are derived.
        emp = self.client.post(
            f"/v1/finance/employee-salaries/?entity={entity.code}",
            {"name": "Ada Obi", "gross_amount": 50000000, "structure": sid}, format="json")
        self.assertEqual(emp.status_code, 201, emp.content)
        row = self.client.get(
            f"/v1/finance/employee-salaries/?entity={entity.code}").json()["data"][0]
        self.assertEqual(row["paye_amount"], 3500000)            # 7% of 50,000,000
        self.assertEqual(row["pension_amount"], 1600000)         # 8% of basic (20,000,000)
        self.assertEqual(row["net_amount"], 50000000 - 3500000 - 1600000)
        self.assertEqual(len(row["components"]), 5)
        self.assertEqual(row["structure_name"], "Senior staff")

        # A generated run copies the derived figures + the payslip breakdown snapshot.
        gen = self.client.post(
            f"/v1/finance/payroll-runs/generate/?entity={entity.code}",
            {"pay_date": "2026-01-25", "period_label": "Jan 2026"}, format="json")
        self.assertEqual(gen.status_code, 201, gen.content)
        line = gen.json()["data"]["lines"][0]
        self.assertEqual(line["paye_amount"], 3500000)
        self.assertEqual(line["pension_amount"], 1600000)
        self.assertEqual(len(line["components"]), 5)

        # Can't delete a structure that's assigned to someone.
        rm = self.client.delete(f"/v1/finance/salary-structures/{sid}/?entity={entity.code}")
        self.assertEqual(rm.status_code, 400, rm.content)

    # Verify budget list enriched and heatmap endpoint behavior.
    def test_budget_list_enriched_and_heatmap_endpoint(self):
        entity, year, periods = self.build_books()
        budget = Budget.objects.create(entity=entity, fiscal_year=year, name="FY26 Plan")
        salaries = Account.objects.get(entity=entity, code="5200")
        add_budget_line(budget, account=salaries, period_no=1, amount=60000)
        post_journal(self.make_entry(
            entity, periods[0], [("5200", 30000, 0), ("1100", 0, 30000)],
            date=datetime.date(2026, 1, 15)))

        # List carries headline budget/actual/consumed so the table needs no extra call.
        lst = self.client.get(f"/v1/finance/budgets/?entity={entity.code}").json()["data"]
        b = next(x for x in lst if x["id"] == budget.id)
        self.assertEqual(b["budgeted_total"], 60000)
        self.assertEqual(b["actual_ytd"], 30000)
        self.assertEqual(b["consumed_pct"], 50.0)

        # Heatmap: 12 periods, the Jan cell carries budget 60,000 / actual 30,000.
        hm = self.client.get(f"/v1/finance/budgets/{budget.id}/heatmap/?entity={entity.code}").json()["data"]
        self.assertEqual(len(hm["periods"]), 12)
        r = next(x for x in hm["rows"] if x["code"] == "5200")
        c1 = next(c for c in r["cells"] if c["period_no"] == 1)
        self.assertEqual((c1["budget"], c1["actual"]), (60000, 30000))

    # Verify budget create with lines autocode and draft edit behavior.
    def test_budget_create_with_lines_autocode_and_draft_edit(self):
        entity, year, _ = self.build_books()
        # Create a budget WITH lines in one call; it gets an auto code.
        resp = self.client.post(
            f"/v1/finance/budgets/?entity={entity.code}",
            {"name": "FY26 Operating", "fiscal_year": year.year, "lines": [
                {"account": "5200", "period_no": 1, "amount": 60000},
                {"account": "5200", "period_no": 2, "amount": 60000},
                {"account": "5100", "period_no": 1, "amount": 20000},
            ]}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        b = resp.json()["data"]
        self.assertTrue(b["code"].startswith(f"CFX-{entity.code}-BDG-{year.year}-"))
        self.assertEqual(len(b["lines"]), 3)
        bid = b["id"]

        # Budgets reject non-P&L accounts (variance is against income/expense only).
        bad = self.client.put(
            f"/v1/finance/budgets/{bid}/lines/?entity={entity.code}",
            {"lines": [{"account": "1100", "period_no": 1, "amount": 5000}]}, format="json")
        self.assertEqual(bad.status_code, 422, bad.content)

        # PUT replaces all lines wholesale.
        rep = self.client.put(
            f"/v1/finance/budgets/{bid}/lines/?entity={entity.code}",
            {"lines": [{"account": "5200", "period_no": 1, "amount": 99000}]}, format="json")
        self.assertEqual(rep.status_code, 200, rep.content)
        self.assertEqual(len(rep.json()["data"]["lines"]), 1)
        line_id = rep.json()["data"]["lines"][0]["id"]

        # PATCH renames a draft.
        pat = self.client.patch(
            f"/v1/finance/budgets/{bid}/?entity={entity.code}", {"name": "FY26 Opex"}, format="json")
        self.assertEqual(pat.status_code, 200, pat.content)
        self.assertEqual(pat.json()["data"]["name"], "FY26 Opex")

        # DELETE one line.
        d = self.client.delete(f"/v1/finance/budgets/{bid}/lines/{line_id}/?entity={entity.code}")
        self.assertEqual(d.status_code, 200, d.content)
        self.assertEqual(len(d.json()["data"]["lines"]), 0)

        # Once approved, edits are refused (the lock).
        self.client.post(f"/v1/finance/budgets/{bid}/approve/?entity={entity.code}")
        locked = self.client.patch(
            f"/v1/finance/budgets/{bid}/?entity={entity.code}", {"name": "nope"}, format="json")
        self.assertEqual(locked.status_code, 422, locked.content)

        # Fiscal-years endpoint lists the open year for the dropdown.
        fy = self.client.get(f"/v1/finance/fiscal-years/?entity={entity.code}").json()["data"]
        fy = fy if isinstance(fy, list) else fy.get("results", [])
        self.assertTrue(any(y["year"] == year.year for y in fy))

    # Verify bank account detail reports metrics and transactions behavior.
    def test_bank_account_detail_reports_metrics_and_transactions(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        # A +50,000 cash inflow on the cash account (book balance moves).
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],
            date=datetime.date(2026, 1, 15)))
        self.client.post(
            f"/v1/finance/bank-accounts/{bank.id}/statement-lines/?entity={entity.code}",
            {"lines": [{"txn_date": "2026-01-16", "amount": 50000}],
             "period_label": "Jan 2026", "opening_balance": 0}, format="json")

        resp = self.client.get(
            f"/v1/finance/bank-accounts/{bank.id}/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["book_balance"], 50000)
        self.assertEqual(data["metrics"]["book_balance"], 50000)
        self.assertEqual(data["metrics"]["statement_balance"], 50000)
        self.assertEqual(data["metrics"]["unreconciled_diff"], 0)
        self.assertEqual(data["metrics"]["unreconciled_count"], 1)
        # Transactions carry a running balance; the latest equals the book balance.
        self.assertEqual(data["transactions"][0]["running_balance"], 50000)
        self.assertEqual(len(data["statements"]), 1)
        self.assertEqual(data["statements"][0]["closing_balance"], 50000)

    # Verify bank book lines and complete reconciliation behavior.
    def test_bank_book_lines_and_complete_reconciliation(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        # Two posted cash movements (the "book" side).
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],
            date=datetime.date(2026, 1, 15)))
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 30000, 0), ("4100", 0, 30000)],
            date=datetime.date(2026, 1, 16)))

        self.client.post(
            f"/v1/finance/bank-accounts/{bank.id}/statement-lines/?entity={entity.code}",
            {"lines": [{"txn_date": "2026-01-15", "amount": 50000}]}, format="json")
        self.client.post(
            f"/v1/finance/bank-accounts/{bank.id}/auto-reconcile/?entity={entity.code}",
            {"tolerance_days": 5}, format="json")

        # Book-lines now lists only the still-unmatched ₦30,000 movement.
        book = self.client.get(
            f"/v1/finance/bank-accounts/{bank.id}/book-lines/?entity={entity.code}")
        self.assertEqual(book.status_code, 200, book.content)
        rows = book.json()["data"]
        self.assertEqual([r["amount"] for r in rows], [30000])

        # Complete records a reconciliation snapshot.
        done = self.client.post(
            f"/v1/finance/bank-accounts/{bank.id}/reconcile/complete/?entity={entity.code}",
            {}, format="json")
        self.assertEqual(done.status_code, 201, done.content)
        self.assertEqual(done.json()["data"]["matched_count"], 1)
        self.assertIn(done.json()["data"]["status"], ("BALANCED", "OUT_OF_BALANCE"))

    # Verify unmatch drops pairing and reverses adjustment behavior.
    def test_unmatch_drops_pairing_and_reverses_adjustment(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        # 1) A plain match: post a +50,000 cash line, import + auto-match it.
        post_journal(self.make_entry(
            entity, periods[0], [("1100", 50000, 0), ("4100", 0, 50000)],
            date=datetime.date(2026, 1, 15)))
        imp = self.client.post(
            f"/v1/finance/bank-accounts/{bank.id}/statement-lines/?entity={entity.code}",
            {"lines": [{"txn_date": "2026-01-15", "amount": 50000}]}, format="json")
        line_id = imp.json()["data"]["imported"][0]["id"]
        self.client.post(
            f"/v1/finance/bank-accounts/{bank.id}/auto-reconcile/?entity={entity.code}",
            {"tolerance_days": 5}, format="json")
        # Unmatch → back to UNMATCHED, no ledger effect.
        un = self.client.post(
            f"/v1/finance/statement-lines/{line_id}/unmatch/?entity={entity.code}", {}, format="json")
        self.assertEqual(un.status_code, 200, un.content)
        self.assertEqual(un.json()["data"]["status"], "UNMATCHED")

        # 2) An adjustment: a -1,500 charge → adjust (books a journal), then unmatch reverses it.
        from vs_finance.constants import DocumentStatus
        adj_imp = self.client.post(
            f"/v1/finance/bank-accounts/{bank.id}/statement-lines/?entity={entity.code}",
            {"lines": [{"txn_date": "2026-01-20", "amount": -1500, "description": "Fee"}]}, format="json")
        adj_line = adj_imp.json()["data"]["imported"][0]["id"]
        adj = self.client.post(
            f"/v1/finance/statement-lines/{adj_line}/adjust/?entity={entity.code}", {}, format="json")
        self.assertEqual(adj.json()["data"]["match_source"], "ADJUSTMENT")
        je_id = adj.json()["data"]["adjusting_journal_id"]
        self.client.post(
            f"/v1/finance/statement-lines/{adj_line}/unmatch/?entity={entity.code}", {}, format="json")
        from vs_finance.models import JournalEntry
        self.assertTrue(JournalEntry.objects.filter(reverses_id=je_id, status=DocumentStatus.POSTED).exists())

    # Verify bank account patch updates settings and primary behavior.
    def test_bank_account_patch_updates_settings_and_primary(self):
        entity, _, _ = self.build_books()
        a = self.make_bank(entity)
        b = BankAccount.objects.create(
            entity=entity, name="Access Collections",
            gl_account=Account.objects.get(entity=entity, code="1500"), is_primary=True)
        # Make `a` primary → `b` is demoted (at most one primary).
        resp = self.client.patch(
            f"/v1/finance/bank-accounts/{a.id}/?entity={entity.code}",
            {"is_primary": True, "bank_name": "GTBank"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["data"]["is_primary"])
        self.assertEqual(resp.json()["data"]["bank_name"], "GTBank")
        b.refresh_from_db()
        self.assertFalse(b.is_primary)

    # Support the seed workflow.
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

    # Verify entity param is required and validated behavior.
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

    # Verify entities and accounts endpoints behavior.
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

    # Verify chart with balance and create account behavior.
    def test_chart_with_balance_and_create_account(self):
        entity, _ = self._seed()
        # ?with_balance returns the full tree with balance + tag + subtype fields.
        resp = self.client.get(f"/v1/finance/accounts/?entity={entity.code}&with_balance=true")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]
        cash = next(r for r in rows if r["code"] == "1100")
        self.assertEqual(cash["tag"], "CASH")
        self.assertIsNotNone(cash["balance"])
        self.assertIn("subtype", cash)

        # Create a new account with a subtype; normal balance is derived for INCOME.
        resp = self.client.post(
            f"/v1/finance/accounts/?entity={entity.code}",
            {"code": "4150", "name": "Boarding Fees", "account_type": "INCOME",
             "subtype": "Operating revenue"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["data"]
        self.assertEqual(data["code"], "4150")
        self.assertEqual(data["subtype"], "Operating revenue")
        self.assertEqual(data["normal_balance"], "CREDIT")

        # Duplicate code is rejected.
        dup = self.client.post(
            f"/v1/finance/accounts/?entity={entity.code}",
            {"code": "4150", "name": "dup", "account_type": "INCOME"}, format="json",
        )
        self.assertEqual(dup.status_code, 400)

    # Verify account detail ledger and update behavior.
    def test_account_detail_ledger_and_update(self):
        entity, _ = self._seed()
        from vs_finance.models import Account
        cash = Account.objects.get(entity=entity, code="1100")

        # Detail: summary + per-account posted activity (the _seed posts hit 1100).
        resp = self.client.get(f"/v1/finance/accounts/{cash.pk}/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        d = resp.json()["data"]
        self.assertEqual(d["account"]["code"], "1100")
        self.assertTrue(d["summary"]["line_count"] > 0)
        self.assertTrue(d["activity"])
        self.assertIn("running_balance", d["activity"][0])

        # Update is gated on finance.account.update; safe fields only.
        patch = self.client.patch(
            f"/v1/finance/accounts/{cash.pk}/?entity={entity.code}",
            {"subtype": "Cash and cash equivalents", "name": "Cash & Bank (main)"}, format="json",
        )
        self.assertEqual(patch.status_code, 200)
        self.assertEqual(patch.json()["data"]["subtype"], "Cash and cash equivalents")
        cash.refresh_from_db()
        self.assertEqual(cash.name, "Cash & Bank (main)")

    # Verify direct entry endpoint posts capital journal behavior.
    def test_direct_entry_endpoint_posts_capital_journal(self):
        # The honest way capital/equity enters: a posted journal, not magic.
        entity, _, _ = self.build_books()
        resp = self.client.post(
            f"/v1/finance/direct-entries/?entity={entity.code}",
            {"narration": "Capital injection",
             "lines": [{"account": "1100", "debit": 5000000000},   # Dr Cash ₦50,000,000
                       {"account": "3100", "credit": 5000000000}]},  # Cr Share Capital
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["source"], "OPENING")
        self.assertEqual(data["status"], "POSTED")
        self.assertEqual(data["total_debit"], 5000000000)
        self.assertEqual(data["total_credit"], 5000000000)

        # It is a real journal: it shows in the read-only journals list…
        journals = self.client.get(
            f"/v1/finance/journals/?entity={entity.code}").json()["data"]
        self.assertIn(data["document_number"], {j["document_number"] for j in journals})
        # …and it moved the trial balance (which still balances).
        tb = self.client.get(
            f"/v1/finance/reports/trial-balance/?entity={entity.code}").json()["data"]
        self.assertTrue(tb["is_balanced"])

    # Verify direct entry rejects unbalanced behavior.
    def test_direct_entry_rejects_unbalanced(self):
        entity, _, _ = self.build_books()
        resp = self.client.post(
            f"/v1/finance/direct-entries/?entity={entity.code}",
            {"lines": [{"account": "1100", "debit": 5000000000},
                       {"account": "3100", "credit": 4000000000}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    # Verify direct entry carries cost centre to gl behavior.
    def test_direct_entry_carries_cost_centre_to_gl(self):
        from .models import CostCenter

        entity, _, _ = self.build_books()
        CostCenter.objects.create(entity=entity, code="PRI", name="Primary")
        resp = self.client.post(
            f"/v1/finance/direct-entries/?entity={entity.code}",
            {"narration": "Dept adjustment",
             "lines": [{"account": "5300", "debit": 100000, "cost_center": "PRI"},
                       {"account": "1100", "credit": 100000}]},  # cash leg unallocated
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        by_acc = {ln["account_code"]: ln["cost_center"] for ln in resp.json()["data"]["lines"]}
        self.assertEqual(by_acc["5300"], "PRI")
        self.assertIsNone(by_acc["1100"])

    # Verify direct entry rejects unknown cost centre behavior.
    def test_direct_entry_rejects_unknown_cost_centre(self):
        entity, _, _ = self.build_books()
        resp = self.client.post(
            f"/v1/finance/direct-entries/?entity={entity.code}",
            {"lines": [{"account": "5300", "debit": 100000, "cost_center": "NOPE"},
                       {"account": "1100", "credit": 100000}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    # Verify customer opening balance posts opening invoice behavior.
    def test_customer_opening_balance_posts_opening_invoice(self):
        from .models import Invoice

        entity, _, _ = self.build_books()
        created = self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "OPN1", "name": "Opening Co", "opening_balance": 500000},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        # An opening invoice (Dr 1200 AR / Cr 3200 Retained Earnings) was raised —
        # opening balances credit equity, not current-period revenue.
        inv = Invoice.objects.get(entity=entity, source="OPENING", customer__code="OPN1")
        self.assertEqual(inv.status, "POSTED")
        self.assertEqual(inv.total, 500000)
        gl = {ln.account.code: (ln.debit, ln.credit) for ln in inv.journal.lines.all()}
        self.assertEqual(gl["1200"], (500000, 0))
        self.assertEqual(gl["3200"], (0, 500000))
        self.assertNotIn("4100", gl)
        # …and it surfaces in the customer's outstanding, now a paginated list.
        listed = self.client.get(f"/v1/finance/customers/?entity={entity.code}").json()
        self.assertIn("pagination", listed)
        row = next(r for r in listed["data"] if r["code"] == "OPN1")
        self.assertEqual(row["balance"], 500000)

    # Verify customer summary and status filter behavior.
    def test_customer_summary_and_status_filter(self):
        """The summary aggregates over ALL customers (accurate while the list paginates),
        and the list's derived-status filter narrows server-side to the matching rows."""
        entity, _, _ = self.build_books()
        # An opening balance makes this customer OVERDUE-or-ACTIVE with a receivable.
        self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "SUMA", "name": "Owes Money", "opening_balance": 300000}, format="json")
        self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "SUMB", "name": "Flat Co", "is_active": False}, format="json")  # INACTIVE

        summ = self.client.get(f"/v1/finance/customers/summary/?entity={entity.code}").json()["data"]
        self.assertEqual(summ["total"], 2)
        self.assertEqual(summ["receivable"]["kobo"], 300000)  # SUMA owes; due today, so ACTIVE
        self.assertEqual(summ["status_counts"]["ACTIVE"], 1)
        self.assertEqual(summ["status_counts"]["INACTIVE"], 1)
        self.assertEqual(sum(summ["status_counts"].values()), 2)

        active = self.client.get(
            f"/v1/finance/customers/?entity={entity.code}&status=ACTIVE").json()
        self.assertIn("pagination", active)
        self.assertEqual([r["code"] for r in active["data"]], ["SUMA"])

    # Verify payment summary totals and counts behavior.
    def test_payment_summary_totals_and_counts(self):
        entity, _, _ = self.build_books()
        c = self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "PSUM", "name": "Payer"}, format="json").json()["data"]
        # A receipt with no invoices → fully unallocated.
        self.client.post(
            f"/v1/finance/customers/{c['code']}/receipt/?entity={entity.code}",
            {"amount": 90000, "payment_date": "2026-01-15", "deposit_account": "1100",
             "auto_allocate": False}, format="json")
        summ = self.client.get(f"/v1/finance/payments/summary/?entity={entity.code}").json()["data"]
        self.assertEqual(summ["count"], 1)
        self.assertEqual(summ["unallocated"]["kobo"], 90000)
        self.assertEqual(summ["status_counts"]["UNALLOCATED"], 1)

    # Verify receipt largest first allocation behavior.
    def test_receipt_largest_first_allocation(self):
        from .models import Invoice

        entity, _, _ = self.build_books()
        c = self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "ALC", "name": "Alloc Co"}, format="json").json()["data"]

        # Prepare or verify the mk invoice test path.
        def mk_invoice(price, date):
            return self.client.post(
                f"/v1/finance/invoices/?entity={entity.code}",
                {"customer": "ALC", "invoice_date": date,
                 "lines": [{"revenue_account": "4100", "quantity": 1, "unit_price": price}]},
                format="json").json()["data"]

        small = mk_invoice(100000, "2026-01-05")   # older, smaller
        large = mk_invoice(300000, "2026-02-05")   # newer, larger
        # Receipt of exactly the large balance, largest-first → clears LARGE, leaves small.
        self.client.post(
            f"/v1/finance/customers/{c['id']}/receipt/?entity={entity.code}",
            {"amount": 300000, "payment_date": "2026-03-01", "deposit_account": "1100",
             "allocation_strategy": "largest"}, format="json")
        self.assertEqual(Invoice.objects.get(id=large["id"]).payment_status, "PAID")
        self.assertEqual(Invoice.objects.get(id=small["id"]).payment_status, "UNPAID")

    # Verify receipt rejects unknown allocation strategy behavior.
    def test_receipt_rejects_unknown_allocation_strategy(self):
        entity, _, _ = self.build_books()
        c = self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "BAD", "name": "Bad Co"}, format="json").json()["data"]
        resp = self.client.post(
            f"/v1/finance/customers/{c['id']}/receipt/?entity={entity.code}",
            {"amount": 100000, "payment_date": "2026-03-01", "deposit_account": "1100",
             "allocation_strategy": "fifo"}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)

    # Verify entity create provisions new books behavior.
    def test_entity_create_provisions_new_books(self):
        # Seed first so the NGN currency exists for the default base_currency.
        self._seed()
        resp = self.client.post(
            "/v1/finance/entities/",
            {"code": "crest", "name": "Crestfield Academy", "kind": "TENANT"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["code"], "CREST")          # normalised to uppercase
        self.assertEqual(data["base_currency"], "NGN")   # model default
        self.assertTrue(data["is_active"])

        # And it now shows up in the list endpoint.
        listed = self.client.get("/v1/finance/entities/")
        self.assertIn("CREST", {e["code"] for e in listed.json()["data"]})

        # The one POST provisions a fully usable set of books: chart of accounts
        # and twelve open periods, so no CLI seed_finance step is needed.
        accounts = self.client.get("/v1/finance/accounts/?entity=CREST").json()["data"]
        codes = {a["code"] for a in accounts}
        self.assertTrue({"1100", "1200", "3100"}.issubset(codes))  # cash, AR, share capital

        periods = self.client.get("/v1/finance/periods/?entity=CREST").json()["data"]
        self.assertEqual(len(periods), 12)
        self.assertTrue(all(p["status"] == "OPEN" for p in periods))

    # Verify customer crud and invoice filter behavior.
    def test_customer_crud_and_invoice_filter(self):
        entity, _, _ = self.build_books()
        # Create — receivable account defaults to 1200.
        resp = self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "cust1", "name": "Acme Ltd", "billing_email": "a@acme.test"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["code"], "CUST1")               # normalised
        self.assertEqual(data["receivable_account_code"], "1200")

        # Duplicate code rejected.
        dup = self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "CUST1", "name": "Dupe"}, format="json")
        self.assertEqual(dup.status_code, 400)

        # List + search.
        listed = self.client.get(
            f"/v1/finance/customers/?entity={entity.code}&search=acme").json()["data"]
        self.assertEqual({c["code"] for c in listed}, {"CUST1"})

        # Detail by code + PATCH.
        det = self.client.get(f"/v1/finance/customers/CUST1/?entity={entity.code}")
        self.assertEqual(det.status_code, 200)
        patched = self.client.patch(
            f"/v1/finance/customers/CUST1/?entity={entity.code}",
            {"name": "Acme Renamed"}, format="json")
        self.assertEqual(patched.json()["data"]["name"], "Acme Renamed")

    # Verify fee structure generates posted invoices behavior.
    def test_fee_structure_generates_posted_invoices(self):
        entity, _, _ = self.build_books()
        self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "stu1", "name": "Student One"}, format="json")

        # A fee structure with one ₦100,000 tuition line.
        created = self.client.post(
            f"/v1/finance/fee-structures/?entity={entity.code}",
            {"code": "jss1t1", "name": "JSS1 Term 1",
             "items": [{"description": "Tuition", "revenue_account": "4100", "amount": 10000000}]},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["data"]["total"], 10000000)

        # Generate → one posted invoice for the customer.
        gen = self.client.post(
            f"/v1/finance/fee-structures/JSS1T1/generate/?entity={entity.code}",
            {"customers": ["STU1"], "invoice_date": "2026-01-10"}, format="json")
        self.assertEqual(gen.status_code, 201, gen.content)
        gdata = gen.json()["data"]
        self.assertEqual(gdata["generated"], 1)
        self.assertEqual(gdata["invoices"][0]["status"], "POSTED")
        self.assertEqual(gdata["invoices"][0]["total"], 10000000)

        # It shows under the customer filter on the invoices list…
        inv = self.client.get(
            f"/v1/finance/invoices/?entity={entity.code}&customer=STU1").json()["data"]
        self.assertEqual(len(inv), 1)
        # …and the trial balance still balances (AR raised).
        tb = self.client.get(
            f"/v1/finance/reports/trial-balance/?entity={entity.code}").json()["data"]
        self.assertTrue(tb["is_balanced"])

        # Re-running is idempotent — no second invoice for the same customer/structure.
        again = self.client.post(
            f"/v1/finance/fee-structures/JSS1T1/generate/?entity={entity.code}",
            {"customers": ["STU1"]}, format="json")
        self.assertEqual(again.json()["data"]["generated"], 0)

    # Verify fee structure applies to defaults filters and edits behavior.
    def test_fee_structure_applies_to_defaults_filters_and_edits(self):
        """`applies_to` defaults to CUSTOMER, is filterable, and PATCHable."""
        entity, _, _ = self.build_books()

        # Default when omitted = CUSTOMER.
        cust = self.client.post(
            f"/v1/finance/fee-structures/?entity={entity.code}",
            {"code": "fs-cust", "name": "Client billing",
             "items": [{"description": "Tuition", "revenue_account": "4100", "amount": 5000000}]},
            format="json")
        self.assertEqual(cust.status_code, 201, cust.content)
        self.assertEqual(cust.json()["data"]["applies_to"], "CUSTOMER")
        self.assertEqual(cust.json()["data"]["applies_to_display"], "Customer")

        # Explicit non-customer type is accepted and case-insensitive.
        vend = self.client.post(
            f"/v1/finance/fee-structures/?entity={entity.code}",
            {"code": "fs-vend", "name": "Vendor charges", "applies_to": "vendor",
             "items": [{"description": "Service", "revenue_account": "4100", "amount": 3000000}]},
            format="json")
        self.assertEqual(vend.status_code, 201, vend.content)
        self.assertEqual(vend.json()["data"]["applies_to"], "VENDOR")

        # A bogus value is rejected.
        bad = self.client.post(
            f"/v1/finance/fee-structures/?entity={entity.code}",
            {"code": "fs-bad", "name": "x", "applies_to": "PARTNER",
             "items": [{"description": "x", "revenue_account": "4100", "amount": 100}]},
            format="json")
        self.assertEqual(bad.status_code, 400, bad.content)

        # ?applies_to= filters the list.
        only_vend = self.client.get(
            f"/v1/finance/fee-structures/?entity={entity.code}&applies_to=VENDOR").json()["data"]
        self.assertEqual([s["code"] for s in only_vend], ["FS-VEND"])

        # PATCH can re-classify a structure.
        patched = self.client.patch(
            f"/v1/finance/fee-structures/FS-CUST/?entity={entity.code}",
            {"applies_to": "STAFF"}, format="json")
        self.assertEqual(patched.status_code, 200, patched.content)
        self.assertEqual(patched.json()["data"]["applies_to"], "STAFF")

    # Verify fee structure lines carry code optional and tax breakdown behavior.
    def test_fee_structure_lines_carry_code_optional_and_tax_breakdown(self):
        entity, _, _ = self.build_books()
        vat = TaxCode.objects.create(
            entity=entity, code="VAT", name="VAT 7.5%", rate_bps=750,
            collected_account=Account.objects.get(entity=entity, code="2200"))

        created = self.client.post(
            f"/v1/finance/fee-structures/?entity={entity.code}",
            {"code": "fs-rich", "name": "Rich structure", "items": [
                {"code": "TUITION", "description": "Tuition", "revenue_account": "4100",
                 "amount": 10000000},
                {"code": "TRANSPORT", "description": "Transport", "revenue_account": "4100",
                 "amount": 2000000, "tax_code": "VAT", "is_optional": True},
            ]}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        data = created.json()["data"]
        # Subtotal (net) + tax (7.5% on the ₦20,000 transport line only) = gross.
        self.assertEqual(data["total"], 12000000)
        self.assertEqual(data["tax_total"], 150000)            # 2,000,000 × 750 / 10000
        self.assertEqual(data["total_with_tax"], 12150000)
        items = {it["code"]: it for it in data["items"]}
        self.assertFalse(items["TUITION"]["is_optional"])
        self.assertTrue(items["TRANSPORT"]["is_optional"])
        self.assertEqual(items["TRANSPORT"]["tax_code_value"], "VAT")

    # Verify fee structure detail reports usage and can be duplicated behavior.
    def test_fee_structure_detail_reports_usage_and_can_be_duplicated(self):
        entity, _, _ = self.build_books()
        self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "stu1", "name": "Student One"}, format="json")
        self.client.post(
            f"/v1/finance/fee-structures/?entity={entity.code}",
            {"code": "fs-src", "name": "Source", "items": [
                {"code": "TUITION", "description": "Tuition", "revenue_account": "4100",
                 "amount": 5000000, "is_optional": False}]}, format="json")
        # Generate one invoice → usage count should reflect it.
        self.client.post(
            f"/v1/finance/fee-structures/FS-SRC/generate/?entity={entity.code}",
            {"all_active": True, "invoice_date": "2026-01-10"}, format="json")

        detail = self.client.get(f"/v1/finance/fee-structures/FS-SRC/?entity={entity.code}")
        self.assertEqual(detail.status_code, 200)
        usage = detail.json()["data"]["usage"]
        self.assertEqual(usage["invoices_generated"], 1)
        self.assertIsNotNone(usage["last_generated_at"])

        # Duplicate → a new INACTIVE clone carrying the same lines (incl. fee code).
        dup = self.client.post(
            f"/v1/finance/fee-structures/FS-SRC/duplicate/?entity={entity.code}",
            {"code": "fs-copy", "name": "Copy"}, format="json")
        self.assertEqual(dup.status_code, 201, dup.content)
        clone = dup.json()["data"]
        self.assertEqual(clone["code"], "FS-COPY")
        self.assertFalse(clone["is_active"])
        self.assertEqual(clone["items"][0]["code"], "TUITION")
        self.assertEqual(clone["usage"]["invoices_generated"], 0)
        # Duplicating onto an existing code is rejected.
        clash = self.client.post(
            f"/v1/finance/fee-structures/FS-SRC/duplicate/?entity={entity.code}",
            {"code": "fs-copy"}, format="json")
        self.assertEqual(clash.status_code, 400, clash.content)

    # Verify fee structure generate blocked for non customer behavior.
    def test_fee_structure_generate_blocked_for_non_customer(self):
        """Only CUSTOMER structures can raise AR invoices."""
        entity, _, _ = self.build_books()
        self.client.post(
            f"/v1/finance/customers/?entity={entity.code}",
            {"code": "stu1", "name": "Student One"}, format="json")
        self.client.post(
            f"/v1/finance/fee-structures/?entity={entity.code}",
            {"code": "fs-staff", "name": "Staff deductions", "applies_to": "STAFF",
             "items": [{"description": "Levy", "revenue_account": "4100", "amount": 100000}]},
            format="json")
        gen = self.client.post(
            f"/v1/finance/fee-structures/FS-STAFF/generate/?entity={entity.code}",
            {"all_active": True}, format="json")
        self.assertEqual(gen.status_code, 400, gen.content)

    # Verify entity create accepts explicit fiscal year behavior.
    def test_entity_create_accepts_explicit_fiscal_year(self):
        self._seed()
        resp = self.client.post(
            "/v1/finance/entities/",
            {"code": "LEKKI", "name": "Lekki Books", "fiscal_year": 2027},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        periods = self.client.get("/v1/finance/periods/?entity=LEKKI").json()["data"]
        self.assertEqual(len(periods), 12)
        self.assertTrue(all(p["name"].startswith("2027-") for p in periods))

    # Verify entity create supports school year start month behavior.
    def test_entity_create_supports_school_year_start_month(self):
        # A school running Sept 2026 → Aug 2027: twelve periods roll over the
        # calendar boundary, labelled by the actual calendar month.
        self._seed()
        resp = self.client.post(
            "/v1/finance/entities/",
            {"code": "STMARY", "name": "St Mary's", "fiscal_year": 2026,
             "fiscal_start_month": 9},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        periods = self.client.get("/v1/finance/periods/?entity=STMARY").json()["data"]
        names = [p["name"] for p in periods]
        self.assertEqual(len(names), 12)
        self.assertEqual(names[0], "2026-09")    # first period: September 2026
        self.assertEqual(names[-1], "2027-08")   # last period: August 2027
        self.assertEqual(periods[0]["start_date"], "2026-09-01")
        self.assertEqual(periods[-1]["end_date"], "2027-08-31")
        self.assertTrue(all(p["status"] == "OPEN" for p in periods))

    # Verify entity create rejects duplicate code behavior.
    def test_entity_create_rejects_duplicate_code(self):
        self._seed()
        first = self.client.post(
            "/v1/finance/entities/", {"code": "CREST", "name": "Crestfield"}, format="json",
        )
        self.assertEqual(first.status_code, 201, first.content)
        dupe = self.client.post(
            "/v1/finance/entities/", {"code": "crest", "name": "Crestfield Dup"}, format="json",
        )
        self.assertEqual(dupe.status_code, 400)

    # Verify statement endpoints match service output behavior.
    def test_statement_endpoints_match_service_output(self):
        entity, _ = self._seed()
        ec = entity.code

        pnl = self.client.get(f"/v1/finance/reports/income-statement/?entity={ec}").json()["data"]
        self.assertEqual(pnl["totals"]["net"]["amount"]["kobo"], 180000)

        bs = self.client.get(f"/v1/finance/reports/balance-sheet/?entity={ec}").json()["data"]
        self.assertTrue(bs["is_balanced"])
        self.assertEqual(bs["total_assets"]["kobo"], 1180000)
        self.assertEqual(bs["retained_earnings"]["kobo"], 180000)

        cf = self.client.get(f"/v1/finance/reports/cash-flow/?entity={ec}").json()["data"]
        self.assertTrue(cf["is_reconciled"])
        self.assertEqual(cf["closing_cash"]["kobo"], 780000)
        self.assertEqual(cf["by_activity"]["financing"]["kobo"], 1000000)

        soce = self.client.get(f"/v1/finance/reports/changes-in-equity/?entity={ec}").json()["data"]
        self.assertTrue(soce["is_reconciled"])
        self.assertEqual(soce["total_closing"]["kobo"], 1180000)
        re = next(c for c in soce["columns"] if c["key"] == "retained_earnings")
        self.assertEqual(re["profit"]["kobo"], 180000)

        pack = self.client.get(f"/v1/finance/reports/statutory-pack/?entity={ec}").json()["data"]
        sofp = pack["statement_of_financial_position"]
        self.assertTrue(sofp["is_balanced"])
        self.assertEqual(sofp["total_assets"]["kobo"], 1180000)
        self.assertEqual(sofp["total_equity"]["kobo"], 1180000)
        self.assertEqual(pack["income_statement"]["net_income"]["kobo"], 180000)
        self.assertTrue(pack["cash_flow"]["is_reconciled"])
        self.assertTrue(pack["trial_balance"]["is_balanced"])

    # Verify journal list detail and post action behavior.
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

    # Verify unbalanced post returns typed error envelope behavior.
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

    # Verify period close action runs checklist behavior.
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

    # Verify period reopen and lock actions behavior.
    def test_period_reopen_and_lock_actions(self):
        entity, periods = self._seed()
        ec = entity.code
        pid = periods[0].id
        closed = self.client.post(
            f"/v1/finance/periods/{pid}/close/?entity={ec}", data={}, format="json")
        self.assertEqual(closed.status_code, 200, closed.content)
        # Re-open the closed period back to OPEN.
        reopened = self.client.post(
            f"/v1/finance/periods/{pid}/reopen/?entity={ec}", data={}, format="json")
        self.assertEqual(reopened.status_code, 200, reopened.content)
        self.assertEqual(reopened.json()["data"]["status"], PeriodStatus.OPEN)
        # Close again, then lock it (permanently sealed).
        self.client.post(f"/v1/finance/periods/{pid}/close/?entity={ec}", data={}, format="json")
        locked = self.client.post(
            f"/v1/finance/periods/{pid}/lock/?entity={ec}", data={}, format="json")
        self.assertEqual(locked.status_code, 200, locked.content)
        self.assertEqual(locked.json()["data"]["status"], PeriodStatus.LOCKED)

    # Verify trial balance exports in each format behavior.
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

    # Verify statement exports available behavior.
    def test_statement_exports_available(self):
        entity, _ = self._seed()
        ec = entity.code
        for path in ("income-statement", "balance-sheet", "changes-in-equity",
                     "statutory-pack", "ar-aging"):
            resp = self.client.get(f"/v1/finance/reports/{path}/?entity={ec}&export=xlsx")
            self.assertEqual(resp.status_code, 200, path)
            self.assertTrue(resp.content)

    # Verify unknown export format is rejected behavior.
    def test_unknown_export_format_is_rejected(self):
        entity, _ = self._seed()
        resp = self.client.get(
            f"/v1/finance/reports/trial-balance/?entity={entity.code}&export=docx"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["success"])


# Group tests for Ops Summary And Pagination Tests.
class OpsSummaryAndPaginationTests(_Phase4FixtureMixin, TestCase):
    """Finance-ops list endpoints paginate (page_size 25) and their /summary/
    siblings aggregate over **all** rows so header KPIs stay accurate.
    """

    # Prepare or verify the setUp test path.
    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment

        User = get_user_model()
        self.user = User.objects.create_user(
            email="ops-admin@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE", first_name="Ops", last_name="Admin",
        )
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(
            user=self.user, role=role, assignment_status="ACTIVE")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    # Support the claim workflow.
    def _claim(self, entity, *, unit_price):
        r = self.client.post(
            f"/v1/finance/expense-claims/?entity={entity.code}",
            {"claimant_name": "Jane Staff", "claim_date": "2026-01-10", "title": "Trip",
             "lines": [{"description": "Diesel", "expense_account": "5300",
                        "quantity": 1, "unit_price": unit_price}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["data"]["id"]

    # Verify expense list paginates and summary aggregates all rows behavior.
    def test_expense_list_paginates_and_summary_aggregates_all_rows(self):
        entity, _, _ = self.build_books()
        self._claim(entity, unit_price=100000)
        self._claim(entity, unit_price=300000)

        lst = self.client.get(f"/v1/finance/expense-claims/?entity={entity.code}")
        self.assertEqual(lst.status_code, 200, lst.content)
        body = lst.json()
        self.assertIn("pagination", body)
        self.assertEqual(body["pagination"]["pageSize"], 25)
        self.assertEqual(body["pagination"]["totalItems"], 2)

        summ = self.client.get(f"/v1/finance/expense-claims/summary/?entity={entity.code}")
        self.assertEqual(summ.status_code, 200, summ.content)
        data = summ.json()["data"]
        self.assertEqual(data["open"], 2)          # both drafts are open
        self.assertEqual(data["avg"], 200000)      # (100000 + 300000) / 2
        self.assertEqual(data["awaiting"], 0)      # none posted yet

    # Verify ops summary endpoints handle empty books behavior.
    def test_ops_summary_endpoints_handle_empty_books(self):
        entity, _, _ = self.build_books()
        for path, keys in (
            ("expense-claims", {"open", "month_total", "avg", "awaiting"}),
            ("payroll-runs", {"runs", "employees", "net", "to_pay"}),
            ("fixed-assets", {"cost", "accum", "nbv", "monthly"}),
            ("tax-filings", {"outstanding", "open", "filed", "paid"}),
        ):
            r = self.client.get(f"/v1/finance/{path}/summary/?entity={entity.code}")
            self.assertEqual(r.status_code, 200, f"{path}: {r.content}")
            self.assertEqual(set(r.json()["data"].keys()), keys, path)

    # ── Audit trail ───────────────────────────────────────────────────────
    # Support the audit workflow.
    def _audit(self, entity, **kw):
        defaults = dict(
            entity=entity, actor=self.user,
            action=FinanceAuditAction.JOURNAL_POSTED,
            status=FinanceAuditStatus.SUCCESS,
            target_type="JournalEntry", target_id="1", document_number="JE-1",
            message="", before={}, after={}, metadata={"secret": "internal-only"},
        )
        defaults.update(kw)
        return FinanceAuditLog.objects.create(**defaults)

    # Verify audit log lists and never leaks metadata behavior.
    def test_audit_log_lists_and_never_leaks_metadata(self):
        entity, _, _ = self.build_books()
        self._audit(entity, before={"status": "DRAFT"}, after={"status": "POSTED"},
                    document_number="JE-9")
        resp = self.client.get(f"/v1/finance/audit-logs/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = resp.json()["data"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertNotIn("metadata", row)                      # internal bag stays server-side
        self.assertEqual(row["before"], {"status": "DRAFT"})
        self.assertEqual(row["after"], {"status": "POSTED"})
        self.assertEqual(row["action_display"], "Journal posted")
        self.assertEqual(row["actor"], self.user.email)

    # Verify audit log filters behavior.
    def test_audit_log_filters(self):
        from django.utils import timezone
        entity, _, _ = self.build_books()
        self._audit(entity, action=FinanceAuditAction.JOURNAL_POSTED,
                    status=FinanceAuditStatus.SUCCESS,
                    created_at=timezone.make_aware(datetime.datetime(2026, 1, 5, 9, 0)))
        self._audit(entity, action=FinanceAuditAction.JOURNAL_POST_REJECTED,
                    status=FinanceAuditStatus.FAILED, target_type="Payment",
                    created_at=timezone.make_aware(datetime.datetime(2026, 6, 20, 9, 0)))

        base = f"/v1/finance/audit-logs/?entity={entity.code}"
        self.assertEqual(len(self.client.get(base).json()["data"]), 2)
        self.assertEqual(len(self.client.get(base + "&status=FAILED").json()["data"]), 1)
        self.assertEqual(len(self.client.get(base + "&action=JOURNAL_POSTED").json()["data"]), 1)
        self.assertEqual(len(self.client.get(base + "&target_type=Payment").json()["data"]), 1)
        self.assertEqual(len(self.client.get(base + f"&actor={self.user.id}").json()["data"]), 2)
        # Inclusive date window keeps only the January row.
        win = self.client.get(base + "&date_from=2026-01-01&date_to=2026-01-31").json()["data"]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["action"], "JOURNAL_POSTED")

    # Verify audit log scoped to entity behavior.
    def test_audit_log_scoped_to_entity(self):
        entity, _, _ = self.build_books()
        other = LedgerEntity.objects.create(
            name="Other Books", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        self._audit(entity, document_number="MINE")
        self._audit(other, document_number="THEIRS")
        rows = self.client.get(f"/v1/finance/audit-logs/?entity={entity.code}").json()["data"]
        self.assertEqual([r["document_number"] for r in rows], ["MINE"])

    # Verify audit facets return present options only behavior.
    def test_audit_facets_return_present_options_only(self):
        entity, _, _ = self.build_books()
        self._audit(entity, action=FinanceAuditAction.JOURNAL_POSTED, target_type="JournalEntry")
        self._audit(entity, action=FinanceAuditAction.PAYMENT_POSTED, target_type="Payment")
        # Two rows share JOURNAL_POSTED — the facet must still be de-duplicated
        # (guards the .distinct()/Meta.ordering gotcha that returned dup codes).
        self._audit(entity, action=FinanceAuditAction.JOURNAL_POSTED, target_type="JournalEntry")
        data = self.client.get(f"/v1/finance/audit-logs/facets/?entity={entity.code}").json()["data"]
        self.assertEqual([a["email"] for a in data["actors"]], [self.user.email])
        self.assertEqual(set(data["target_types"]), {"JournalEntry", "Payment"})
        codes = [a["value"] for a in data["actions"]]
        self.assertEqual(sorted(codes), ["JOURNAL_POSTED", "PAYMENT_POSTED"])   # no dupes
        self.assertEqual(
            {a["value"]: a["label"] for a in data["actions"]},
            {"JOURNAL_POSTED": "Journal posted", "PAYMENT_POSTED": "Payment posted"})


# Group tests for Entity Create Permission Tests.
class EntityCreatePermissionTests(TestCase):
    """Provisioning a new entity must be gated on ``finance.entity.create``.

    A plain authenticated staff user holding no role (hence no grant) must be
    denied — proving the POST is RBAC-gated, not open like the GET-only list was.
    """

    # Prepare or verify the setUp test path.
    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        User = get_user_model()
        self.user = User.objects.create_user(
            email="no-grant@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="No", last_name="Grant",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    # Verify create denied without grant behavior.
    def test_create_denied_without_grant(self):
        resp = self.client.post(
            "/v1/finance/entities/", {"code": "NOPE", "name": "Nope"}, format="json",
        )
        self.assertEqual(resp.status_code, 403)

    # Verify list still denied without view grant behavior.
    def test_list_still_denied_without_view_grant(self):
        # The GET side is gated on finance.entity.view; same ungranted user is denied.
        resp = self.client.get("/v1/finance/entities/")
        self.assertEqual(resp.status_code, 403)

    # Verify direct entry denied without grant behavior.
    def test_direct_entry_denied_without_grant(self):
        # Posting a direct entry is gated on finance.directentry.post (CRITICAL).
        resp = self.client.post(
            "/v1/finance/direct-entries/?entity=TBOOK",
            {"lines": [{"account": "1100", "debit": 100}, {"account": "3100", "credit": 100}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    # Verify audit view denied without grant behavior.
    def test_audit_view_denied_without_grant(self):
        # The audit trail (and its facets) are gated on finance.audit.view.
        self.assertEqual(self.client.get("/v1/finance/audit-logs/?entity=TBOOK").status_code, 403)
        self.assertEqual(self.client.get("/v1/finance/audit-logs/facets/?entity=TBOOK").status_code, 403)


# Group tests for Stub User.
class _StubUser:
    """A minimal user carrying just the attributes get_queryset reads."""
    # Initialize this object with its required state.
    def __init__(self, user_type, school=None):
        self.user_type = user_type
        self.school = school


# Group tests for Stub Request.
class _StubRequest:
    """A minimal request exposing user + query_params (and optionally .school)."""
    # Initialize this object with its required state.
    def __init__(self, user, params=None):
        self.user = user
        self.query_params = params or {}


# Group tests for Entity List Scoping Tests.
class EntityListScopingTests(TestCase):
    """EntityListCreateView.get_queryset is tenancy-scoped for non-platform staff (F1)."""

    # Prepare or verify the setUp test path.
    def setUp(self):
        self.school = School.objects.create(name="Greenfield", slug="greenfield-f1", code="GRNF1")
        self.other = School.objects.create(name="Bluewater", slug="bluewater-f1", code="BLUF1")
        self.mine = LedgerEntity.objects.create(
            name="Greenfield Books", code="GREENF1",
            kind=LedgerEntity.Kind.TENANT, source_school=self.school,
        )
        self.theirs = LedgerEntity.objects.create(
            name="Bluewater Books", code="BLUEF1",
            kind=LedgerEntity.Kind.TENANT, source_school=self.other,
        )

    # Support the codes workflow.
    def _codes(self, user, params=None):
        from vs_finance.views import EntityListCreateView

        view = EntityListCreateView()
        view.request = _StubRequest(user=user, params=params)
        return set(view.get_queryset().values_list("code", flat=True))

    # Verify cx staff sees every entity behavior.
    def test_cx_staff_sees_every_entity(self):
        codes = self._codes(_StubUser("CX_STAFF"))
        self.assertTrue({"GREENF1", "BLUEF1"} <= codes)

    # Verify school user sees only own behavior.
    def test_school_user_sees_only_own(self):
        codes = self._codes(_StubUser("SCHOOL_STAFF", school=self.school))
        self.assertEqual(codes, {"GREENF1"})

    # Verify user without school sees none behavior.
    def test_user_without_school_sees_none(self):
        self.assertEqual(self._codes(_StubUser("SCHOOL_STAFF")), set())

    # Verify scoping composes with kind filter behavior.
    def test_scoping_composes_with_kind_filter(self):
        codes = self._codes(_StubUser("SCHOOL_STAFF", school=self.school),
                            params={"kind": LedgerEntity.Kind.TENANT})
        self.assertEqual(codes, {"GREENF1"})


# Group tests for Finance Dashboard Tests.
class FinanceDashboardTests(_ARFixtureMixin, TestCase):
    """The aggregated Finance-overview payload computes every block from the GL."""

    # Verify dashboard payload reflects posted invoice behavior.
    def test_dashboard_payload_reflects_posted_invoice(self):
        from django.contrib.auth import get_user_model

        from vs_finance.dashboard import finance_dashboard

        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        post_invoice(inv)  # Dr AR 100,000 ; Cr Revenue 100,000 (no tax)

        # A journal with a real author guards the actor-label path (the custom User
        # model has no get_full_name; recent_journals must compose first/last/email).
        author = get_user_model().objects.create_user(
            email="fin.officer@test.com", password="x",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="Fin", last_name="Officer",
        )
        je = JournalEntry.objects.create(
            entity=entity, date=datetime.date(2026, 1, 20), period=period,
            narration="manual adj", created_by=author,
        )
        JournalLine.objects.create(
            entry=je, account=Account.objects.get(entity=entity, code="1100"),
            debit=5000, credit=0, line_no=1,
        )
        JournalLine.objects.create(
            entry=je, account=Account.objects.get(entity=entity, code="4100"),
            debit=0, credit=5000, line_no=2,
        )

        # A capital injection into 1100 Cash & Bank must surface as cash position —
        # even with no operational BankAccount record (the reported glitch).
        post_journal(self.make_entry(entity, period, [("1100", 8000000, 0), ("3100", 0, 8000000)]))

        d = finance_dashboard(entity)

        self.assertIn("Fin Officer", [j["created_by"] for j in d["recent_journals"]])

        # Cash position reflects the 1100 posting and reconciles to the cash-flow stmt.
        from vs_finance.reports import cash_flow_statement
        self.assertEqual(d["kpis"]["cash_position"]["value"]["kobo"], 8000000)
        self.assertEqual(
            d["kpis"]["cash_position"]["value"]["kobo"], cash_flow_statement(entity).closing_cash,
        )
        self.assertEqual(d["fiscal_year"], "2026")

        # As-of defaults to the present day; pinning a period moves it to period-end.
        self.assertEqual(d["as_of"], datetime.date.today().isoformat())
        pinned = finance_dashboard(entity, period=period)
        self.assertEqual(pinned["as_of"], period.end_date.isoformat())

        # Top-level blocks all present.
        for key in (
            "kpis", "revenue_vs_budget", "ar_aging", "trend", "top_overdue",
            "vendor_due", "approvals", "close_progress", "recent_journals",
        ):
            self.assertIn(key, d)

        # KPI envelope shape.
        for kpi in d["kpis"].values():
            self.assertIn("value", kpi)
            self.assertIn("delta_pct", kpi)
            self.assertIsInstance(kpi["spark"], list)

        # Receivables + net income reflect the posted invoice.
        self.assertEqual(d["kpis"]["receivables"]["value"]["kobo"], 100000)
        self.assertEqual(d["kpis"]["net_income_ytd"]["value"]["kobo"], 100000)
        self.assertEqual(d["ar_aging"]["total"]["kobo"], 100000)

        # The overdue invoice (due 25 Jan, as-of 31 Jan) tops the overdue list.
        self.assertTrue(d["top_overdue"])
        self.assertEqual(d["top_overdue"][0]["reference"], inv.document_number)
        self.assertEqual(d["top_overdue"][0]["amount"]["kobo"], 100000)

        # Trend is a fixed 12-month window; recent journals include the AR entry.
        self.assertEqual(len(d["trend"]["labels"]), 12)
        self.assertEqual(len(d["trend"]["issued"]), 12)
        self.assertTrue(d["recent_journals"])

        # Period close progress runs the checklist for the open period.
        self.assertIsNotNone(d["close_progress"])
        self.assertEqual(d["close_progress"]["total"], len(d["close_progress"]["checks"]))


# Group tests for Invoice Detail Endpoint Tests.
class InvoiceDetailEndpointTests(_ARFixtureMixin, TestCase):
    """The invoice detail drawer endpoint returns lines + GL postings."""

    # Verify detail returns lines and postings behavior.
    def test_detail_returns_lines_and_postings(self):
        import json
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment
        from vs_finance.views import InvoiceDetailView

        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, vat)])
        post_invoice(inv)

        u = get_user_model().objects.create_user(
            email="inv-detail@test.com", password="x", user_type="CX_STAFF", status="ACTIVE",
            first_name="Inv", last_name="Detail")
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")

        req = APIRequestFactory().get(f"/v1/finance/invoices/{inv.pk}/", {"entity": entity.code})
        force_authenticate(req, user=u)
        resp = InvoiceDetailView.as_view()(req, pk=inv.pk)
        resp.render()
        self.assertEqual(resp.status_code, 200)
        d = json.loads(resp.content)["data"]
        self.assertEqual(d["invoice"]["document_number"], inv.document_number)
        self.assertTrue(d["lines"])
        self.assertEqual(d["lines"][0]["account_code"], "4100")
        self.assertTrue(d["gl_postings"])
        self.assertEqual(d["summary"]["total"]["kobo"], inv.total)

    # Verify detail surfaces credit note concession and write off settlements behavior.
    def test_detail_surfaces_credit_note_concession_and_write_off_settlements(self):
        """A credit note, a concession and a write-off must all appear in settlements,
        gl_journals and the summary — not just cash payments."""
        import json
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment
        from vs_finance.views import InvoiceDetailView

        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        post_invoice(inv)  # total 100,000, all AR

        # 30,000 credit note + 20,000 concession, then write off the remaining balance.
        note = CreditNote.objects.create(
            entity=entity, customer=customer, kind=CreditNoteKind.CREDIT,
            note_date=datetime.date(2026, 1, 15), invoice=inv, reason="Goodwill",
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=Account.objects.get(entity=entity, code="4900"),
            quantity=1, unit_price=30000, tax_code=None, line_no=1,
        )
        post_credit_note(note, auto_allocate=True)
        concession = Concession.objects.create(
            entity=entity, customer=customer, invoice=inv, kind="WAIVER",
            concession_date=datetime.date(2026, 1, 18), amount=20000,
        )
        post_concession(concession)
        write_off_invoice(inv, write_off_date=datetime.date(2026, 1, 28))
        inv.refresh_from_db()

        u = get_user_model().objects.create_user(
            email="inv-settle@test.com", password="x", user_type="CX_STAFF", status="ACTIVE",
            first_name="Inv", last_name="Settle")
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")

        req = APIRequestFactory().get(f"/v1/finance/invoices/{inv.pk}/", {"entity": entity.code})
        force_authenticate(req, user=u)
        resp = InvoiceDetailView.as_view()(req, pk=inv.pk)
        resp.render()
        self.assertEqual(resp.status_code, 200)
        d = json.loads(resp.content)["data"]

        # Summary now splits cash vs non-cash and fully settles the invoice.
        self.assertEqual(d["summary"]["paid"]["kobo"], 0)
        self.assertEqual(d["summary"]["credited"]["kobo"], 100000)
        self.assertEqual(d["summary"]["settled"]["kobo"], 100000)
        self.assertEqual(d["summary"]["balance"]["kobo"], 0)

        # Settlements carry the credit note, the concession and the write-off (no cash).
        types = {s["type"] for s in d["settlements"]}
        self.assertEqual(types, {"CREDIT_NOTE", "CONCESSION", "WRITE_OFF"})
        self.assertEqual([s["type"] for s in d["settlements"] if s["type"] == "PAYMENT"], [])

        # GL history has every journal: invoice, credit note, concession, write-off.
        doc_types = {g["document_type"] for g in d["gl_journals"]}
        self.assertEqual(doc_types, {"INVOICE", "CREDIT_NOTE", "CONCESSION", "WRITE_OFF"})

        # Activity timeline mentions all three non-cash events.
        labels = " ".join(a["label"] for a in d["activity"])
        self.assertIn("Credit note", labels)
        self.assertIn("Waiver", labels)
        self.assertIn("Write-off", labels)


# Group tests for Finance Document Endpoint Tests.
class FinanceDocumentEndpointTests(_ARFixtureMixin, TestCase):
    """Printable invoice and receipt document endpoints."""

    # Support the user workflow.
    def _user(self, email="finance-docs@test.com"):
        from django.contrib.auth import get_user_model
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment

        u = get_user_model().objects.create_user(
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",
            first_name="Finance", last_name="Docs",
        )
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")
        return u

    # Support the request workflow.
    def _request(self, path, entity, user):
        from rest_framework.test import APIRequestFactory, force_authenticate

        req = APIRequestFactory().get(path, {"entity": entity.code})
        force_authenticate(req, user=user)
        return req

    # Verify invoice document renders html with collection account behavior.
    def test_invoice_document_renders_html_with_collection_account(self):
        from vs_finance.documents import primary_collection_account
        from vs_finance.views import InvoiceDocumentView

        entity, period, customer, vat = self.build_ar()
        # BankAccount.gl_account is unique per entity, so each bank account needs its
        # own GL cash account.
        cash_ops = Account.objects.create(
            entity=entity, code="1101", name="Cash — Operations", account_type="ASSET",
        )
        BankAccount.objects.create(
            entity=entity, name="Operations Account",
            bank_name="Access Bank",
            account_number="111",
            gl_account=cash_ops,
            is_active=True,
        )
        collection = BankAccount.objects.create(
            entity=entity, name="Collections Account",
            bank_name="GTBank",
            account_number="222",
            gl_account=Account.objects.get(entity=entity, code="1100"),
            is_active=True,
            is_primary_collection=True,
        )
        inv = self.make_invoice(
            entity, customer, lines=[("4100", 1, 100000, None)],
            due=datetime.date(2026, 1, 31),
        )
        post_invoice(inv)

        self.assertEqual(primary_collection_account(entity), collection)
        req = self._request(f"/v1/finance/invoices/{inv.pk}/document/", entity, self._user())
        resp = InvoiceDocumentView.as_view()(req, pk=inv.pk)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/html; charset=utf-8")
        html = resp.content.decode()
        self.assertIn("Tax Invoice", html)
        self.assertIn(inv.document_number, html)
        self.assertIn("Acme Ltd", html)
        self.assertIn("GTBank", html)
        self.assertIn("222", html)

    # Verify receipt document renders html behavior.
    def test_receipt_document_renders_html(self):
        from vs_finance.views_ar import PaymentReceiptView

        entity, period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, None)])
        post_invoice(inv)
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=40000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment)
        payment.refresh_from_db()

        req = self._request(f"/v1/finance/payments/{payment.pk}/receipt/", entity, self._user("receipt-docs@test.com"))
        resp = PaymentReceiptView.as_view()(req, pk=payment.pk)

        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Receipt", html)
        self.assertIn(payment.document_number, html)
        self.assertIn(inv.document_number, html)
        self.assertIn("Four hundred naira only", html)

    # Verify primary collection account falls back to first active account behavior.
    def test_primary_collection_account_falls_back_to_first_active_account(self):
        from vs_finance.documents import primary_collection_account

        entity, period, customer, vat = self.build_ar()
        # BankAccount.gl_account is unique per entity — give each its own GL account.
        cash2 = Account.objects.create(
            entity=entity, code="1102", name="Cash — Secondary", account_type="ASSET",
        )
        inactive = BankAccount.objects.create(
            entity=entity, name="Inactive",
            gl_account=Account.objects.get(entity=entity, code="1100"),
            is_active=False,
        )
        active = BankAccount.objects.create(
            entity=entity, name="Active Collections",
            gl_account=cash2,
            is_active=True,
        )

        self.assertEqual(primary_collection_account(entity), active)
        inactive.is_primary_collection = True
        inactive.save(update_fields=["is_primary_collection", "updated_at"])
        self.assertEqual(primary_collection_account(entity), inactive)

    @override_settings(PLATFORM_ISSUER={
        "name": "CodeX", "tagline": "Run your school", "address": "12 Marina, Lagos",
        "email": "billing@codex.example", "phone": "+234 1 000 0000",
        "website": "codex.example", "logo_url": "",
    })
    def test_platform_entity_prints_codex_issuer_details(self):
        # When the CodeX platform entity bills a customer (a school), the document
        # letterhead is CodeX's own identity from PLATFORM_ISSUER — not blanks.
        from vs_finance.views import InvoiceDocumentView

        seed_currencies()
        platform = LedgerEntity.objects.create(
            name="CodeX Platform Books", code="PLATX", kind=LedgerEntity.Kind.PLATFORM,
        )
        seed_chart_of_accounts(platform)
        year = FiscalYear.objects.create(
            entity=platform, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=platform, fiscal_year=year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )
        customer = Customer.objects.create(
            entity=platform, code="SCH1", name="Crestfield Academy",
            receivable_account=Account.objects.get(entity=platform, code="1200"),
        )
        inv = Invoice.objects.create(
            entity=platform, customer=customer,
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 25),
        )
        InvoiceLine.objects.create(
            invoice=inv, revenue_account=Account.objects.get(entity=platform, code="4100"),
            quantity=1, unit_price=5000000, tax_code=None, line_no=1,
        )
        post_invoice(inv)

        req = self._request(
            f"/v1/finance/invoices/{inv.pk}/document/", platform,
            self._user("codex-doc@test.com"))
        resp = InvoiceDocumentView.as_view()(req, pk=inv.pk)
        html = resp.content.decode()

        self.assertEqual(resp.status_code, 200)
        self.assertIn("CodeX", html)                 # CodeX issuer name (not blank)
        self.assertIn("12 Marina, Lagos", html)       # CodeX address from PLATFORM_ISSUER
        self.assertIn("Crestfield Academy", html)     # the school is the customer here


# Group tests for Finance Migration State Tests.
class FinanceMigrationStateTests(TestCase):
    # Verify bank account primary collection column exists behavior.
    def test_bank_account_primary_collection_column_exists(self):
        from django.db import connection

        with connection.cursor() as cursor:
            columns = {
                col.name
                for col in connection.introspection.get_table_description(
                    cursor, BankAccount._meta.db_table)
            }
        self.assertIn("is_primary_collection", columns)


# Group tests for Invoice Create Endpoint Tests.
class InvoiceCreateEndpointTests(_ARFixtureMixin, TestCase):
    """POST /finance/invoices/ raises (and posts) a manual invoice, gated on create."""

    # Support the super admin workflow.
    def _super_admin(self, email):
        from django.contrib.auth import get_user_model
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment
        u = get_user_model().objects.create_user(
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",
            first_name="Inv", last_name="Maker")
        role, _ = PlatformRoleTemplate.objects.get_or_create(id="xvs_super_admin", defaults={"name": "Super Admin"})
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")
        return u

    # Support the post workflow.
    def _post(self, entity, user, body):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_finance.views import InvoiceListCreateView as InvoiceListView
        req = APIRequestFactory().post(
            f"/v1/finance/invoices/?entity={entity.code}", body, format="json")
        force_authenticate(req, user=user)
        resp = InvoiceListView.as_view()(req)
        resp.render()
        return resp

    # Verify create posts invoice with tax behavior.
    def test_create_posts_invoice_with_tax(self):
        import json
        from vs_finance.constants import DocumentStatus
        entity, _period, _customer, vat = self.build_ar()
        u = self._super_admin("inv-create@test.com")
        resp = self._post(entity, u, {
            "customer": "CUST1", "invoice_date": "2026-01-10", "due_date": "2026-01-25",
            "lines": [{"revenue_account": "4100", "description": "Consulting",
                       "quantity": 2, "unit_price": 50000, "tax_code": "VAT"}],
        })
        self.assertEqual(resp.status_code, 201)
        d = json.loads(resp.content)["data"]
        self.assertEqual(d["status"], DocumentStatus.POSTED)
        self.assertEqual(d["subtotal"], 100000)
        self.assertEqual(d["tax_total"], 7500)
        self.assertEqual(d["total"], 107500)
        inv = Invoice.objects.get(pk=d["id"])
        self.assertIsNotNone(inv.journal_id)   # AR journal raised

    # Verify create draft when post false behavior.
    def test_create_draft_when_post_false(self):
        import json
        from vs_finance.constants import DocumentStatus
        entity, _p, _c, _vat = self.build_ar()
        u = self._super_admin("inv-draft@test.com")
        resp = self._post(entity, u, {
            "customer": "CUST1", "invoice_date": "2026-01-10", "post": False,
            "lines": [{"revenue_account": "4100", "quantity": 1, "unit_price": 30000}],
        })
        self.assertEqual(resp.status_code, 201)
        d = json.loads(resp.content)["data"]
        self.assertEqual(d["status"], DocumentStatus.DRAFT)
        self.assertEqual(d["total"], 30000)
        self.assertIsNone(Invoice.objects.get(pk=d["id"]).journal_id)

    # Verify create requires permission behavior.
    def test_create_requires_permission(self):
        from django.contrib.auth import get_user_model
        entity, _p, _c, _vat = self.build_ar()
        # A plain active user with no super-admin role lacks finance.invoice.create.
        u = get_user_model().objects.create_user(
            email="inv-nobody@test.com", password="x", user_type="CX_STAFF",
            status="ACTIVE", first_name="No", last_name="Perm")
        resp = self._post(entity, u, {
            "customer": "CUST1", "invoice_date": "2026-01-10",
            "lines": [{"revenue_account": "4100", "quantity": 1, "unit_price": 30000}],
        })
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Invoice.objects.filter(entity=entity).count(), 0)

    # Verify create rejects empty lines behavior.
    def test_create_rejects_empty_lines(self):
        entity, _p, _c, _vat = self.build_ar()
        u = self._super_admin("inv-empty@test.com")
        resp = self._post(entity, u, {"customer": "CUST1", "invoice_date": "2026-01-10", "lines": []})
        self.assertEqual(resp.status_code, 400)


# Group tests for Invoice Pay Remind Endpoint Tests.
class InvoicePayRemindEndpointTests(_ARFixtureMixin, TestCase):
    """POST /invoices/<id>/pay/ records a receipt; /remind/ raises a dunning notice."""

    # Support the super admin workflow.
    def _super_admin(self, email):
        from django.contrib.auth import get_user_model
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment
        u = get_user_model().objects.create_user(
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",
            first_name="Pay", last_name="Tester")
        role, _ = PlatformRoleTemplate.objects.get_or_create(id="xvs_super_admin", defaults={"name": "Super Admin"})
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")
        return u

    # Support the call workflow.
    def _call(self, view, entity, user, pk, body):
        from rest_framework.test import APIRequestFactory, force_authenticate
        req = APIRequestFactory().post(f"/v1/finance/invoices/{pk}/x/?entity={entity.code}", body, format="json")
        force_authenticate(req, user=user)
        resp = view.as_view()(req, pk=pk)
        resp.render()
        return resp

    # Support the posted invoice workflow.
    def _posted_invoice(self):
        entity, _period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, vat)])
        post_invoice(inv)
        return entity, inv

    # Verify pay settles invoice behavior.
    def test_pay_settles_invoice(self):
        import json
        from vs_finance.constants import InvoicePaymentStatus
        from vs_finance.views_ar import InvoicePayView
        entity, inv = self._posted_invoice()
        u = self._super_admin("pay-ok@test.com")
        resp = self._call(InvoicePayView, entity, u, inv.pk, {
            "amount": inv.total, "payment_date": "2026-01-20",
            "method": "BANK_TRANSFER", "deposit_account": "1100",
        })
        self.assertEqual(resp.status_code, 201)
        d = json.loads(resp.content)["data"]
        self.assertEqual(d["payment_status"], InvoicePaymentStatus.PAID)
        self.assertEqual(d["balance_due"], 0)
        inv.refresh_from_db()
        self.assertEqual(inv.amount_paid, inv.total)

    # Verify partial payment leaves balance behavior.
    def test_partial_payment_leaves_balance(self):
        import json
        from vs_finance.constants import InvoicePaymentStatus
        from vs_finance.views_ar import InvoicePayView
        entity, inv = self._posted_invoice()
        u = self._super_admin("pay-part@test.com")
        resp = self._call(InvoicePayView, entity, u, inv.pk, {
            "amount": 40000, "payment_date": "2026-01-20", "deposit_account": "1100",
        })
        self.assertEqual(resp.status_code, 201)
        d = json.loads(resp.content)["data"]
        self.assertEqual(d["payment_status"], InvoicePaymentStatus.PARTIAL)
        self.assertEqual(d["balance_due"], inv.total - 40000)

    # Verify pay requires permission behavior.
    def test_pay_requires_permission(self):
        from django.contrib.auth import get_user_model
        from vs_finance.models import Payment
        from vs_finance.views_ar import InvoicePayView
        entity, inv = self._posted_invoice()
        u = get_user_model().objects.create_user(
            email="pay-nobody@test.com", password="x", user_type="CX_STAFF",
            status="ACTIVE", first_name="No", last_name="Perm")
        resp = self._call(InvoicePayView, entity, u, inv.pk, {
            "amount": inv.total, "payment_date": "2026-01-20", "deposit_account": "1100",
        })
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 0)

    # Verify remind raises and sends notice behavior.
    def test_remind_raises_and_sends_notice(self):
        import json
        from vs_finance.constants import DunningNoticeStatus
        from vs_finance.models import DunningNotice
        from vs_finance.views_ar import InvoiceRemindView
        entity, inv = self._posted_invoice()   # due 2026-01-25 → overdue today
        u = self._super_admin("remind-ok@test.com")
        resp = self._call(InvoiceRemindView, entity, u, inv.pk, {})
        self.assertEqual(resp.status_code, 200)
        d = json.loads(resp.content)["data"]
        self.assertEqual(d["notice_status"], DunningNoticeStatus.SENT)
        notice = DunningNotice.objects.get(invoice=inv)
        self.assertEqual(notice.notice_status, DunningNoticeStatus.SENT)
        self.assertGreaterEqual(notice.level, 1)

    # Verify remind is idempotent on level behavior.
    def test_remind_is_idempotent_on_level(self):
        from vs_finance.models import DunningNotice
        from vs_finance.views_ar import InvoiceRemindView
        entity, inv = self._posted_invoice()
        u = self._super_admin("remind-twice@test.com")
        self._call(InvoiceRemindView, entity, u, inv.pk, {})
        self._call(InvoiceRemindView, entity, u, inv.pk, {})
        # Same (invoice, level) → reused, never a duplicate row.
        self.assertEqual(DunningNotice.objects.filter(invoice=inv).count(), 1)


# Group tests for Customer Endpoint Tests.
class CustomerEndpointTests(_ARFixtureMixin, TestCase):
    """Customer list balance/status, enriched detail/statement, and receipt."""

    # Support the super admin workflow.
    def _super_admin(self, email):
        from django.contrib.auth import get_user_model
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment
        u = get_user_model().objects.create_user(
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",
            first_name="Cust", last_name="Tester")
        role, _ = PlatformRoleTemplate.objects.get_or_create(id="xvs_super_admin", defaults={"name": "Super Admin"})
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")
        return u

    # Support the fixture workflow.
    def _fixture(self):
        entity, _period, customer, vat = self.build_ar()
        inv = self.make_invoice(entity, customer, lines=[("4100", 1, 100000, vat)])  # total 107500
        post_invoice(inv)
        return entity, customer, inv

    # Verify list includes balance and status behavior.
    def test_list_includes_balance_and_status(self):
        import json
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_finance.views_ar import CustomerListCreateView
        entity, customer, inv = self._fixture()
        u = self._super_admin("cust-list@test.com")
        req = APIRequestFactory().get("/v1/finance/customers/", {"entity": entity.code})
        force_authenticate(req, user=u)
        resp = CustomerListCreateView.as_view()(req); resp.render()
        self.assertEqual(resp.status_code, 200)
        row = next(r for r in json.loads(resp.content)["data"] if r["code"] == customer.code)
        self.assertEqual(row["balance"], inv.total)
        self.assertEqual(row["account_status"], "OVERDUE")  # due 2026-01-25 is past

    # Verify detail returns statement and summary behavior.
    def test_detail_returns_statement_and_summary(self):
        import json
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_finance.views_ar import CustomerDetailView
        entity, customer, inv = self._fixture()
        u = self._super_admin("cust-detail@test.com")
        req = APIRequestFactory().get(f"/v1/finance/customers/{customer.pk}/", {"entity": entity.code})
        force_authenticate(req, user=u)
        resp = CustomerDetailView.as_view()(req, pk=str(customer.pk)); resp.render()
        self.assertEqual(resp.status_code, 200)
        d = json.loads(resp.content)["data"]
        self.assertEqual(d["summary"]["current_balance"]["kobo"], inv.total)
        self.assertEqual(d["summary"]["open_invoice_count"], 1)
        self.assertTrue(d["statement"])
        self.assertEqual(d["statement"][-1]["balance"]["kobo"], inv.total)

    # Verify receipt settles and allocates behavior.
    def test_receipt_settles_and_allocates(self):
        import json
        from vs_finance.constants import InvoicePaymentStatus
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_finance.views_ar import CustomerReceiptView
        entity, customer, inv = self._fixture()
        u = self._super_admin("cust-receipt@test.com")
        req = APIRequestFactory().post(
            f"/v1/finance/customers/{customer.pk}/receipt/?entity={entity.code}",
            {"amount": inv.total, "payment_date": "2026-01-20", "deposit_account": "1100"},
            format="json")
        force_authenticate(req, user=u)
        resp = CustomerReceiptView.as_view()(req, pk=str(customer.pk)); resp.render()
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(json.loads(resp.content)["data"]["allocated"], inv.total)
        inv.refresh_from_db()
        self.assertEqual(inv.payment_status, InvoicePaymentStatus.PAID)

    # Verify receipt requires permission behavior.
    def test_receipt_requires_permission(self):
        from django.contrib.auth import get_user_model
        from vs_finance.models import Payment
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_finance.views_ar import CustomerReceiptView
        entity, customer, inv = self._fixture()
        u = get_user_model().objects.create_user(
            email="cust-noperm@test.com", password="x", user_type="CX_STAFF",
            status="ACTIVE", first_name="No", last_name="Perm")
        req = APIRequestFactory().post(
            f"/v1/finance/customers/{customer.pk}/receipt/?entity={entity.code}",
            {"amount": 5000, "payment_date": "2026-01-20", "deposit_account": "1100"},
            format="json")
        force_authenticate(req, user=u)
        resp = CustomerReceiptView.as_view()(req, pk=str(customer.pk)); resp.render()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 0)

    # Verify receipt allocates oldest first partially behavior.
    def test_receipt_allocates_oldest_first_partially(self):
        # Owe ₦79 (older) + ₦56; pay ₦90 → ₦79 fully + ₦11, leaving ₦45 on the 2nd.
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_finance.views_ar import CustomerReceiptView
        entity, _period, customer, _vat = self.build_ar()
        a = self.make_invoice(entity, customer, lines=[("4100", 1, 7900, None)])  # ₦79, older
        post_invoice(a)
        b = self.make_invoice(entity, customer, lines=[("4100", 1, 5600, None)])  # ₦56
        post_invoice(b)
        u = self._super_admin("cust-alloc@test.com")
        req = APIRequestFactory().post(
            f"/v1/finance/customers/{customer.pk}/receipt/?entity={entity.code}",
            {"amount": 9000, "payment_date": "2026-01-20", "deposit_account": "1100"},
            format="json")
        force_authenticate(req, user=u)
        resp = CustomerReceiptView.as_view()(req, pk=str(customer.pk)); resp.render()
        self.assertEqual(resp.status_code, 201)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.balance_due, 0)       # ₦79 fully settled
        self.assertEqual(b.balance_due, 4500)    # ₦56 − ₦11 = ₦45 remaining


# Group tests for Receipt Allocation Endpoint Tests.
class ReceiptAllocationEndpointTests(_ARFixtureMixin, TestCase):
    """Receipts list/detail and explicit (and auto) allocation to open invoices."""

    # Support the super admin workflow.
    def _super_admin(self, email):
        from django.contrib.auth import get_user_model
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment
        u = get_user_model().objects.create_user(
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",
            first_name="Rcpt", last_name="Tester")
        role, _ = PlatformRoleTemplate.objects.get_or_create(id="xvs_super_admin", defaults={"name": "Super Admin"})
        PlatformUserRoleAssignment.objects.create(user=u, role=role, assignment_status="ACTIVE")
        return u

    # Support the unallocated receipt workflow.
    def _unallocated_receipt(self, entity, customer, amount):
        import datetime
        from vs_finance.models import Account, Payment
        from vs_finance.receivables import post_payment
        p = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 20),
            method="BANK_TRANSFER", amount=amount,
            deposit_account=Account.objects.get(entity=entity, code="1100"))
        post_payment(p, auto_allocate=False)   # posts Dr bank, Cr AR — left unallocated
        return p

    # Verify list returns unallocated status behavior.
    def test_list_returns_unallocated_status(self):
        import json
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_finance.views_ar import PaymentListView
        entity, _p, customer, _v = self.build_ar()
        self._unallocated_receipt(entity, customer, 9000)
        u = self._super_admin("rcpt-list@test.com")
        req = APIRequestFactory().get("/v1/finance/payments/", {"entity": entity.code})
        force_authenticate(req, user=u)
        resp = PaymentListView.as_view()(req); resp.render()
        self.assertEqual(resp.status_code, 200)
        row = json.loads(resp.content)["data"][0]
        self.assertEqual(row["allocation_status"], "UNALLOCATED")
        self.assertEqual(row["unallocated_amount"], 9000)

    # Verify detail has open invoices and postings behavior.
    def test_detail_has_open_invoices_and_postings(self):
        import json
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_finance.views_ar import PaymentDetailView
        entity, _p, customer, _v = self.build_ar()
        a = self.make_invoice(entity, customer, lines=[("4100", 1, 7900, None)]); post_invoice(a)
        p = self._unallocated_receipt(entity, customer, 9000)
        u = self._super_admin("rcpt-detail@test.com")
        req = APIRequestFactory().get(f"/v1/finance/payments/{p.pk}/", {"entity": entity.code})
        force_authenticate(req, user=u)
        resp = PaymentDetailView.as_view()(req, pk=p.pk); resp.render()
        d = json.loads(resp.content)["data"]
        self.assertTrue(d["open_invoices"])
        self.assertTrue(d["gl_postings"])   # the receipt's Dr bank / Cr AR

    # Verify explicit allocation splits across invoices behavior.
    def test_explicit_allocation_splits_across_invoices(self):
        import json
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_finance.views_ar import PaymentAllocateView
        entity, _p, customer, _v = self.build_ar()
        a = self.make_invoice(entity, customer, lines=[("4100", 1, 7900, None)]); post_invoice(a)
        b = self.make_invoice(entity, customer, lines=[("4100", 1, 5600, None)]); post_invoice(b)
        p = self._unallocated_receipt(entity, customer, 9000)
        u = self._super_admin("rcpt-alloc@test.com")
        body = {"allocations": [{"invoice": a.id, "amount": 7900}, {"invoice": b.id, "amount": 1100}]}
        req = APIRequestFactory().post(f"/v1/finance/payments/{p.pk}/allocate/?entity={entity.code}", body, format="json")
        force_authenticate(req, user=u)
        resp = PaymentAllocateView.as_view()(req, pk=p.pk); resp.render()
        self.assertEqual(resp.status_code, 200)
        a.refresh_from_db(); b.refresh_from_db(); p.refresh_from_db()
        self.assertEqual(a.balance_due, 0)
        self.assertEqual(b.balance_due, 4500)
        self.assertEqual(p.unallocated_amount, 0)

    # Verify allocate requires permission behavior.
    def test_allocate_requires_permission(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_finance.views_ar import PaymentAllocateView
        entity, _p, customer, _v = self.build_ar()
        a = self.make_invoice(entity, customer, lines=[("4100", 1, 7900, None)]); post_invoice(a)
        p = self._unallocated_receipt(entity, customer, 9000)
        u = get_user_model().objects.create_user(
            email="rcpt-noperm@test.com", password="x", user_type="CX_STAFF",
            status="ACTIVE", first_name="No", last_name="Perm")
        req = APIRequestFactory().post(f"/v1/finance/payments/{p.pk}/allocate/?entity={entity.code}", {"auto_allocate": True}, format="json")
        force_authenticate(req, user=u)
        resp = PaymentAllocateView.as_view()(req, pk=p.pk); resp.render()
        self.assertEqual(resp.status_code, 403)
        p.refresh_from_db()
        self.assertEqual(p.unallocated_amount, 9000)


# Group tests for Dimension Analytics Tests.
class DimensionAnalyticsTests(_Phase4FixtureMixin, TestCase):
    """Analytical dimensions: constrained values, write-through to the GL, and the slice.

    Mirrors :class:`CostCenterPropagationTests` but for the second axis — the
    ``{axis: value}`` map carried on a journal line and the report that buckets by it.
    """

    # Support the axis workflow.
    def _axis(self, entity, *, code="FUND", values=("GRANT-A", "INTERNAL")):
        from vs_finance.models import Dimension
        return Dimension.objects.create(
            entity=entity, code=code, name=code.title(), allowed_values=list(values),
        )

    # Support the spend workflow.
    def _spend(self, entity, *, amount, cost_center=None, dimensions=None,
               date=datetime.date(2026, 1, 10)):
        """Post a balanced direct entry: Dr 5500 expense / Cr 1100 bank."""
        from vs_finance.posting import post_direct_entry
        return post_direct_entry(
            entity,
            lines=[
                ("5500", amount, 0, cost_center, dimensions or {}),
                ("1100", 0, amount, None, {}),
            ],
            date=date,
        )

    # --- resolver validation -------------------------------------------------
    # Verify resolve accepts allowed value behavior.
    def test_resolve_accepts_allowed_value(self):
        from vs_finance.views_ops import _resolve_dimensions
        entity, _, _ = self.build_books()
        self._axis(entity)
        self.assertEqual(
            _resolve_dimensions(entity, {"FUND": "GRANT-A"}), {"FUND": "GRANT-A"})

    # Verify resolve blank yields empty map behavior.
    def test_resolve_blank_yields_empty_map(self):
        from vs_finance.views_ops import _resolve_dimensions
        entity, _, _ = self.build_books()
        self.assertEqual(_resolve_dimensions(entity, None), {})
        self.assertEqual(_resolve_dimensions(entity, ""), {})
        self.assertEqual(_resolve_dimensions(entity, {}), {})

    # Verify resolve rejects unknown axis behavior.
    def test_resolve_rejects_unknown_axis(self):
        from rest_framework.exceptions import ValidationError
        from vs_finance.views_ops import _resolve_dimensions
        entity, _, _ = self.build_books()
        self._axis(entity)
        with self.assertRaises(ValidationError):
            _resolve_dimensions(entity, {"NOPE": "GRANT-A"})

    # Verify resolve rejects value not in allowlist behavior.
    def test_resolve_rejects_value_not_in_allowlist(self):
        from rest_framework.exceptions import ValidationError
        from vs_finance.views_ops import _resolve_dimensions
        entity, _, _ = self.build_books()
        self._axis(entity)
        with self.assertRaises(ValidationError):
            _resolve_dimensions(entity, {"FUND": "GRANT-Z"})

    # Verify resolve axis with no values rejects all behavior.
    def test_resolve_axis_with_no_values_rejects_all(self):
        from rest_framework.exceptions import ValidationError
        from vs_finance.views_ops import _resolve_dimensions
        entity, _, _ = self.build_books()
        self._axis(entity, code="EMPTY", values=())
        with self.assertRaises(ValidationError):
            _resolve_dimensions(entity, {"EMPTY": "anything"})

    # Verify resolve is tenant scoped behavior.
    def test_resolve_is_tenant_scoped(self):
        from rest_framework.exceptions import ValidationError
        from vs_finance.views_ops import _resolve_dimensions
        entity_a, _, _ = self.build_books()
        # A second tenant with its own FUND axis must not leak into entity A.
        entity_b = LedgerEntity.objects.create(
            name="Other Books", code="OBOOK", kind=LedgerEntity.Kind.TENANT)
        self._axis(entity_b)
        with self.assertRaises(ValidationError):
            _resolve_dimensions(entity_a, {"FUND": "GRANT-A"})

    # --- write-through to the GL + reversal ----------------------------------
    # Verify direct entry carries dimensions into gl and reversal behavior.
    def test_direct_entry_carries_dimensions_into_gl_and_reversal(self):
        from vs_finance.posting import reverse_journal
        from vs_finance.views_ops import _resolve_dimensions
        entity, _, _ = self.build_books()
        self._axis(entity)
        dims = _resolve_dimensions(entity, {"FUND": "GRANT-A"})
        entry = self._spend(entity, amount=100000, dimensions=dims)

        exp = entry.lines.get(account__code="5500")
        self.assertEqual(exp.dimensions, {"FUND": "GRANT-A"})

        rev = reverse_journal(entry)
        self.assertEqual(rev.lines.get(account__code="5500").dimensions, {"FUND": "GRANT-A"})

    # --- the slice report ----------------------------------------------------
    # Verify slice groups by dimension and cost centre behavior.
    def test_slice_groups_by_dimension_and_cost_centre(self):
        from vs_finance.models import CostCenter
        from vs_finance.reports import analytics_slice
        from vs_finance.views_ops import _resolve_dimensions
        entity, _, _ = self.build_books()
        self._axis(entity)
        pri = CostCenter.objects.create(entity=entity, code="PRI", name="Primary")
        self._spend(entity, amount=100000, cost_center=pri,
                    dimensions=_resolve_dimensions(entity, {"FUND": "GRANT-A"}))
        self._spend(entity, amount=40000,
                    dimensions=_resolve_dimensions(entity, {"FUND": "INTERNAL"}),
                    date=datetime.date(2026, 1, 11))

        by_fund = analytics_slice(entity, axis="FUND")
        self.assertEqual(
            {r.bucket: r.net for r in by_fund.rows if r.code == "5500"},
            {"GRANT-A": 100000, "INTERNAL": 40000})

        # Only cost-centre-tagged lines appear — the untagged 40,000 spend is excluded
        # (no "Unassigned" catch-all).
        by_cc = analytics_slice(entity, axis="cost_center")
        self.assertEqual(
            {r.bucket: r.net for r in by_cc.rows if r.code == "5500"},
            {"PRI": 100000})

    # Verify slice period scoping behavior.
    def test_slice_period_scoping(self):
        from vs_finance.reports import analytics_slice
        from vs_finance.views_ops import _resolve_dimensions
        entity, _, periods = self.build_books()
        self._axis(entity)
        self._spend(entity, amount=100000,
                    dimensions=_resolve_dimensions(entity, {"FUND": "GRANT-A"}),
                    date=datetime.date(2026, 1, 10))
        self._spend(entity, amount=40000,
                    dimensions=_resolve_dimensions(entity, {"FUND": "INTERNAL"}),
                    date=datetime.date(2026, 2, 10))

        jan = analytics_slice(entity, axis="FUND", period=periods[0])
        self.assertEqual(
            {r.bucket: r.net for r in jan.rows if r.code == "5500"}, {"GRANT-A": 100000})

    # Verify slice empty books has no rows behavior.
    def test_slice_empty_books_has_no_rows(self):
        from vs_finance.reports import analytics_slice
        entity, _, _ = self.build_books()
        self._axis(entity)
        sl = analytics_slice(entity, axis="FUND")
        self.assertEqual(sl.rows, [])
        self.assertEqual(sl.total_net, 0)


# Group tests for Dimension Analytics A P I Tests.
class DimensionAnalyticsAPITests(_Phase4FixtureMixin, TestCase):
    """The dimensions CRUD + analytics-slice REST surface."""

    # Prepare or verify the setUp test path.
    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment

        User = get_user_model()
        self.user = User.objects.create_user(
            email="dim-admin@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE", first_name="Dim", last_name="Admin",
        )
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(
            user=self.user, role=role, assignment_status="ACTIVE")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    # Verify dimension crud persists and dedupes values behavior.
    def test_dimension_crud_persists_and_dedupes_values(self):
        entity, _, _ = self.build_books()
        r = self.client.post(
            f"/v1/finance/dimensions/?entity={entity.code}",
            {"code": "FUND", "name": "Fund",
             "allowed_values": ["GRANT-A", "GRANT-A", "INTERNAL"]}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["data"]["allowed_values"], ["GRANT-A", "INTERNAL"])
        # Re-POST upserts the axis and replaces the value list.
        r2 = self.client.post(
            f"/v1/finance/dimensions/?entity={entity.code}",
            {"code": "FUND", "name": "Fund", "allowed_values": ["GRANT-B"]}, format="json")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["data"]["allowed_values"], ["GRANT-B"])

    # Verify dimension rejects blank value behavior.
    def test_dimension_rejects_blank_value(self):
        entity, _, _ = self.build_books()
        r = self.client.post(
            f"/v1/finance/dimensions/?entity={entity.code}",
            {"code": "FUND", "allowed_values": ["GRANT-A", "  "]}, format="json")
        self.assertEqual(r.status_code, 400)

    # Verify analytics slice endpoint buckets activity behavior.
    def test_analytics_slice_endpoint_buckets_activity(self):
        from vs_finance.posting import post_direct_entry
        from vs_finance.views_ops import _resolve_dimensions
        from vs_finance.models import Dimension
        entity, _, _ = self.build_books()
        Dimension.objects.create(
            entity=entity, code="FUND", name="Fund", allowed_values=["GRANT-A"])
        post_direct_entry(
            entity,
            lines=[("5500", 100000, 0, None,
                    _resolve_dimensions(entity, {"FUND": "GRANT-A"})),
                   ("1100", 0, 100000, None, {})],
            date=datetime.date(2026, 1, 10))

        resp = self.client.get(
            f"/v1/finance/reports/analytics-slice/?entity={entity.code}&axis=FUND")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["axis"], "FUND")
        exp = next(r for r in data["rows"] if r["code"] == "5500")
        self.assertEqual(exp["bucket"], "GRANT-A")
        self.assertEqual(exp["net"]["kobo"], 100000)

    # Verify analytics slice requires valid axis behavior.
    def test_analytics_slice_requires_valid_axis(self):
        entity, _, _ = self.build_books()
        missing = self.client.get(
            f"/v1/finance/reports/analytics-slice/?entity={entity.code}")
        self.assertEqual(missing.status_code, 400)
        unknown = self.client.get(
            f"/v1/finance/reports/analytics-slice/?entity={entity.code}&axis=NOPE")
        self.assertEqual(unknown.status_code, 400)

    # Verify analytics slice denied without report permission behavior.
    def test_analytics_slice_denied_without_report_permission(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_finance.views import AnalyticsSliceView
        entity, _, _ = self.build_books()
        u = get_user_model().objects.create_user(
            email="dim-noperm@test.com", password="x", user_type="CX_STAFF",
            status="ACTIVE", first_name="No", last_name="Perm")
        req = APIRequestFactory().get(
            f"/v1/finance/reports/analytics-slice/?entity={entity.code}&axis=cost_center")
        force_authenticate(req, user=u)
        resp = AnalyticsSliceView.as_view()(req); resp.render()
        self.assertEqual(resp.status_code, 403)


# Group tests for Journal Approval Workflow Tests.
class JournalApprovalWorkflowTests(_GLFixtureMixin, TestCase):
    """The journal approval slice: opt-in-by-template gating, SoD, and post-on-approve.

    Wires a manual JournalEntry into vs_workflow so that — when a template exists
    for ``finance.journal`` — GL posting happens only inside the engine's
    ``on_approved`` callback. When no template exists, direct posting is unchanged
    (regression guard). Covers the security-first cases from design §11.
    """

    APPROVE_KEY = "finance.journal.approve"

    # Prepare or verify the setUp test path.
    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from vs_rbac.models import (
            PlatformRoleTemplate, PlatformUserRoleAssignment,
            SchoolRolePermission, SchoolRoleTemplate, SchoolUserRoleAssignment,
        )

        # The approver permission key must exist for the RBAC grant FK to resolve.
        import io
        from django.core.management import call_command
        call_command("seed_finance_permissions", verbosity=0, stdout=io.StringIO())

        self.User = get_user_model()
        self.School = School
        self.SchoolRoleTemplate = SchoolRoleTemplate
        self.SchoolRolePermission = SchoolRolePermission
        self.SchoolUserRoleAssignment = SchoolUserRoleAssignment

        # A school-owned entity, so document.school resolves to a real school and the
        # engine's SCHOOL-scoped approver resolution has a pool to draw from.
        self.school = School.objects.create(name="Greenfield", slug="greenfield-jaw", code="GRNJAW")
        seed_currencies()
        self.entity = LedgerEntity.objects.create(
            name="Greenfield Books", code="GRNBK", kind=LedgerEntity.Kind.TENANT,
            source_school=self.school,
        )
        seed_chart_of_accounts(self.entity)
        self.year = FiscalYear.objects.create(
            entity=self.entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        self.period = FiscalPeriod.objects.create(
            entity=self.entity, fiscal_year=self.year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )

        # Requester: a CX super admin (bypasses the per-endpoint RBAC gate and sees
        # every entity). SoD still excludes them from approving their own journal.
        self.requester = self.User.objects.create_user(
            email="req-jaw@test.com", password="pw", user_type="CX_STAFF", status="ACTIVE",
            first_name="Reqi", last_name="Ester",
        )
        super_role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(
            user=self.requester, role=super_role, assignment_status="ACTIVE",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.requester)

    # --- fixtures ---------------------------------------------------------- #

    # Support the make draft workflow.
    def _make_draft(self, *, debit=50000, period=None):
        """A balanced two-line DRAFT journal (Dr cash / Cr revenue)."""
        entry = JournalEntry.objects.create(
            entity=self.entity, date=datetime.date(2026, 1, 15),
            period=period or self.period, narration="approval test",
            created_by=self.requester,
        )
        cash = Account.objects.get(entity=self.entity, code="1100")
        rev = Account.objects.get(entity=self.entity, code="4100")
        JournalLine.objects.create(entry=entry, account=cash, debit=debit, credit=0, line_no=1)
        JournalLine.objects.create(entry=entry, account=rev, debit=0, credit=debit, line_no=2)
        return entry

    # Support the publish standard template workflow.
    def _publish_standard_template(self):
        from vs_workflow.services.templates import publish_template

        return publish_template(
            school=self.school, branch=None,
            document_type="finance.journal", code="standard",
            name="Standard journal approval",
            stages_payload=[{
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",
                "order": 1, "approver_permission_key": self.APPROVE_KEY,
                "approver_scope": "SCHOOL", "advance_rule": "ANY",
                "on_rejection": "RETURN_TO_REQUESTER", "skip_if_no_approvers": False,
            }],
        )

    # Support the make approver workflow.
    def _make_approver(self, email="apr-jaw@test.com"):
        """A school user holding finance.journal.approve at self.school."""
        user = self.User.objects.create_user(
            email=email, password="pw", user_type="SCHOOL_ADMIN", status="ACTIVE",
            first_name="Apro", last_name="Ver", school=self.school,
        )
        role, _ = self.SchoolRoleTemplate.objects.get_or_create(
            id="checker-role", defaults={"school": self.school, "name": "Journal Checker"},
        )
        self.SchoolRolePermission.objects.get_or_create(
            role=role, permission_id=self.APPROVE_KEY, defaults={"granted": True},
        )
        self.SchoolUserRoleAssignment.objects.create(
            school=self.school, user=user, role=role, assignment_status="ACTIVE",
        )
        return user

    # Support the submit workflow.
    def _submit(self, entry):
        return self.client.post(
            f"/v1/finance/journals/{entry.id}/submit/?entity={self.entity.code}", {}, format="json")

    # Support the post workflow.
    def _post(self, entry):
        return self.client.post(
            f"/v1/finance/journals/{entry.id}/post/?entity={self.entity.code}", {}, format="json")

    # Support the instance for workflow.
    def _instance_for(self, entry):
        from vs_workflow.models import WorkflowInstance
        return WorkflowInstance.objects.for_document(entry).first()

    # --- 1. Gate off: no template → direct post still works ---------------- #

    # Verify gate off direct post still works behavior.
    def test_gate_off_direct_post_still_works(self):
        from vs_finance.approvals import approval_required

        entry = self._make_draft()
        self.assertFalse(approval_required(entry))
        resp = self._post(entry)
        self.assertEqual(resp.status_code, 200, resp.content)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.POSTED)
        self.assertTrue(
            AccountBalance.objects.filter(
                account__entity=self.entity, period=self.period).exists())

    # --- 2. Gate on: template → direct post refused, submit → PENDING, GL untouched --- #

    # Verify gate on direct post refused behavior.
    def test_gate_on_direct_post_refused(self):
        self._publish_standard_template()
        entry = self._make_draft()
        resp = self._post(entry)
        self.assertEqual(resp.status_code, 400, resp.content)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.DRAFT)

    # Verify gate on submit moves to pending and leaves gl untouched behavior.
    def test_gate_on_submit_moves_to_pending_and_leaves_gl_untouched(self):
        self._publish_standard_template()
        self._make_approver()  # keep the stage ACTIVE (do not auto-skip)
        entry = self._make_draft()
        resp = self._submit(entry)
        self.assertEqual(resp.status_code, 200, resp.content)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.PENDING_APPROVAL)
        # GL is untouched: no POSTED status, no balance movement.
        self.assertFalse(JournalEntry.objects.filter(
            pk=entry.pk, status=DocumentStatus.POSTED).exists())
        self.assertFalse(AccountBalance.objects.filter(
            account__entity=self.entity, period=self.period).exists())

    # --- 3. SoD: requester cannot approve their own journal ---------------- #

    # Verify requester cannot approve own journal behavior.
    def test_requester_cannot_approve_own_journal(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.exceptions import (
            NotAnEligibleApproverError, RequesterCannotApproveError,
        )

        self._publish_standard_template()
        self._make_approver()
        entry = self._make_draft()
        self._submit(entry)
        instance = self._instance_for(entry)
        # The requester is never on the eligible snapshot and is hard-blocked either
        # way — both are correct SoD outcomes.
        with self.assertRaises((RequesterCannotApproveError, NotAnEligibleApproverError)):
            wf_actions.record_action(instance.id, self.requester, ActionEnum.APPROVED)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.PENDING_APPROVAL)

    # --- 4. Happy path: a different approver approves → posts, posted_by == approver --- #

    # Verify approval posts and stamps approver as poster behavior.
    def test_approval_posts_and_stamps_approver_as_poster(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._publish_standard_template()
        approver = self._make_approver()
        entry = self._make_draft()
        self._submit(entry)
        instance = self._instance_for(entry)

        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)

        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.POSTED)
        self.assertEqual(entry.posted_by_id, approver.id)         # Q2: poster == final approver
        self.assertEqual(entry.created_by_id, self.requester.id)  # Q2: maker unchanged
        self.assertTrue(AccountBalance.objects.filter(
            account__entity=self.entity, period=self.period).exists())

    # --- 5. Reject → DRAFT and Return → DRAFT ------------------------------ #

    # Verify reject returns journal to draft behavior.
    def test_reject_returns_journal_to_draft(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        # A TERMINAL-on-rejection template so REJECTED ends the instance.
        from vs_workflow.services.templates import publish_template
        publish_template(
            school=self.school, branch=None,
            document_type="finance.journal", code="standard",
            name="Standard journal approval",
            stages_payload=[{
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",
                "order": 1, "approver_permission_key": self.APPROVE_KEY,
                "approver_scope": "SCHOOL", "advance_rule": "ANY",
                "on_rejection": "TERMINAL", "skip_if_no_approvers": False,
            }])
        approver = self._make_approver()
        entry = self._make_draft()
        self._submit(entry)
        instance = self._instance_for(entry)

        wf_actions.record_action(instance.id, approver, ActionEnum.REJECTED, comment="no")

        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.DRAFT)
        self.assertFalse(AccountBalance.objects.filter(
            account__entity=self.entity, period=self.period).exists())

    # Verify return sends journal back to draft behavior.
    def test_return_sends_journal_back_to_draft(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._publish_standard_template()  # on_rejection=RETURN_TO_REQUESTER
        approver = self._make_approver()
        entry = self._make_draft()
        self._submit(entry)
        instance = self._instance_for(entry)

        wf_actions.record_action(instance.id, approver, ActionEnum.RETURNED, comment="fix narration")

        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.DRAFT)

    # --- 6. Posting failure at approval time (Option A rollback) ----------- #

    # Verify posting failure at approval rolls back and keeps stage active behavior.
    def test_posting_failure_at_approval_rolls_back_and_keeps_stage_active(self):
        from vs_workflow.constants import WorkflowInstanceStatus, WorkflowStageStatus
        from vs_finance.exceptions import PeriodClosedError
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.models import WorkflowStageAction

        self._publish_standard_template()
        approver = self._make_approver()
        entry = self._make_draft()
        self._submit(entry)  # preflight passes while the period is OPEN
        instance = self._instance_for(entry)

        # The period closes while the journal sits in the queue → posting must fail.
        self.period.status = PeriodStatus.CLOSED
        self.period.save(update_fields=["status"])

        with self.assertRaises(PeriodClosedError):
            wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)

        # Option A: the approval action rolled back — journal not POSTED, and the
        # stage is still ACTIVE for a retry once the period reopens.
        entry.refresh_from_db()
        self.assertNotEqual(entry.status, DocumentStatus.POSTED)
        self.assertFalse(AccountBalance.objects.filter(
            account__entity=self.entity, period=self.period).exists())
        self.assertFalse(WorkflowStageAction.objects.filter(
            stage_instance__instance=instance, action=ActionEnum.APPROVED,
            reversed_at__isnull=True, is_reversal_of__isnull=True).exists())
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertTrue(
            instance.stage_instances.filter(status=WorkflowStageStatus.ACTIVE).exists())

        # Retry succeeds once the period reopens.
        self.period.status = PeriodStatus.OPEN
        self.period.save(update_fields=["status"])
        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.POSTED)
        self.assertEqual(entry.posted_by_id, approver.id)


# Group tests for Refund Approval Workflow Tests.
class RefundApprovalWorkflowTests(_ARFixtureMixin, TestCase):
    """The refund approval slice: opt-in-by-template gating, SoD, and payout-on-approve.

    Wires a customer :class:`Refund` into vs_workflow so that — when a template
    exists for ``finance.refund`` — the cash payout happens only inside the engine's
    ``on_approved`` callback (``credit_notes.post_refund``). With no template, direct
    posting is unchanged. Reuses the same RBAC/user/template fixture shape as the
    journal slice; a refund needs a customer holding available credit, seated here by
    posting a standalone over-payment (books to customer-credit 2140).
    """

    APPROVE_KEY = "finance.refund.approve"

    # Prepare or verify the setUp test path.
    def setUp(self):
        import io
        from django.contrib.auth import get_user_model
        from django.core.management import call_command
        from rest_framework.test import APIClient
        from vs_rbac.models import (
            PlatformRoleTemplate, PlatformUserRoleAssignment,
            SchoolRolePermission, SchoolRoleTemplate, SchoolUserRoleAssignment,
        )

        # The approver permission key must exist for the RBAC grant FK to resolve.
        call_command("seed_finance_permissions", verbosity=0, stdout=io.StringIO())

        self.User = get_user_model()
        self.SchoolRoleTemplate = SchoolRoleTemplate
        self.SchoolRolePermission = SchoolRolePermission
        self.SchoolUserRoleAssignment = SchoolUserRoleAssignment

        # A school-owned entity, so refund.school resolves to a real school and the
        # engine's SCHOOL-scoped approver resolution has a pool to draw from.
        self.school = School.objects.create(name="Riverside", slug="riverside-raw", code="RVRAW")
        seed_currencies()
        self.entity = LedgerEntity.objects.create(
            name="Riverside Books", code="RVRBK", kind=LedgerEntity.Kind.TENANT,
            source_school=self.school,
        )
        seed_chart_of_accounts(self.entity)
        self.year = FiscalYear.objects.create(
            entity=self.entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        self.period = FiscalPeriod.objects.create(
            entity=self.entity, fiscal_year=self.year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )
        self.bank = Account.objects.get(entity=self.entity, code="1100")
        self.customer = Customer.objects.create(
            entity=self.entity, code="CUSTR", name="Payer Ltd",
            receivable_account=Account.objects.get(entity=self.entity, code="1200"),
        )

        # Requester: a CX super admin (bypasses the per-endpoint RBAC gate, sees every
        # entity). SoD still excludes them from approving their own refund.
        self.requester = self.User.objects.create_user(
            email="req-raw@test.com", password="pw", user_type="CX_STAFF", status="ACTIVE",
            first_name="Reqi", last_name="Ester",
        )
        super_role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(
            user=self.requester, role=super_role, assignment_status="ACTIVE",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.requester)

    # --- fixtures ---------------------------------------------------------- #

    # Support the seat credit workflow.
    def _seat_credit(self, amount):
        """Seat ``amount`` kobo of available customer credit via a standalone payment.

        A receipt with no open invoices books its whole amount to the customer-credit
        liability (2140) — exactly what a refund pays back out.
        """
        pay = Payment.objects.create(
            entity=self.entity, customer=self.customer,
            payment_date=datetime.date(2026, 1, 5), amount=amount, deposit_account=self.bank,
        )
        post_payment(pay)
        self.assertEqual(customer_credit_balance(self.customer), amount)

    # Support the make draft refund workflow.
    def _make_draft_refund(self, *, amount=30000):
        return Refund.objects.create(
            entity=self.entity, customer=self.customer,
            refund_date=datetime.date(2026, 1, 18), amount=amount,
            deposit_account=self.bank, created_by=self.requester,
        )

    # Support the publish standard template workflow.
    def _publish_standard_template(self, *, on_rejection="RETURN_TO_REQUESTER"):
        from vs_workflow.services.templates import publish_template

        return publish_template(
            school=self.school, branch=None,
            document_type="finance.refund", code="standard",
            name="Standard refund approval",
            stages_payload=[{
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",
                "order": 1, "approver_permission_key": self.APPROVE_KEY,
                "approver_scope": "SCHOOL", "advance_rule": "ANY",
                "on_rejection": on_rejection, "skip_if_no_approvers": False,
            }])

    # Support the make approver workflow.
    def _make_approver(self, email="apr-raw@test.com"):
        user = self.User.objects.create_user(
            email=email, password="pw", user_type="SCHOOL_ADMIN", status="ACTIVE",
            first_name="Apro", last_name="Ver", school=self.school,
        )
        role, _ = self.SchoolRoleTemplate.objects.get_or_create(
            id="refund-checker-role", defaults={"school": self.school, "name": "Refund Checker"},
        )
        self.SchoolRolePermission.objects.get_or_create(
            role=role, permission_id=self.APPROVE_KEY, defaults={"granted": True},
        )
        self.SchoolUserRoleAssignment.objects.create(
            school=self.school, user=user, role=role, assignment_status="ACTIVE",
        )
        return user

    # Support the submit workflow.
    def _submit(self, refund):
        return self.client.post(
            f"/v1/finance/refunds/{refund.pk}/submit/?entity={self.entity.code}", {}, format="json")

    # Support the post workflow.
    def _post(self, refund):
        return self.client.post(
            f"/v1/finance/refunds/{refund.pk}/post/?entity={self.entity.code}", {}, format="json")

    # Support the instance for workflow.
    def _instance_for(self, refund):
        from vs_workflow.models import WorkflowInstance
        return WorkflowInstance.objects.for_document(refund).first()

    # --- 1. Gate off: no template → direct post still works ---------------- #

    # Verify gate off direct post still works behavior.
    def test_gate_off_direct_post_still_works(self):
        from vs_finance.approvals import approval_required

        self._seat_credit(30000)
        refund = self._make_draft_refund(amount=30000)
        self.assertFalse(approval_required(refund))
        resp = self._post(refund)
        self.assertEqual(resp.status_code, 200, resp.content)
        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.POSTED)
        self.assertIsNotNone(refund.journal_id)

    # --- 2. Gate on: direct post refused ----------------------------------- #

    # Verify gate on direct post refused behavior.
    def test_gate_on_direct_post_refused(self):
        self._seat_credit(30000)
        self._publish_standard_template()
        refund = self._make_draft_refund(amount=30000)
        resp = self._post(refund)
        self.assertEqual(resp.status_code, 400, resp.content)
        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.DRAFT)
        self.assertIsNone(refund.journal_id)

    # --- 3. Gate on: submit → PENDING, no refund journal posted ------------ #

    # Verify gate on submit moves to pending and no payout behavior.
    def test_gate_on_submit_moves_to_pending_and_no_payout(self):
        self._seat_credit(30000)
        self._publish_standard_template()
        self._make_approver()  # keep the stage ACTIVE (do not auto-skip)
        refund = self._make_draft_refund(amount=30000)
        resp = self._submit(refund)
        self.assertEqual(resp.status_code, 200, resp.content)
        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.PENDING_APPROVAL)
        self.assertIsNone(refund.journal_id)
        # The credit is untouched until approval.
        self.assertEqual(customer_credit_balance(self.customer), 30000)

    # --- 4. SoD: requester cannot approve own refund ----------------------- #

    # Verify requester cannot approve own refund behavior.
    def test_requester_cannot_approve_own_refund(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.exceptions import (
            NotAnEligibleApproverError, RequesterCannotApproveError,
        )

        self._seat_credit(30000)
        self._publish_standard_template()
        self._make_approver()
        refund = self._make_draft_refund(amount=30000)
        self._submit(refund)
        instance = self._instance_for(refund)
        with self.assertRaises((RequesterCannotApproveError, NotAnEligibleApproverError)):
            wf_actions.record_action(instance.id, self.requester, ActionEnum.APPROVED)
        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.PENDING_APPROVAL)

    # --- 5. Happy path: approver approves → post_refund runs, refund POSTED --- #

    # Verify approval pays out and posts refund behavior.
    def test_approval_pays_out_and_posts_refund(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._seat_credit(30000)
        self._publish_standard_template()
        approver = self._make_approver()
        refund = self._make_draft_refund(amount=30000)
        self._submit(refund)
        instance = self._instance_for(refund)

        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)

        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.POSTED)
        self.assertIsNotNone(refund.journal_id)                   # payout journal linked
        self.assertEqual(refund.journal.posted_by_id, approver.id)  # posted by the approver
        self.assertEqual(customer_credit_balance(self.customer), 0)  # credit paid out

    # --- 6. Reject → DRAFT and Return → DRAFT ------------------------------ #

    # Verify reject returns refund to draft behavior.
    def test_reject_returns_refund_to_draft(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._seat_credit(30000)
        self._publish_standard_template(on_rejection="TERMINAL")
        approver = self._make_approver()
        refund = self._make_draft_refund(amount=30000)
        self._submit(refund)
        instance = self._instance_for(refund)

        wf_actions.record_action(instance.id, approver, ActionEnum.REJECTED, comment="no")

        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.DRAFT)
        self.assertIsNone(refund.journal_id)
        self.assertEqual(customer_credit_balance(self.customer), 30000)

    # Verify return sends refund back to draft behavior.
    def test_return_sends_refund_back_to_draft(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._seat_credit(30000)
        self._publish_standard_template()  # on_rejection=RETURN_TO_REQUESTER
        approver = self._make_approver()
        refund = self._make_draft_refund(amount=30000)
        self._submit(refund)
        instance = self._instance_for(refund)

        wf_actions.record_action(instance.id, approver, ActionEnum.RETURNED, comment="wrong account")

        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.DRAFT)

    # --- 7. Option-A rollback: credit drained after submit ----------------- #

    # Verify posting failure at approval rolls back and keeps stage active behavior.
    def test_posting_failure_at_approval_rolls_back_and_keeps_stage_active(self):
        from vs_workflow.constants import (
            WorkflowInstanceStatus, WorkflowStageAction as ActionEnum, WorkflowStageStatus,
        )
        from vs_finance.exceptions import PostingError
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.models import WorkflowStageAction

        # Seat exactly enough credit for one refund; submit passes preflight.
        self._seat_credit(30000)
        self._publish_standard_template()
        approver = self._make_approver()
        refund = self._make_draft_refund(amount=30000)
        self._submit(refund)
        instance = self._instance_for(refund)

        # Drain the customer's available credit while the refund sits in the queue by
        # paying it out through a second, directly-posted refund (no template gate on
        # that path yet — it's the same entity, but we bypass via the service). After
        # this, post_refund on the queued refund must exceed available credit.
        drain = Refund.objects.create(
            entity=self.entity, customer=self.customer, refund_date=datetime.date(2026, 1, 6),
            amount=30000, deposit_account=self.bank, created_by=self.requester,
        )
        from vs_finance.credit_notes import post_refund
        post_refund(drain, actor_user=self.requester)
        self.assertEqual(customer_credit_balance(self.customer), 0)

        with self.assertRaises(PostingError):
            wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)

        # Option A: the approval action rolled back — refund not POSTED, no journal,
        # and the stage is still ACTIVE for a retry.
        refund.refresh_from_db()
        self.assertNotEqual(refund.status, DocumentStatus.POSTED)
        self.assertIsNone(refund.journal_id)
        self.assertFalse(WorkflowStageAction.objects.filter(
            stage_instance__instance=instance, action=ActionEnum.APPROVED,
            reversed_at__isnull=True, is_reversal_of__isnull=True).exists())
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertTrue(
            instance.stage_instances.filter(status=WorkflowStageStatus.ACTIVE).exists())

        # Retry succeeds once credit is re-seated.
        self._seat_credit(30000)
        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)
        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.POSTED)
        self.assertEqual(refund.journal.posted_by_id, approver.id)


# Group tests for Write Off Request Approval Workflow Tests.
class WriteOffRequestApprovalWorkflowTests(_ARFixtureMixin, TestCase):
    """The bad-debt write-off approval slice: opt-in gating, SoD, write-off-on-approve.

    Wires the first-class :class:`WriteOffRequest` document into vs_workflow so that —
    when a template exists for ``finance.write_off`` — the invoice write-off happens
    only inside ``on_approved`` (``credit_notes.write_off_invoice``, unchanged). With
    no template, the direct-post path (and the invoice-write-off bridge) is unchanged.
    Reuses the same RBAC/user/template fixture shape as the refund slice; needs a
    POSTED invoice with an outstanding balance.
    """

    APPROVE_KEY = "finance.writeoff.approve"

    # Prepare or verify the setUp test path.
    def setUp(self):
        import io
        from django.contrib.auth import get_user_model
        from django.core.management import call_command
        from rest_framework.test import APIClient
        from vs_rbac.models import (
            PlatformRoleTemplate, PlatformUserRoleAssignment,
            SchoolRolePermission, SchoolRoleTemplate, SchoolUserRoleAssignment,
        )

        call_command("seed_finance_permissions", verbosity=0, stdout=io.StringIO())

        self.User = get_user_model()
        self.SchoolRoleTemplate = SchoolRoleTemplate
        self.SchoolRolePermission = SchoolRolePermission
        self.SchoolUserRoleAssignment = SchoolUserRoleAssignment

        # School-owned entity, so write_off_request.school resolves to a real school.
        self.school = School.objects.create(name="Lakeside", slug="lakeside-woa", code="LKSWO")
        seed_currencies()
        self.entity = LedgerEntity.objects.create(
            name="Lakeside Books", code="LKSBK", kind=LedgerEntity.Kind.TENANT,
            source_school=self.school,
        )
        seed_chart_of_accounts(self.entity)
        self.year = FiscalYear.objects.create(
            entity=self.entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        self.period = FiscalPeriod.objects.create(
            entity=self.entity, fiscal_year=self.year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )
        self.customer = Customer.objects.create(
            entity=self.entity, code="CUSTW", name="Debtor Ltd",
            receivable_account=Account.objects.get(entity=self.entity, code="1200"),
        )

        self.requester = self.User.objects.create_user(
            email="req-woa@test.com", password="pw", user_type="CX_STAFF", status="ACTIVE",
            first_name="Reqi", last_name="Ester",
        )
        super_role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(
            user=self.requester, role=super_role, assignment_status="ACTIVE",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.requester)

    # --- fixtures ---------------------------------------------------------- #

    # Support the posted invoice workflow.
    def _posted_invoice(self, *, unit_price=100000):
        """A POSTED invoice with a full outstanding balance (no tax, unpaid)."""
        inv = Invoice.objects.create(
            entity=self.entity, customer=self.customer,
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 25),
        )
        InvoiceLine.objects.create(
            invoice=inv, revenue_account=Account.objects.get(entity=self.entity, code="4100"),
            quantity=1, unit_price=unit_price, tax_code=None, line_no=1,
        )
        post_invoice(inv)
        inv.refresh_from_db()
        return inv

    # Support the make request workflow.
    def _make_request(self, invoice, *, amount=None):
        from vs_finance.models import WriteOffRequest
        return WriteOffRequest.objects.create(
            entity=self.entity, invoice=invoice,
            amount=amount if amount is not None else invoice.balance_due,
            write_off_date=datetime.date(2026, 1, 20), reason="uncollectable",
            created_by=self.requester,
        )

    # Support the publish standard template workflow.
    def _publish_standard_template(self, *, on_rejection="RETURN_TO_REQUESTER"):
        from vs_workflow.services.templates import publish_template

        return publish_template(
            school=self.school, branch=None,
            document_type="finance.write_off", code="standard",
            name="Standard write-off approval",
            stages_payload=[{
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",
                "order": 1, "approver_permission_key": self.APPROVE_KEY,
                "approver_scope": "SCHOOL", "advance_rule": "ANY",
                "on_rejection": on_rejection, "skip_if_no_approvers": False,
            }])

    # Support the make approver workflow.
    def _make_approver(self, email="apr-woa@test.com"):
        user = self.User.objects.create_user(
            email=email, password="pw", user_type="SCHOOL_ADMIN", status="ACTIVE",
            first_name="Apro", last_name="Ver", school=self.school,
        )
        role, _ = self.SchoolRoleTemplate.objects.get_or_create(
            id="writeoff-checker-role", defaults={"school": self.school, "name": "Write-off Checker"},
        )
        self.SchoolRolePermission.objects.get_or_create(
            role=role, permission_id=self.APPROVE_KEY, defaults={"granted": True},
        )
        self.SchoolUserRoleAssignment.objects.create(
            school=self.school, user=user, role=role, assignment_status="ACTIVE",
        )
        return user

    # Support the submit workflow.
    def _submit(self, wor):
        return self.client.post(
            f"/v1/finance/write-offs/{wor.pk}/submit/?entity={self.entity.code}", {}, format="json")

    # Support the post workflow.
    def _post(self, wor):
        return self.client.post(
            f"/v1/finance/write-offs/{wor.pk}/post/?entity={self.entity.code}", {}, format="json")

    # Support the instance for workflow.
    def _instance_for(self, wor):
        from vs_workflow.models import WorkflowInstance
        return WorkflowInstance.objects.for_document(wor).first()

    # --- 1. Gate off: direct post writes the invoice off ------------------- #

    # Verify gate off direct post writes off behavior.
    def test_gate_off_direct_post_writes_off(self):
        from vs_finance.approvals import approval_required

        inv = self._posted_invoice()
        wor = self._make_request(inv)
        self.assertFalse(approval_required(wor))
        resp = self._post(wor)
        self.assertEqual(resp.status_code, 200, resp.content)
        wor.refresh_from_db(); inv.refresh_from_db()
        self.assertEqual(wor.status, DocumentStatus.POSTED)
        self.assertIsNotNone(wor.journal_id)
        self.assertEqual(inv.amount_credited, 100000)
        self.assertEqual(inv.balance_due, 0)

    # --- 2. Gate on: direct post refused ----------------------------------- #

    # Verify gate on direct post refused behavior.
    def test_gate_on_direct_post_refused(self):
        self._publish_standard_template()
        inv = self._posted_invoice()
        wor = self._make_request(inv)
        resp = self._post(wor)
        self.assertEqual(resp.status_code, 400, resp.content)
        wor.refresh_from_db(); inv.refresh_from_db()
        self.assertEqual(wor.status, DocumentStatus.DRAFT)
        self.assertIsNone(wor.journal_id)
        self.assertEqual(inv.amount_credited, 0)

    # --- 3. Gate on: submit → PENDING, invoice untouched ------------------- #

    # Verify gate on submit moves to pending and invoice untouched behavior.
    def test_gate_on_submit_moves_to_pending_and_invoice_untouched(self):
        self._publish_standard_template()
        self._make_approver()
        inv = self._posted_invoice()
        wor = self._make_request(inv)
        resp = self._submit(wor)
        self.assertEqual(resp.status_code, 200, resp.content)
        wor.refresh_from_db(); inv.refresh_from_db()
        self.assertEqual(wor.status, DocumentStatus.PENDING_APPROVAL)
        self.assertIsNone(wor.journal_id)
        self.assertEqual(inv.balance_due, 100000)

    # --- 4. SoD: requester cannot approve own request ---------------------- #

    # Verify requester cannot approve own request behavior.
    def test_requester_cannot_approve_own_request(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.exceptions import (
            NotAnEligibleApproverError, RequesterCannotApproveError,
        )

        self._publish_standard_template()
        self._make_approver()
        inv = self._posted_invoice()
        wor = self._make_request(inv)
        self._submit(wor)
        instance = self._instance_for(wor)
        with self.assertRaises((RequesterCannotApproveError, NotAnEligibleApproverError)):
            wf_actions.record_action(instance.id, self.requester, ActionEnum.APPROVED)
        wor.refresh_from_db()
        self.assertEqual(wor.status, DocumentStatus.PENDING_APPROVAL)

    # --- 5. Happy path: approver approves → invoice written off ------------ #

    # Verify approval writes off and posts request behavior.
    def test_approval_writes_off_and_posts_request(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._publish_standard_template()
        approver = self._make_approver()
        inv = self._posted_invoice()
        wor = self._make_request(inv)
        self._submit(wor)
        instance = self._instance_for(wor)

        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)

        wor.refresh_from_db(); inv.refresh_from_db()
        self.assertEqual(wor.status, DocumentStatus.POSTED)
        self.assertIsNotNone(wor.journal_id)
        self.assertEqual(wor.journal.posted_by_id, approver.id)
        self.assertEqual(inv.amount_credited, 100000)
        self.assertEqual(inv.balance_due, 0)

    # --- 6. Reject → DRAFT and Return → DRAFT ------------------------------ #

    # Verify reject returns request to draft behavior.
    def test_reject_returns_request_to_draft(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._publish_standard_template(on_rejection="TERMINAL")
        approver = self._make_approver()
        inv = self._posted_invoice()
        wor = self._make_request(inv)
        self._submit(wor)
        instance = self._instance_for(wor)

        wf_actions.record_action(instance.id, approver, ActionEnum.REJECTED, comment="no")

        wor.refresh_from_db(); inv.refresh_from_db()
        self.assertEqual(wor.status, DocumentStatus.DRAFT)
        self.assertIsNone(wor.journal_id)
        self.assertEqual(inv.balance_due, 100000)

    # Verify return sends request back to draft behavior.
    def test_return_sends_request_back_to_draft(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._publish_standard_template()  # RETURN_TO_REQUESTER
        approver = self._make_approver()
        inv = self._posted_invoice()
        wor = self._make_request(inv)
        self._submit(wor)
        instance = self._instance_for(wor)

        wf_actions.record_action(instance.id, approver, ActionEnum.RETURNED, comment="wrong amount")

        wor.refresh_from_db(); inv.refresh_from_db()
        self.assertEqual(wor.status, DocumentStatus.DRAFT)
        self.assertEqual(inv.balance_due, 100000)

    # --- 7. Option-A rollback: invoice settled after submit ---------------- #

    # Verify posting failure at approval rolls back and keeps stage active behavior.
    def test_posting_failure_at_approval_rolls_back_and_keeps_stage_active(self):
        from vs_workflow.constants import (
            WorkflowInstanceStatus, WorkflowStageAction as ActionEnum, WorkflowStageStatus,
        )
        from vs_finance.exceptions import PostingError
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.models import WorkflowStageAction

        self._publish_standard_template()
        approver = self._make_approver()
        inv = self._posted_invoice()
        wor = self._make_request(inv)
        self._submit(wor)  # preflight passes while the balance is outstanding
        instance = self._instance_for(wor)

        # Settle the invoice in full while the request sits in the queue, so
        # write_off_invoice raises "no outstanding balance" at approval.
        bank = Account.objects.get(entity=self.entity, code="1100")
        pay = Payment.objects.create(
            entity=self.entity, customer=self.customer,
            payment_date=datetime.date(2026, 1, 15), amount=100000, deposit_account=bank,
        )
        post_payment(pay)
        inv.refresh_from_db()
        self.assertEqual(inv.balance_due, 0)

        with self.assertRaises(PostingError):
            wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)

        # Option A: the approval action rolled back — request not POSTED, no journal,
        # invoice untouched by any write-off, stage still ACTIVE for a retry.
        wor.refresh_from_db()
        self.assertNotEqual(wor.status, DocumentStatus.POSTED)
        self.assertIsNone(wor.journal_id)
        self.assertFalse(WorkflowStageAction.objects.filter(
            stage_instance__instance=instance, action=ActionEnum.APPROVED,
            reversed_at__isnull=True, is_reversal_of__isnull=True).exists())
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertTrue(
            instance.stage_instances.filter(status=WorkflowStageStatus.ACTIVE).exists())

    # --- 8. Backward-compat bridge on /invoices/<id>/write-off/ ------------ #

    # Verify invoice write off bridge submits when gated behavior.
    def test_invoice_write_off_bridge_submits_when_gated(self):
        from vs_finance.models import WriteOffRequest

        self._publish_standard_template()
        self._make_approver()
        inv = self._posted_invoice()
        resp = self.client.post(
            f"/v1/finance/invoices/{inv.pk}/write-off/?entity={self.entity.code}", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        # A request was created and submitted; the invoice is NOT yet written off.
        wor = WriteOffRequest.objects.get(invoice=inv)
        self.assertEqual(wor.status, DocumentStatus.PENDING_APPROVAL)
        inv.refresh_from_db()
        self.assertEqual(inv.balance_due, 100000)

    # Verify invoice write off bridge posts directly when ungated behavior.
    def test_invoice_write_off_bridge_posts_directly_when_ungated(self):
        from vs_finance.models import WriteOffRequest

        inv = self._posted_invoice()
        resp = self.client.post(
            f"/v1/finance/invoices/{inv.pk}/write-off/?entity={self.entity.code}", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        # No template → posts directly; the invoice is written off as before.
        wor = WriteOffRequest.objects.get(invoice=inv)
        self.assertEqual(wor.status, DocumentStatus.POSTED)
        self.assertIsNotNone(wor.journal_id)
        inv.refresh_from_db()
        self.assertEqual(inv.amount_credited, 100000)
        self.assertEqual(inv.balance_due, 0)


# Group tests for Dunning Notification Tests.
class DunningNotificationTests(_GLFixtureMixin, TestCase):
    """Dunning delivery routed entirely through vs_notifications.

    Proves that generating + sending a dunning notice creates a
    ``vs_notifications.Notification`` record (delivery goes through the notification
    system, never from vs_finance directly), that the policy stage's message carries
    the escalation wording, and that the daily scheduler + graceful-degradation paths
    behave. Notifications are school-scoped, so these use a school-owned entity.
    """

    # Prepare or verify the setUp test path.
    def setUp(self):
        from vs_notifications.services.seed import (
            seed_event_types, seed_notification_templates, seed_school_settings,
        )

        # Seed the notification event types + default templates (fresh test DB, so the
        # get_or_create seed picks up the extended overdue template), then the school's
        # channel settings.
        seed_event_types()
        seed_notification_templates()

        self.school = School.objects.create(name="Maplewood", slug="maplewood-dnt", code="MPLDN")
        seed_school_settings(self.school)

        seed_currencies()
        self.entity = LedgerEntity.objects.create(
            name="Maplewood Books", code="MPLBK", kind=LedgerEntity.Kind.TENANT,
            source_school=self.school,
        )
        seed_chart_of_accounts(self.entity)
        self.year = FiscalYear.objects.create(
            entity=self.entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        self.period = FiscalPeriod.objects.create(
            entity=self.entity, fiscal_year=self.year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )
        self.customer = Customer.objects.create(
            entity=self.entity, code="CUSTD", name="Debtor Ltd",
            receivable_account=Account.objects.get(entity=self.entity, code="1200"),
            billing_email="debtor@example.com",
        )

    # --- helpers ----------------------------------------------------------- #

    # Support the overdue invoice workflow.
    def _overdue_invoice(self, *, unit_price=100000, due=datetime.date(2026, 1, 10)):
        inv = Invoice.objects.create(
            entity=self.entity, customer=self.customer,
            invoice_date=datetime.date(2026, 1, 1), due_date=due,
        )
        InvoiceLine.objects.create(
            invoice=inv, revenue_account=Account.objects.get(entity=self.entity, code="4100"),
            quantity=1, unit_price=unit_price, tax_code=None, line_no=1,
        )
        post_invoice(inv)
        inv.refresh_from_db()
        return inv

    # Support the generate one workflow.
    def _generate_one(self, *, as_of=datetime.date(2026, 2, 15)):
        ensure_default_policy(self.entity)
        self._overdue_invoice()
        notices = generate_dunning(self.entity, as_of=as_of)
        self.assertEqual(len(notices), 1)
        return notices[0]

    # --- 1. delivery goes through vs_notifications ------------------------- #

    # Verify mark sent creates email notification and flips sent behavior.
    def test_mark_sent_creates_email_notification_and_flips_sent(self):
        from vs_notifications.models import Notification
        from vs_notifications.constants import ChannelChoices

        notice = self._generate_one()
        mark_notice_sent(notice)

        notice.refresh_from_db()
        self.assertEqual(notice.notice_status, "SENT")
        self.assertIsNotNone(notice.sent_at)

        email = Notification.objects.filter(
            school=self.school, channel=ChannelChoices.EMAIL,
            unregistered_email="debtor@example.com",
        )
        self.assertTrue(email.exists(), "an EMAIL notification should be created for the customer")

    # --- 2. escalation wording comes from the policy ---------------------- #

    # Verify email body contains policy reminder message behavior.
    def test_email_body_contains_policy_reminder_message(self):
        from vs_notifications.models import Notification
        from vs_notifications.constants import ChannelChoices

        notice = self._generate_one()
        # The generated notice snapshots the stage message (the policy's wording).
        self.assertTrue(notice.message)
        mark_notice_sent(notice)

        # Scope to the overdue event: post_invoice also fires an invoice_issued EMAIL
        # to the same customer, so filter by event key to get the dunning notice.
        email = Notification.objects.get(
            school=self.school, channel=ChannelChoices.EMAIL,
            unregistered_email="debtor@example.com",
            event_type__key="billing.invoice_overdue",
        )
        self.assertIn(notice.message, email.body)

    # --- 3. platform/no-school entity: skipped gracefully ------------------ #

    # Verify no school entity still delivers behavior.
    def test_no_school_entity_still_delivers(self):
        # Recipient-centric notifications: a platform/product book (no source_school)
        # still delivers to the customer's billing_email — school is an optional scope,
        # not a gate. (Tracks the notifications overhaul.)
        from vs_notifications.models import Notification

        platform = LedgerEntity.objects.create(
            name="Platform Books", code="PLTDN", kind=LedgerEntity.Kind.PLATFORM,
        )
        seed_chart_of_accounts(platform)
        FiscalYear.objects.create(
            entity=platform, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=platform, fiscal_year=FiscalYear.objects.get(entity=platform),
            period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )
        cust = Customer.objects.create(
            entity=platform, code="PCUST", name="Platform Debtor",
            receivable_account=Account.objects.get(entity=platform, code="1200"),
            billing_email="p@example.com",
        )
        inv = Invoice.objects.create(
            entity=platform, customer=cust, invoice_date=datetime.date(2026, 1, 1),
            due_date=datetime.date(2026, 1, 10),
        )
        InvoiceLine.objects.create(
            invoice=inv, revenue_account=Account.objects.get(entity=platform, code="4100"),
            quantity=1, unit_price=100000, tax_code=None, line_no=1,
        )
        post_invoice(inv)
        ensure_default_policy(platform)
        notices = generate_dunning(platform, as_of=datetime.date(2026, 2, 15))
        self.assertEqual(len(notices), 1)

        before = Notification.objects.count()
        mark_notice_sent(notices[0])  # must not raise
        notices[0].refresh_from_db()
        # No school → still delivered (recipient-centric), and the notice flips SENT.
        self.assertEqual(notices[0].notice_status, "SENT")
        self.assertGreater(Notification.objects.count(), before)

    # --- 4. customer without billing_email → FAILED notification ---------- #

    # Verify missing billing email records failed notification behavior.
    def test_missing_billing_email_records_failed_notification(self):
        from vs_notifications.models import Notification
        from vs_notifications.constants import ChannelChoices, NotificationStatus

        self.customer.billing_email = ""
        self.customer.save(update_fields=["billing_email"])

        notice = self._generate_one()
        mark_notice_sent(notice)  # must not crash

        failed = Notification.objects.filter(
            school=self.school, channel=ChannelChoices.EMAIL,
            status=NotificationStatus.FAILED, failure_reason="NO_EMAIL_ADDRESS",
        )
        self.assertTrue(failed.exists())
        notice.refresh_from_db()
        self.assertEqual(notice.notice_status, "SENT")

    # --- 5. run_daily_dunning end-to-end + skips no-policy entity ---------- #

    # Verify run daily dunning generates dispatches and skips no policy behavior.
    def test_run_daily_dunning_generates_dispatches_and_skips_no_policy(self):
        from vs_finance.tasks import run_daily_dunning
        from vs_notifications.models import Notification
        from vs_notifications.constants import ChannelChoices
        from vs_notifications.services.seed import seed_school_settings

        # This entity has a policy + an overdue invoice.
        ensure_default_policy(self.entity)
        self._overdue_invoice(due=datetime.date(2026, 1, 5))

        # A second school entity with NO policy — must be skipped, not crash the run.
        other_school = School.objects.create(name="Oak", slug="oak-dnt", code="OAKDN")
        seed_school_settings(other_school)
        other = LedgerEntity.objects.create(
            name="Oak Books", code="OAKBK", kind=LedgerEntity.Kind.TENANT,
            source_school=other_school,
        )
        seed_chart_of_accounts(other)

        result = run_daily_dunning()

        self.assertGreaterEqual(result["generated"], 1)
        self.assertGreaterEqual(result["sent"], 1)
        self.assertGreaterEqual(result["skipped"], 1)  # the no-policy entity
        self.assertTrue(Notification.objects.filter(
            school=self.school, channel=ChannelChoices.EMAIL,
            unregistered_email="debtor@example.com").exists())

    # --- 6. idempotency: second mark_notice_sent is a no-op --------------- #

    # Verify second mark sent does not duplicate notification behavior.
    def test_second_mark_sent_does_not_duplicate_notification(self):
        from vs_notifications.models import Notification

        notice = self._generate_one()
        mark_notice_sent(notice)
        count_after_first = Notification.objects.count()

        mark_notice_sent(notice)  # already SENT → no-op
        self.assertEqual(Notification.objects.count(), count_after_first)


# Group tests for Invoice Notification Tests.
class InvoiceNotificationTests(_GLFixtureMixin, TestCase):
    """Invoice + receipt notifications routed through vs_notifications (best-effort).

    Fee/manual invoices email the customer on issue; opening-balance invoices stay
    silent; every receipt emails a confirmation. Delivery is recipient-centric (works
    with or without a school) and must NEVER break the underlying money posting.
    """

    # Prepare or verify the setUp test path.
    def setUp(self):
        from vs_notifications.services.seed import (
            seed_event_types, seed_notification_templates, seed_school_settings,
        )
        seed_event_types()
        seed_notification_templates()
        self.school = School.objects.create(name="Birchwood", slug="birchwood-int", code="BRCIN")
        seed_school_settings(self.school)
        seed_currencies()
        self.entity = LedgerEntity.objects.create(
            name="Birchwood Books", code="BRCBK", kind=LedgerEntity.Kind.TENANT,
            source_school=self.school,
        )
        seed_chart_of_accounts(self.entity)
        self.year = FiscalYear.objects.create(
            entity=self.entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        self.period = FiscalPeriod.objects.create(
            entity=self.entity, fiscal_year=self.year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )
        self.bank = Account.objects.get(entity=self.entity, code="1100")
        self.customer = Customer.objects.create(
            entity=self.entity, code="CUSTI", name="Payer Ltd",
            receivable_account=Account.objects.get(entity=self.entity, code="1200"),
            billing_email="payer@example.com",
        )

    # Support the make invoice workflow.
    def _make_invoice(self, *, unit_price=100000, source="MANUAL"):
        inv = Invoice.objects.create(
            entity=self.entity, customer=self.customer,
            invoice_date=datetime.date(2026, 1, 5), due_date=datetime.date(2026, 1, 20),
            source=source,
        )
        InvoiceLine.objects.create(
            invoice=inv, revenue_account=Account.objects.get(entity=self.entity, code="4100"),
            quantity=1, unit_price=unit_price, tax_code=None, line_no=1,
        )
        return inv

    # Support the issued workflow.
    def _issued(self):
        from vs_notifications.models import Notification
        return Notification.objects.filter(event_type__key="billing.invoice_issued")

    # Support the received workflow.
    def _received(self):
        from vs_notifications.models import Notification
        return Notification.objects.filter(event_type__key="billing.payment_received")

    # Verify posting manual invoice notifies customer behavior.
    def test_posting_manual_invoice_notifies_customer(self):
        from vs_notifications.constants import ChannelChoices

        inv = self._make_invoice()
        post_invoice(inv)
        self.assertTrue(self._issued().filter(
            channel=ChannelChoices.EMAIL, unregistered_email="payer@example.com").exists())

    # Verify opening balance invoice stays silent behavior.
    def test_opening_balance_invoice_stays_silent(self):
        from vs_finance.receivables import post_opening_balance

        self.customer.opening_balance = 500000
        self.customer.save(update_fields=["opening_balance"])
        post_opening_balance(self.customer, date=datetime.date(2026, 1, 5))
        # Opening balances are migration artefacts — no invoice_issued email.
        self.assertFalse(self._issued().exists())

    # Verify posting receipt notifies customer behavior.
    def test_posting_receipt_notifies_customer(self):
        from vs_notifications.constants import ChannelChoices

        inv = self._make_invoice()
        post_invoice(inv)
        pay = Payment.objects.create(
            entity=self.entity, customer=self.customer,
            payment_date=datetime.date(2026, 1, 10), amount=100000, deposit_account=self.bank,
        )
        post_payment(pay, allocations=[(inv, 100000)])
        self.assertTrue(self._received().filter(
            channel=ChannelChoices.EMAIL, unregistered_email="payer@example.com").exists())

    # Verify notification failure does not break posting behavior.
    def test_notification_failure_does_not_break_posting(self):
        from vs_notifications.models import NotificationEventType

        # Deactivate the event so send_notification raises inside the best-effort
        # wrapper; the invoice must still post cleanly (money is never held hostage
        # to a notification problem).
        NotificationEventType.objects.filter(key="billing.invoice_issued").update(is_active=False)
        inv = self._make_invoice()
        post_invoice(inv)  # must not raise
        inv.refresh_from_db()
        self.assertEqual(inv.status, DocumentStatus.POSTED)
        self.assertTrue(AccountBalance.objects.filter(
            account__entity=self.entity, period=self.period).exists())
        self.assertFalse(self._issued().exists())

    # Verify gateway style receipt notifies behavior.
    def test_gateway_style_receipt_notifies(self):
        # A standalone receipt (as the gateway books it) fires payment_received too.
        pay = Payment.objects.create(
            entity=self.entity, customer=self.customer,
            payment_date=datetime.date(2026, 1, 12), amount=50000, deposit_account=self.bank,
        )
        post_payment(pay, auto_allocate=False)
        self.assertTrue(self._received().exists())

    # Verify no school entity posts and delivers behavior.
    def test_no_school_entity_posts_and_delivers(self):
        # Recipient-centric: a platform book (no school) still notifies, and posting
        # is unaffected.
        platform = LedgerEntity.objects.create(
            name="Platform Books", code="PLTIN", kind=LedgerEntity.Kind.PLATFORM,
        )
        seed_chart_of_accounts(platform)
        FiscalYear.objects.create(
            entity=platform, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=platform, fiscal_year=FiscalYear.objects.get(entity=platform),
            period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )
        cust = Customer.objects.create(
            entity=platform, code="PLC", name="Platform Payer",
            receivable_account=Account.objects.get(entity=platform, code="1200"),
            billing_email="pp@example.com",
        )
        inv = Invoice.objects.create(
            entity=platform, customer=cust, invoice_date=datetime.date(2026, 1, 5),
            due_date=datetime.date(2026, 1, 20), source="MANUAL",
        )
        InvoiceLine.objects.create(
            invoice=inv, revenue_account=Account.objects.get(entity=platform, code="4100"),
            quantity=1, unit_price=100000, tax_code=None, line_no=1,
        )
        post_invoice(inv)  # must not raise
        inv.refresh_from_db()
        self.assertEqual(inv.status, DocumentStatus.POSTED)
        self.assertTrue(self._issued().filter(unregistered_email="pp@example.com").exists())
