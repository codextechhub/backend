"""Receivables accruals: deferred income, the doubtful-debt provision, deposits and payers.

Corona runs Ikeja and Lekki; Harbour runs one branch. The Eze family is billed at
Ikeja, the Bellos at Lekki, and the Adebayo Trust sponsors pupils without being
filed under a branch. The tests follow the owner's decisions:

* fees billed for a service period are income over that period, released month by
  month, and a month cannot close with its share unreleased;
* a credit note, concession, write-off or void of a deferred bill takes back the
  unreleased part first;
* a provision run sizes each branch's allowance from how long debts are owed, a
  write-off uses it and is dated when made, and a debt paid after all is recovered;
* a refundable deposit is held, returned or set against bills when the customer
  leaves, and forfeited only years after they left;
* a sponsor is a customer billed for a beneficiary, and its statement names them.
"""
from __future__ import annotations

import datetime
from unittest import mock

from django.db.models import Sum
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_finance.constants import (
    ChargeKind,
    CreditNoteKind,
    DeferredIncomeStatus,
    DepositStatus,
    DocumentStatus,
    IFRSLine,
    RevenueRecognitionMethod,
)
from vs_finance.exceptions import PeriodCloseError, PostingError
from vs_finance.models import (
    Account,
    Concession,
    CreditNote,
    CreditNoteLine,
    Customer,
    CustomerDeposit,
    DeferredIncomeEntry,
    FeeItem,
    FeeStructure,
    FinanceCalendarSettings,
    FinanceReceivablesPolicy,
    FiscalPeriod,
    Invoice,
    InvoiceLine,
    LedgerEntity,
    Payment,
    WriteOffRequest,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies, seed_fiscal_year

from .tests_branch_scope import _FinanceBranchFixture

D = datetime.date
SECOND_TERM = (D(2027, 1, 6), D(2027, 4, 4))


class _AccrualFixture(TestCase):
    """Two schools: Corona with two branches, Harbour with one."""

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.tests.helpers import make_branch, make_school

        seed_currencies()
        cls.group = make_school(slug="acc-corona", name="Corona Group", status="ACTIVE")
        cls.tenant = cls.group.tenant
        cls.ikeja = make_branch(cls.group, name="Ikeja Branch")
        cls.lekki = make_branch(cls.group, name="Lekki Branch", is_main=False)
        cls.books = cls.build_books("ACCCOR", cls.tenant)

        cls.harbour = make_school(slug="acc-harbour", name="Harbour", status="ACTIVE")
        cls.solo_tenant = cls.harbour.tenant
        cls.solo_main = make_branch(cls.harbour, name="Main Branch")
        cls.solo_books = cls.build_books("ACCSOLO", cls.solo_tenant)

        cls.eze = cls.customer(cls.books, "EZE", cls.ikeja)
        cls.bello = cls.customer(cls.books, "BELLO", cls.lekki)
        cls.trust = cls.customer(cls.books, "TRUST", None)
        cls.ada = cls.customer(cls.solo_books, "ADA", cls.solo_main)

    @classmethod
    def build_books(cls, code, tenant):
        """Books for 2026 and 2027 whose months close in any order.

        The accrual tests close a month of 2027 while 2026 is still open, because
        the second term's fees are billed in December. The order the months close
        in is not what they test (:mod:`vs_finance.tests_period_order` does that),
        so these books turn it off.
        """
        entity = LedgerEntity.objects.create(
            name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
        )
        seed_chart_of_accounts(entity)
        seed_fiscal_year(entity, year=2026)
        seed_fiscal_year(entity, year=2027)
        FinanceCalendarSettings.objects.create(entity=entity, periods_close_in_order=False)
        return entity

    @classmethod
    def customer(cls, entity, code, branch):
        return Customer.objects.create(
            entity=entity, code=code, name=f"Parent {code}", branch=branch,
            receivable_account=Account.objects.get(entity=entity, code="1200"),
        )

    # -- documents ----------------------------------------------------------- #

    def acct(self, code, books=None):
        return Account.objects.get(entity=books or self.books, code=code)

    def bill(self, customer, amount=150_000, *, on=D(2026, 12, 10), due=None,
             service=SECOND_TERM, kind=ChargeKind.CHARGE, books=None, post=True):
        from vs_finance.receivables import post_invoice

        books = books or customer.entity
        invoice = Invoice.objects.create(
            entity=books, customer=customer, branch=customer.branch, invoice_date=on,
            due_date=due or on + datetime.timedelta(days=15),
        )
        start, end = service if service else (None, None)
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=amount, kind=kind,
            revenue_account=self.acct("4100", books), service_start=start, service_end=end,
        )
        if post:
            post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def receipt(self, customer, amount, *, on, invoice=None):
        from vs_finance.receivables import post_payment

        payment = Payment.objects.create(
            entity=customer.entity, customer=customer, branch=customer.branch,
            payment_date=on, amount=amount, deposit_account=self.acct("1100", customer.entity),
        )
        post_payment(payment, allocations=[(invoice, amount)] if invoice else None,
                     auto_allocate=invoice is None)
        payment.refresh_from_db()
        return payment

    def credit_note(self, invoice, amount, *, on):
        from vs_finance.credit_notes import post_credit_note

        note = CreditNote.objects.create(
            entity=invoice.entity, customer=invoice.customer, branch=invoice.branch,
            kind=CreditNoteKind.CREDIT, note_date=on, invoice=invoice, reason="Withdrawal",
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=self.acct("4900", invoice.entity), quantity=1,
            unit_price=amount, line_no=1,
        )
        post_credit_note(note)
        note.refresh_from_db()
        return note

    def net_credit(self, code, *, books=None, branch=None):
        """Credit minus debit posted to account ``code``, optionally one branch's journals."""
        from vs_finance.branch_ledger import ledger_lines

        lines = ledger_lines(books or self.books).filter(account__code=code)
        if branch is not None:
            lines = lines.filter(entry__branch=branch)
        totals = lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        return int(totals["credit"] or 0) - int(totals["debit"] or 0)

    def period(self, year, month, books=None):
        return FiscalPeriod.objects.get(
            entity=books or self.books, start_date=D(year, month, 1), is_closing=False)

    def policy(self, books=None, **values):
        FinanceReceivablesPolicy.objects.update_or_create(
            entity=books or self.books, defaults=values)

    def schedule(self, invoice):
        return list(DeferredIncomeEntry.objects.filter(invoice=invoice)
                    .order_by("recognition_date", "pk")
                    .values_list("recognition_date", "amount", "unwound_amount", "status"))


