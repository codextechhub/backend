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
from vs_finance.models import (
    Account,
    AccountBalance,
    Customer,
    FinanceAuditLog,
    FiscalPeriod,
    FiscalYear,
    Invoice,
    InvoiceLine,
    JournalEntry,
    JournalLine,
    LedgerEntity,
    Payment,
    TaxCode,
)
from vs_finance.money import format_naira, to_kobo, to_naira
from vs_finance.numbering import next_document_number
from vs_finance.posting import ensure_balanced, ensure_period_open, post_journal, reverse_journal
from vs_finance.receivables import post_invoice, post_payment
from vs_finance.reports import ar_aging, reconcile_ar, trial_balance
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