# --------------------------------------------------------------------------- #
# Deferred income                                                              #
# --------------------------------------------------------------------------- #

class DeferredIncomePostingTests(_AccrualFixture):
    """Second Term billed on 10 December is a liability in December, income from January."""

    def test_a_bill_raised_before_its_period_is_deferred_not_income(self):
        invoice = self.bill(self.eze)

        self.assertEqual(self.net_credit("2160"), 150_000)
        self.assertEqual(self.net_credit("4100"), 0)
        self.assertEqual(self.schedule(invoice), [
            (D(2027, 1, 6), 37_500, 0, DeferredIncomeStatus.PENDING),
            (D(2027, 2, 1), 37_500, 0, DeferredIncomeStatus.PENDING),
            (D(2027, 3, 1), 37_500, 0, DeferredIncomeStatus.PENDING),
            (D(2027, 4, 1), 37_500, 0, DeferredIncomeStatus.PENDING),
        ])

    def test_the_last_month_takes_the_remainder_in_whole_kobo(self):
        invoice = self.bill(self.eze, 100_001, service=(D(2027, 1, 6), D(2027, 3, 31)))

        self.assertEqual([row[1] for row in self.schedule(invoice)], [33_333, 33_333, 33_335])

    def test_at_period_start_recognises_it_all_when_the_period_begins(self):
        self.policy(revenue_recognition=RevenueRecognitionMethod.AT_PERIOD_START)
        invoice = self.bill(self.eze)

        self.assertEqual(self.schedule(invoice),
                         [(D(2027, 1, 6), 150_000, 0, DeferredIncomeStatus.PENDING)])

    def test_a_period_already_begun_or_no_period_is_income_on_the_day(self):
        self.bill(self.eze, service=(D(2026, 12, 1), D(2027, 3, 31)))
        self.bill(self.eze, 20_000, service=None)

        self.assertEqual(self.net_credit("4100"), 170_000)
        self.assertEqual(self.net_credit("2160"), 0)
        self.assertFalse(DeferredIncomeEntry.objects.exists())

    def test_a_deferred_bill_is_still_owed_in_full(self):
        from vs_finance.reports import reconcile_ar

        self.bill(self.eze)
        self.assertTrue(reconcile_ar(self.books).is_reconciled)
        self.assertEqual(self.net_credit("1200"), -150_000)

    def test_deferred_income_presents_as_a_current_liability(self):
        from vs_finance.reports import _ifrs_sofp_sections

        sections = {key: lines for key, _label, lines in _ifrs_sofp_sections()}
        self.assertIn(IFRSLine.DEFERRED_INCOME, sections["current_liabilities"])
        self.assertEqual(self.acct("2160").ifrs_line, IFRSLine.DEFERRED_INCOME)


class DeferredIncomeReleaseTests(_AccrualFixture):
    """Each month's share moves to revenue once, one journal per branch per month."""

    def test_release_posts_one_journal_per_branch_per_month_and_only_once(self):
        from vs_finance.deferred_income import release_deferred_income

        self.bill(self.eze)
        self.bill(self.bello)
        releases = release_deferred_income(self.books, up_to=D(2027, 2, 28))

        self.assertEqual(
            sorted((r.branch_id, r.journal.date, r.amount) for r in releases),
            sorted([(self.ikeja.pk, D(2027, 1, 31), 37_500), (self.ikeja.pk, D(2027, 2, 28), 37_500),
                    (self.lekki.pk, D(2027, 1, 31), 37_500), (self.lekki.pk, D(2027, 2, 28), 37_500)]),
        )
        self.assertEqual(self.net_credit("4100", branch=self.ikeja), 75_000)
        self.assertEqual(self.net_credit("4100", branch=self.lekki), 75_000)
        self.assertEqual(self.net_credit("2160"), 150_000)
        self.assertEqual(release_deferred_income(self.books, up_to=D(2027, 2, 28)), [])

    def test_a_single_branch_tenant_releases_into_its_branch(self):
        from vs_finance.deferred_income import release_deferred_income

        self.bill(self.ada)
        (release,) = release_deferred_income(self.solo_books, up_to=D(2027, 1, 31))
        self.assertEqual(release.branch_id, self.solo_main.pk)
        self.assertEqual(self.net_credit("4100", books=self.solo_books), 37_500)

    def test_a_month_cannot_close_with_its_share_unreleased(self):
        from vs_finance.close import close_checklist, close_period

        invoice = self.bill(self.eze)
        january = self.period(2027, 1)
        failing = close_checklist(self.books, january)
        self.assertIn("deferred_income_released", [item.name for item in failing.failures])

        close_period(self.books, january)
        january.refresh_from_db()
        self.assertEqual(january.status, "CLOSED")
        self.assertEqual(self.schedule(invoice)[0][3], DeferredIncomeStatus.RELEASED)

    def test_the_close_preview_shows_the_share_as_done_by_the_close(self):
        from vs_finance.close import close_checklist

        self.bill(self.eze)
        preview = close_checklist(self.books, self.period(2027, 1), preview=True)
        item = next(i for i in preview.items if i.name == "deferred_income_released")
        self.assertTrue(item.passed)
        self.assertTrue(item.done_by_close)
        self.assertEqual(
            item.detail,
            "1 deferred income share (₦375.00) is due; closing the period releases it.",
        )
        self.assertTrue(preview.passed)

    def test_a_close_told_not_to_release_the_share_is_still_blocked(self):
        from vs_finance.close import close_period

        self.bill(self.eze)
        with self.assertRaises(PeriodCloseError) as refused:
            close_period(self.books, self.period(2027, 1), release_deferred=False)
        self.assertIn("deferred_income_released", refused.exception.failures)

    def test_releases_are_reversed_with_their_open_month_and_released_again(self):
        from vs_finance.deferred_income import release_deferred_income, reverse_deferred_release

        invoice = self.bill(self.eze)
        release_deferred_income(self.books, up_to=D(2027, 1, 31))
        self.assertEqual(reverse_deferred_release(self.books, self.period(2027, 1)), 1)

        self.assertEqual(self.net_credit("4100"), 0)
        self.assertEqual(self.schedule(invoice)[0][3], DeferredIncomeStatus.PENDING)
        self.assertEqual(len(release_deferred_income(self.books, up_to=D(2027, 1, 31))), 1)
        self.assertEqual(self.net_credit("4100"), 37_500)

    def test_a_closed_months_releases_stay_sealed(self):
        from vs_finance.close import close_period
        from vs_finance.deferred_income import reverse_deferred_release

        self.bill(self.eze)
        january = self.period(2027, 1)
        close_period(self.books, january)
        with self.assertRaises(PeriodCloseError):
            reverse_deferred_release(self.books, january)


class DeferredIncomeAdjustmentTests(_AccrualFixture):
    """What a bill's adjustments take back comes out of the months still to come."""

    def test_a_credit_note_takes_back_unreleased_income_latest_months_first(self):
        from vs_finance.deferred_income import release_deferred_income
        from vs_finance.voids import void_credit_note

        invoice = self.bill(self.eze)
        release_deferred_income(self.books, up_to=D(2027, 1, 31))
        note = self.credit_note(invoice, 60_000, on=D(2027, 2, 2))

        self.assertEqual(self.net_credit("2160"), 150_000 - 37_500 - 60_000)
        self.assertEqual(self.net_credit("4900"), 0)
        self.assertEqual(self.schedule(invoice)[2:], [
            (D(2027, 3, 1), 37_500, 22_500, DeferredIncomeStatus.PENDING),
            (D(2027, 4, 1), 37_500, 37_500, DeferredIncomeStatus.CANCELLED),
        ])

        void_credit_note(note)
        self.assertEqual(self.net_credit("2160"), 150_000 - 37_500)
        self.assertEqual(self.schedule(invoice)[2:], [
            (D(2027, 3, 1), 37_500, 0, DeferredIncomeStatus.PENDING),
            (D(2027, 4, 1), 37_500, 0, DeferredIncomeStatus.PENDING),
        ])

    def test_a_credit_note_beyond_what_is_deferred_reduces_revenue_for_the_rest(self):
        from vs_finance.deferred_income import release_deferred_income

        invoice = self.bill(self.eze)
        release_deferred_income(self.books, up_to=D(2027, 3, 31))
        self.credit_note(invoice, 60_000, on=D(2027, 4, 2))

        self.assertEqual(self.net_credit("2160"), 0)
        self.assertEqual(self.net_credit("4900"), -(60_000 - 37_500))

    def test_a_concession_on_a_deferred_bill_lowers_the_income_still_to_come(self):
        from vs_finance.installments import post_concession

        invoice = self.bill(self.eze)
        concession = Concession.objects.create(
            entity=self.books, customer=self.eze, invoice=invoice, branch=self.ikeja,
            concession_date=D(2026, 12, 20), amount=50_000,
        )
        post_concession(concession)

        self.assertEqual(self.net_credit("2160"), 100_000)
        self.assertEqual(self.net_credit("4910"), 0)

    def test_voiding_a_deferred_bill_takes_released_income_back(self):
        from vs_finance.deferred_income import release_deferred_income
        from vs_finance.voids import void_invoice

        invoice = self.bill(self.eze)
        release_deferred_income(self.books, up_to=D(2027, 1, 31))
        void_invoice(invoice, date=D(2027, 2, 5))

        self.assertEqual(self.net_credit("2160"), 0)
        self.assertEqual(self.net_credit("4100"), 0)
        self.assertEqual(self.net_credit("1200"), 0)
        self.assertEqual({row[3] for row in self.schedule(invoice)[1:]},
                         {DeferredIncomeStatus.CANCELLED})

    def test_a_write_off_of_a_deferred_bill_is_not_a_loss_for_the_unearned_part(self):
        from vs_finance.credit_notes import write_off_invoice

        invoice = self.bill(self.eze)
        write_off_invoice(invoice, write_off_date=D(2026, 12, 20))

        self.assertEqual(self.net_credit("2160"), 0)
        self.assertEqual(self.net_credit("5350"), 0)


# --------------------------------------------------------------------------- #
# Doubtful-debt provision, write-offs and recovery                             #
# --------------------------------------------------------------------------- #

class ProvisionTests(_AccrualFixture):
    """The allowance each branch holds follows how long its debts have been owed."""

    def owe(self, customer, amount, due):
        return self.bill(customer, amount, on=due - datetime.timedelta(days=5), due=due,
                         service=None)

    def test_a_run_sizes_each_branchs_allowance_from_the_age_of_its_debts(self):
        from vs_finance.provisions import post_provision, prepare_provision

        self.owe(self.eze, 1_000_000, D(2026, 1, 10))    # 355 days: 25%.
        self.owe(self.bello, 400_000, D(2026, 9, 1))      # 121 days: nothing yet.
        provision = post_provision(prepare_provision(self.books, as_of=D(2026, 12, 31)))

        lines = {line.branch_id: line for line in provision.lines.all()}
        self.assertEqual(set(lines), {self.ikeja.pk})
        self.assertEqual((lines[self.ikeja.pk].required, lines[self.ikeja.pk].movement),
                         (250_000, 250_000))
        self.assertEqual(lines[self.ikeja.pk].journal.branch_id, self.ikeja.pk)
        self.assertEqual(self.net_credit("1290", branch=self.ikeja), 250_000)
        self.assertEqual(self.net_credit("5350"), -250_000)
        self.assertEqual(provision.status, DocumentStatus.POSTED)

    def test_a_later_run_releases_what_is_no_longer_needed(self):
        from vs_finance.provisions import post_provision, prepare_provision

        invoice = self.owe(self.eze, 1_000_000, D(2026, 1, 10))
        post_provision(prepare_provision(self.books, as_of=D(2026, 12, 31)))
        self.receipt(self.eze, 600_000, on=D(2027, 1, 20), invoice=invoice)
        provision = post_provision(prepare_provision(self.books, as_of=D(2027, 1, 31)))

        (line,) = provision.lines.all()
        # 400,000 still owed, now 386 days overdue: 50%.
        self.assertEqual((line.required, line.current, line.movement), (200_000, 250_000, -50_000))
        self.assertEqual(self.net_credit("1290", branch=self.ikeja), 200_000)

    def test_a_write_off_uses_the_allowance_before_expense_and_is_dated_when_made(self):
        from vs_finance.credit_notes import post_write_off_request
        from vs_finance.provisions import post_provision, prepare_provision

        invoice = self.owe(self.eze, 1_000_000, D(2026, 1, 10))
        post_provision(prepare_provision(self.books, as_of=D(2026, 12, 31)))
        request = WriteOffRequest.objects.create(
            entity=self.books, invoice=invoice, branch=self.ikeja, amount=0)
        with mock.patch("vs_config.clock.branch_today", return_value=D(2027, 1, 15)):
            post_write_off_request(request)

        request.refresh_from_db()
        self.assertEqual(request.journal.date, D(2027, 1, 15))
        self.assertEqual((request.amount, request.allowance_used), (1_000_000, 250_000))
        self.assertEqual(self.net_credit("1290", branch=self.ikeja), 0)
        self.assertEqual(self.net_credit("5350"), -1_000_000)

    def test_a_written_off_debt_paid_later_is_recovered_as_income(self):
        from vs_finance.credit_notes import post_write_off_request, recover_write_off
        from vs_finance.reports import reconcile_ar
        from vs_finance.voids import void_payment

        invoice = self.owe(self.eze, 850_000, D(2026, 1, 10))
        request = WriteOffRequest.objects.create(
            entity=self.books, invoice=invoice, branch=self.ikeja, amount=850_000,
            write_off_date=D(2026, 12, 20))
        post_write_off_request(request)
        paid = self.receipt(self.eze, 300_000, on=D(2027, 2, 1))
        self.assertEqual(paid.credit_remaining, 300_000)

        recover_write_off(request, paid)
        invoice.refresh_from_db()
        paid.refresh_from_db()
        self.assertEqual((invoice.amount_paid, invoice.amount_credited), (300_000, 550_000))
        self.assertEqual(paid.credit_remaining, 0)
        self.assertEqual(self.net_credit("4810"), 300_000)
        self.assertEqual(self.net_credit("2140"), 0)
        self.assertTrue(reconcile_ar(self.books).is_reconciled)
        self.assertTrue(reconcile_ar(self.books, as_of=D(2027, 2, 1)).is_reconciled)

        void_payment(paid)
        invoice.refresh_from_db()
        self.assertEqual((invoice.amount_paid, invoice.amount_credited), (0, 850_000))
        self.assertEqual(self.net_credit("4810"), 0)
        self.assertTrue(reconcile_ar(self.books).is_reconciled)

    def test_a_receipt_from_before_the_write_off_is_not_a_recovery(self):
        from vs_finance.credit_notes import post_write_off_request, recover_write_off

        invoice = self.owe(self.eze, 100_000, D(2026, 1, 10))
        early = self.receipt(self.eze, 20_000, on=D(2026, 1, 1))
        request = WriteOffRequest.objects.create(
            entity=self.books, invoice=invoice, branch=self.ikeja, amount=100_000,
            write_off_date=D(2026, 12, 20))
        post_write_off_request(request)
        with self.assertRaisesMessage(PostingError, "dated before the write-off"):
            recover_write_off(request, early)


# --------------------------------------------------------------------------- #
# Customer deposits                                                            #
# --------------------------------------------------------------------------- #

class DepositTests(_AccrualFixture):
    """Mr Bello's caution deposit is held for him, never income."""

    def deposit_bill(self, customer, amount=50_000, *, on=D(2026, 1, 10)):
        return self.bill(customer, amount, on=on, service=None, kind=ChargeKind.DEPOSIT)

    def test_a_deposit_is_held_in_a_liability_never_revenue(self):
        invoice = self.deposit_bill(self.bello)

        self.assertEqual(self.net_credit("2170"), 50_000)
        self.assertEqual(self.net_credit("4100"), 0)
        deposit = CustomerDeposit.objects.get(invoice=invoice)
        self.assertEqual((deposit.status, deposit.branch_id), (DepositStatus.HELD, self.lekki.pk))

    def test_a_fee_run_bills_a_deposit_item_as_a_deposit(self):
        from vs_finance.fees import generate_invoices

        structure = FeeStructure.objects.create(entity=self.books, code="JSS1", name="JSS1")
        FeeItem.objects.create(structure=structure, line_no=1, description="Caution deposit",
                               revenue_account=self.acct("2170"), amount=50_000,
                               kind=ChargeKind.DEPOSIT)
        FeeItem.objects.create(structure=structure, line_no=2, description="Fees",
                               revenue_account=self.acct("4100"), amount=150_000)
        generate_invoices(structure, [self.bello], invoice_date=D(2026, 12, 10),
                          service_start=SECOND_TERM[0], service_end=SECOND_TERM[1])

        self.assertEqual(self.net_credit("2170"), 50_000)
        self.assertEqual(self.net_credit("2160"), 150_000)
        deposit_line = InvoiceLine.objects.get(kind=ChargeKind.DEPOSIT)
        self.assertIsNone(deposit_line.service_start)

    def test_leaving_opens_the_claim_and_a_release_returns_it_as_refundable_credit(self):
        from vs_finance.customers import set_customer_active
        from vs_finance.deposits import release_deposits
        from vs_finance.receivables import customer_refund_available_balance

        invoice = self.deposit_bill(self.bello)
        self.receipt(self.bello, 50_000, on=D(2026, 1, 12), invoice=invoice)
        with mock.patch("vs_finance.deposits.tenant_today", return_value=D(2026, 7, 20)):
            set_customer_active(self.bello, False)
        deposit = CustomerDeposit.objects.get(invoice=invoice)
        self.assertEqual(deposit.claim_opened_on, D(2026, 7, 20))
        self.assertEqual(deposit.status, DepositStatus.HELD)

        (note,) = release_deposits(self.bello, note_date=D(2026, 7, 21))
        deposit.refresh_from_db()
        self.assertEqual((deposit.status, deposit.release_note_id), (DepositStatus.RELEASED, note.pk))
        self.assertEqual(self.net_credit("2170"), 0)
        self.assertEqual(customer_refund_available_balance(self.bello, branch=self.lekki.pk), 50_000)

    def test_a_deposit_never_paid_is_cancelled_not_refunded(self):
        from vs_finance.deposits import release_deposits
        from vs_finance.receivables import customer_refund_available_balance

        invoice = self.deposit_bill(self.bello)
        release_deposits(self.bello, note_date=D(2026, 7, 21))

        invoice.refresh_from_db()
        self.assertEqual(invoice.balance_due, 0)
        self.assertEqual(customer_refund_available_balance(self.bello, branch=self.lekki.pk), 0)

    def test_setting_a_deposit_against_bills_needs_the_policy(self):
        from vs_finance.deposits import release_deposits

        self.deposit_bill(self.bello)
        with self.assertRaisesMessage(PostingError, "do not allow a deposit"):
            release_deposits(self.bello, offset=True)

    def test_leaving_owing_sets_the_deposit_against_bills_where_allowed(self):
        from vs_finance.customers import set_customer_active
        from vs_finance.receivables import customer_refund_available_balance

        self.policy(deposits_offset_unpaid_bills=True)
        deposit_invoice = self.deposit_bill(self.bello)
        self.receipt(self.bello, 50_000, on=D(2026, 1, 12), invoice=deposit_invoice)
        fees = self.bill(self.bello, 30_000, on=D(2026, 1, 15), service=None)
        with mock.patch("vs_config.clock.branch_today", return_value=D(2026, 7, 20)), \
                mock.patch("vs_finance.deposits.tenant_today", return_value=D(2026, 7, 20)):
            set_customer_active(self.bello, False)

        fees.refresh_from_db()
        self.assertEqual(fees.balance_due, 0)
        self.assertEqual(CustomerDeposit.objects.get(invoice=deposit_invoice).status,
                         DepositStatus.RELEASED)
        self.assertEqual(customer_refund_available_balance(self.bello, branch=self.lekki.pk), 20_000)

    def test_a_bill_carrying_the_deposit_is_never_settled_twice(self):
        """Fees and deposit on one bill, part paid: the deposit clears that bill, then the next."""
        from vs_finance.deposits import release_deposits
        from vs_finance.receivables import post_invoice

        self.policy(deposits_offset_unpaid_bills=True)
        both = Invoice.objects.create(
            entity=self.books, customer=self.bello, branch=self.lekki,
            invoice_date=D(2026, 1, 10), due_date=D(2026, 1, 25))
        InvoiceLine.objects.create(invoice=both, line_no=1, quantity=1, unit_price=50_000,
                                   kind=ChargeKind.DEPOSIT, revenue_account=self.acct("2170"))
        InvoiceLine.objects.create(invoice=both, line_no=2, quantity=1, unit_price=100_000,
                                   revenue_account=self.acct("4100"))
        post_invoice(both)
        both.refresh_from_db()
        self.receipt(self.bello, 120_000, on=D(2026, 1, 12), invoice=both)
        later = self.bill(self.bello, 40_000, on=D(2026, 2, 10), service=None)

        release_deposits(self.bello, offset=True, note_date=D(2026, 7, 21))
        both.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual((both.balance_due, later.balance_due), (0, 20_000))
        self.assertEqual(self.net_credit("2140"), 0)

    def test_unclaimed_deposits_are_forfeited_only_after_the_limit(self):
        from vs_finance.customers import set_customer_active
        from vs_finance.deposits import forfeit_unclaimed_deposits

        old = self.deposit_bill(self.bello)
        recent = self.deposit_bill(self.eze)
        self.receipt(self.bello, 50_000, on=D(2026, 1, 12), invoice=old)
        self.receipt(self.eze, 50_000, on=D(2026, 1, 12), invoice=recent)
        for customer in (self.bello, self.eze):
            set_customer_active(customer, False)
        CustomerDeposit.objects.filter(invoice=old).update(claim_opened_on=D(2020, 6, 30))
        CustomerDeposit.objects.filter(invoice=recent).update(claim_opened_on=D(2021, 1, 1))

        outcome = forfeit_unclaimed_deposits(self.books, as_of=D(2026, 6, 30))
        (forfeiture,) = outcome["forfeitures"]
        self.assertEqual((forfeiture.branch_id, forfeiture.amount), (self.lekki.pk, 50_000))
        self.assertEqual(self.net_credit("4820", branch=self.lekki), 50_000)
        self.assertEqual(CustomerDeposit.objects.get(invoice=recent).status, DepositStatus.HELD)
        again = forfeit_unclaimed_deposits(self.books, as_of=D(2026, 6, 30))
        self.assertEqual(again["forfeitures"], [])

    def test_voiding_a_deposit_bill_cancels_the_deposit_unless_it_was_returned(self):
        from vs_finance.deposits import release_deposits
        from vs_finance.voids import void_invoice, void_payment

        held = self.deposit_bill(self.bello)
        void_invoice(held)
        self.assertEqual(CustomerDeposit.objects.get(invoice=held).status, DepositStatus.CANCELLED)

        returned = self.deposit_bill(self.eze)
        paid = self.receipt(self.eze, 50_000, on=D(2026, 1, 12), invoice=returned)
        release_deposits(self.eze, note_date=D(2026, 7, 21))
        void_payment(paid)  # The bill is unpaid again, but its deposit was returned.
        with self.assertRaisesMessage(PostingError, "void the credit note that released it"):
            void_invoice(returned)


# --------------------------------------------------------------------------- #
# The API: whole-tenant runs, the policy, payers                               #
# --------------------------------------------------------------------------- #

class AccrualApiTests(_AccrualFixture):
    """Runs for every branch need a whole-tenant caller; reads narrow to the caller."""

    KEYS = (
        "finance.provision.view", "finance.provision.create", "finance.provision.post",
        "finance.deferredincome.view", "finance.deferredincome.run",
        "finance.settings.view", "finance.settings.update",
        "finance.invoice.create", "finance.invoice.view", "finance.report.view",
        "finance.deposit.view", "finance.deposit.settle",
    )

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.okafor = _FinanceBranchFixture.grant(
            _FinanceBranchFixture.user_for(cls.tenant, "okafor@acc.test"), *cls.KEYS,
            tenant=cls.tenant, role_key="acc-bursar")
        cls.lekki_bursar = _FinanceBranchFixture.grant(
            _FinanceBranchFixture.user_for(cls.tenant, "lekki@acc.test"), *cls.KEYS,
            tenant=cls.tenant, role_key="acc-lekki", branch=cls.lekki)
        cls.harbour_bursar = _FinanceBranchFixture.grant(
            _FinanceBranchFixture.user_for(cls.solo_tenant, "bursar@harbour.test"), *cls.KEYS,
            tenant=cls.solo_tenant, role_key="acc-harbour")

    def setUp(self):
        super().setUp()
        self.whole = TenantAPIClient(user=self.okafor)
        self.pinned = TenantAPIClient(user=self.lekki_bursar)
        self.rival = TenantAPIClient(user=self.harbour_bursar)

    def url(self, path, books=None):
        return f"/v1/finance/{path}?entity={(books or self.books).code}"

    def test_a_branch_bound_bursar_cannot_run_a_provision_for_every_branch(self):
        response = self.pinned.post(self.url("provisions/"), {"as_of": "2026-12-31"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_a_provision_posts_directly_only_when_confirmed_without_approval_steps(self):
        from vs_finance.approvals import ensure_tenant_approval_templates

        ensure_tenant_approval_templates(self.tenant, with_default_stages=False)
        self.bill(self.eze, 1_000_000, on=D(2026, 1, 5), due=D(2026, 1, 10), service=None)
        created = self.whole.post(self.url("provisions/"), {"as_of": "2026-12-31"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        pk = created.data["data"]["id"]

        refused = self.whole.post(self.url(f"provisions/{pk}/post/"), {}, format="json")
        self.assertNotEqual(refused.status_code, 200)
        posted = self.whole.post(self.url(f"provisions/{pk}/post/"),
                                 {"confirm_without_approval": True, "reason": "Year end"},
                                 format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["data"]["status"], DocumentStatus.POSTED)

    def test_another_tenants_bursar_cannot_read_a_provision(self):
        from vs_finance.provisions import prepare_provision

        provision = prepare_provision(self.books, as_of=D(2026, 12, 31))
        response = self.rival.get(self.url(f"provisions/{provision.pk}/"))
        self.assertIn(response.status_code, (403, 404))

    def test_deferred_income_release_is_whole_tenant_and_never_ahead_of_today(self):
        self.assertEqual(
            self.pinned.post(self.url("deferred-income/release/"), {}, format="json").status_code,
            403)
        ahead = self.whole.post(self.url("deferred-income/release/"),
                                {"up_to": "2099-01-31"}, format="json")
        self.assertEqual(ahead.status_code, 400)

    def test_deferred_income_reads_narrow_to_the_callers_branches(self):
        self.bill(self.eze)
        self.bill(self.bello, 90_000)
        pinned = self.pinned.get(self.url("deferred-income/"))
        whole = self.whole.get(self.url("deferred-income/"))
        self.assertEqual(pinned.status_code, 200, pinned.data)
        self.assertEqual(pinned.data["data"]["pending"], 90_000)
        self.assertEqual(whole.data["data"]["pending"], 240_000)

    def test_a_written_off_debt_is_recovered_through_the_api_with_its_own_key(self):
        from vs_finance.credit_notes import post_write_off_request

        invoice = self.bill(self.eze, 100_000, on=D(2026, 1, 5), service=None)
        request = WriteOffRequest.objects.create(
            entity=self.books, invoice=invoice, branch=self.ikeja, amount=100_000,
            write_off_date=D(2026, 6, 30))
        post_write_off_request(request)
        paid = self.receipt(self.eze, 40_000, on=D(2026, 8, 1))
        path = self.url(f"write-offs/{request.pk}/recover/")

        self.assertEqual(self.whole.post(path, {"payment": paid.pk}, format="json").status_code, 403)
        recoverer = _FinanceBranchFixture.grant(
            _FinanceBranchFixture.user_for(self.tenant, "recover@acc.test"),
            "finance.writeoff.reverse", tenant=self.tenant, role_key="acc-recover")
        response = TenantAPIClient(user=recoverer).post(path, {"payment": paid.pk}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["write_off"]["recovered_amount"], 40_000)

    def test_the_policy_is_whole_tenant_and_refuses_a_falling_rate(self):
        path = self.url("settings/receivables/")
        self.assertEqual(self.pinned.patch(
            path, {"unclaimed_deposit_years": 3}, format="json").status_code, 403)
        falling = self.whole.patch(path, {"provision_bands": [
            {"over_days": 180, "rate_bps": 5000}, {"over_days": 365, "rate_bps": 2500}]},
            format="json")
        self.assertEqual(falling.status_code, 400)
        saved = self.whole.patch(path, {"revenue_recognition": "AT_PERIOD_START",
                                        "deposits_offset_unpaid_bills": True}, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(saved.data["data"]["settings"]["revenue_recognition"], "AT_PERIOD_START")
        self.assertTrue(FinanceReceivablesPolicy.objects.get(entity=self.books)
                        .deposits_offset_unpaid_bills)

    def test_a_sponsor_is_billed_for_a_beneficiary_at_the_beneficiarys_branch(self):
        response = self.whole.post(self.url("invoices/"), {
            "customer": "TRUST", "beneficiary": "EZE", "invoice_date": "2026-01-10",
            "lines": [{"revenue_account": "4100", "unit_price": 300_000}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data["data"]
        self.assertEqual((data["customer_code"], data["beneficiary_code"], data["branch_id"]),
                         ("TRUST", "EZE", self.ikeja.pk))

        statement = self.whole.get(self.url("reports/customer-statement/") + "&customer=TRUST")
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertIn("(for Parent EZE)", statement.data["data"]["entries"][0]["description"])

    def test_a_branch_bound_bursar_cannot_name_another_branchs_beneficiary(self):
        response = self.pinned.post(self.url("invoices/"), {
            "customer": "TRUST", "beneficiary": "EZE", "invoice_date": "2026-01-10",
            "lines": [{"revenue_account": "4100", "unit_price": 300_000}],
        }, format="json")
        self.assertEqual(response.status_code, 404)

    def test_a_branch_bound_bursar_cannot_release_another_branchs_deposit(self):
        self.bill(self.eze, 50_000, on=D(2026, 1, 10), service=None, kind=ChargeKind.DEPOSIT)
        response = self.pinned.post(self.url("deposits/release/"), {"customer": "EZE"},
                                    format="json")
        self.assertEqual(response.status_code, 404)

    def test_the_deposit_list_narrows_to_the_callers_branches(self):
        self.bill(self.eze, 50_000, on=D(2026, 1, 10), service=None, kind=ChargeKind.DEPOSIT)
        self.bill(self.bello, 50_000, on=D(2026, 1, 10), service=None, kind=ChargeKind.DEPOSIT)
        response = self.pinned.get(self.url("deposits/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["customer_code"] for row in response.data["data"]], ["BELLO"])


class ExistingBooksMigrationTests(_AccrualFixture):
    """Books seeded before the accruals accounts existed receive them, and keep their own codes."""

    def test_existing_books_receive_the_new_accounts_without_losing_their_own(self):
        import importlib

        from django.apps import apps as django_apps

        migration = importlib.import_module(
            "vs_finance.migrations.0044_receivables_accruals_data")
        Account.objects.filter(entity=self.books, code__in=("1290", "2160", "4810")).delete()
        Account.objects.filter(entity=self.books, code="2170").update(name="Staff Loans")

        migration.forwards(django_apps, None)

        allowance = self.acct("1290")
        self.assertEqual((allowance.is_contra, allowance.normal_balance, allowance.parent.code),
                         (True, "CREDIT", "1000"))
        self.assertEqual(self.acct("2160").ifrs_line, IFRSLine.DEFERRED_INCOME)
        self.assertEqual(self.acct("4810").parent.code, "4000")
        self.assertEqual(self.acct("2170").name, "Staff Loans")
