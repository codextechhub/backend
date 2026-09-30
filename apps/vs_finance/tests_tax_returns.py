"""A tax return declares its own source lines, per branch, and nothing else.

Harbour Primary has one branch, Main. Its bursar files June's VAT on 5 July and
pays it on 21 July; July's return then declares July's tax and nothing of June's
filing or payment, because a return reads the lines no filed return has declared
rather than the payable account's movement between two dates. PAYE accrued each
month and paid the next is declared month by month the same way. A March sale
posted after March was filed is declared on April's return and listed "from
March". A month of heavy purchases leaves input VAT over, which the next VAT
return uses before anything is paid, and a month with nothing to declare still
files as a nil return.

Lagoon View runs Ikeja and Lekki. Its one VAT return breaks down by branch: each
branch's share is netted in its own journal and paid from its own bank account,
and the return is PAID when both shares are. A sale booked with no branch is a
"no branch yet" group that stops the return being filed; at Harbour the same
entry simply counts as Main's.

Un-filing releases the lines to the next return, a payment recorded in error is
reversed from the tax module (and only there), and an exempt fee line puts no
VAT on the return.
"""
from __future__ import annotations

import datetime

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.test import TestCase, tag

from core.migration_testing import RewoundSchemaTestCase
from vs_rbac.tests.helpers import make_branch, make_school
from vs_tenants.models import Branch

from .branch_ledger import ledger_lines
from .constants import (
    DocumentStatus,
    InvoicePaymentStatus,
    JournalSource,
    TaxFilingStatus,
    TaxTreatment,
)
from .exceptions import PostingError, TaxFilingError
from .models import (
    Account,
    BankAccount,
    Customer,
    FeeStructure,
    Invoice,
    InvoiceLine,
    JournalEntry,
    JournalLine,
    LedgerEntity,
    TaxCode,
    TaxFilingLine,
    TaxObligation,
)
from .posting import post_journal, resolve_period, reverse_journal
from .receivables import post_invoice
from .seed import seed_chart_of_accounts, seed_currencies, seed_fiscal_year
from .tax_filing import (
    ANY_SHARE,
    file_filing,
    pay_filing,
    prepare_filing,
    reverse_remittance,
    unfile_filing,
)
from .tests import _Phase4FixtureMixin


def d(month, day):
    return datetime.date(2026, month, day)


def month_end(month):
    return (d(month + 1, 1) - datetime.timedelta(days=1)) if month < 12 else d(12, 31)


def _books(code, tenant):
    entity = LedgerEntity.objects.create(
        name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
    )
    seed_chart_of_accounts(entity)
    seed_fiscal_year(entity, year=2026, start_month=1)
    return entity


def _bank(books, code, name, branch=None):
    gl = Account.objects.create(
        entity=books, code=code, name=name, is_postable=True,
        account_type=Account.objects.get(entity=books, code="1100").account_type,
    )
    return BankAccount.objects.create(entity=books, name=name, branch=branch, gl_account=gl)


class _ReturnsFixture(TestCase):
    """Harbour Primary (Main only) and Lagoon View (Ikeja, Lekki), each with 2026 books."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.harbour = make_school(slug="harbour-tax-returns", name="Harbour Primary")
        cls.main = make_branch(cls.harbour, name="Main Branch")
        cls.harbour_books = _books("HBRTAX", cls.harbour.tenant)
        cls.harbour_bank = _bank(cls.harbour_books, "1150", "Harbour GTBank", cls.main)

        cls.lagoon = make_school(slug="lagoon-tax-returns", name="Lagoon View")
        cls.ikeja = make_branch(cls.lagoon, name="Ikeja Branch")
        cls.lekki = make_branch(cls.lagoon, name="Lekki Branch", is_main=False)
        cls.lagoon_books = _books("LAGTAX", cls.lagoon.tenant)
        cls.ikeja_bank = _bank(cls.lagoon_books, "1151", "Ikeja GTBank", cls.ikeja)
        cls.lekki_bank = _bank(cls.lagoon_books, "1152", "Lekki Zenith", cls.lekki)
        cls.lagoon_bank = _bank(cls.lagoon_books, "1153", "Lagoon Access", None)

    # -- rows ----------------------------------------------------------------- #

    def post(self, books, branch, date, pairs, *, source="MANUAL"):
        """Post ``pairs`` of ``(code, debit, credit)`` on ``date`` for ``branch``.

        ``source`` names the document the journal stands in for: a ``MANUAL``
        journal may not touch a tax account or a bank ledger.
        """
        entry = JournalEntry.objects.create(
            entity=books, branch=branch, date=date,
            period=resolve_period(books, date), narration="test", source=source,
        )
        for line_no, (code, debit, credit) in enumerate(pairs, start=1):
            JournalLine.objects.create(
                entry=entry, account=Account.objects.get(entity=books, code=code),
                debit=debit, credit=credit, line_no=line_no,
            )
        post_journal(entry)
        return entry

    def sale(self, books, branch, date, vat):
        """A sale whose output VAT is ``vat``: Dr cash, Cr revenue, Cr 2200."""
        net = vat * 40 // 3
        return self.post(books, branch, date, [("1100", net + vat, 0), ("4100", 0, net), ("2200", 0, vat)],
                         source="SALES")

    def purchase(self, books, branch, date, vat):
        """A purchase carrying ``vat`` of input VAT: Dr expense, Dr 1300, Cr cash."""
        net = vat * 40 // 3
        return self.post(books, branch, date, [("5300", net, 0), ("1300", vat, 0), ("1100", 0, net + vat)],
                         source="PURCHASE")

    def payroll(self, books, branch, date, paye):
        """A payroll accrual parking ``paye`` in PAYE Payable."""
        return self.post(books, branch, date, [("5200", paye, 0), ("2310", 0, paye)],
                         source="PAYROLL")

    def withholding(self, books, branch, date, wht):
        """A vendor payment withholding ``wht``."""
        return self.post(books, branch, date, [("5300", wht, 0), ("2300", 0, wht)],
                         source="PURCHASE")

    def obligation(self, books, code):
        return TaxObligation.objects.get(entity=books, code=code)

    def prepare(self, books, code, month):
        return prepare_filing(self.obligation(books, code), period_start=d(month, 1),
                              period_end=month_end(month))

    def balance(self, books, code):
        """Credit minus debit on ``code`` across the whole ledger."""
        agg = ledger_lines(books).filter(account__code=code).aggregate(
            d=Sum("debit"), c=Sum("credit"))
        return int(agg["c"] or 0) - int(agg["d"] or 0)


class ConsecutiveReturnTests(_ReturnsFixture):
    """Last month's filing and payment never reduce this month's tax."""

    def test_july_declares_exactly_julys_vat_after_june_is_filed_and_paid_in_july(self):
        books = self.harbour_books
        self.sale(books, self.main, d(6, 10), 75_000)
        self.purchase(books, self.main, d(6, 12), 20_000)
        june = self.prepare(books, "VAT", 6)
        self.assertEqual(june.amount_due, 55_000)
        file_filing(june, filed_date=d(7, 5), filing_reference="FIRS-JUN")
        pay_filing(june, bank_account=self.harbour_bank, pay_date=d(7, 21))
        self.assertEqual(june.filing_status, TaxFilingStatus.PAID)

        self.sale(books, self.main, d(7, 15), 80_000)
        self.purchase(books, self.main, d(7, 16), 10_000)
        july = self.prepare(books, "VAT", 7)

        self.assertEqual(
            (july.gross_liability, july.recoverable_amount, july.amount_due),
            (80_000, 10_000, 70_000),
        )
        self.assertEqual(july.declared_line_count, 2)
        self.assertEqual(july.late_items, [])

    def test_paye_accrued_each_month_and_paid_the_next_declares_each_months_own_accrual(self):
        books = self.harbour_books
        accruals = {5: 400_000, 6: 420_000, 7: 425_000}
        for month, paye in accruals.items():
            self.payroll(books, self.main, d(month, 27), paye)
            filing = self.prepare(books, "PAYE", month)
            with self.subTest(month=month):
                self.assertEqual(filing.gross_liability, paye)
                self.assertEqual(filing.amount_due, paye)
                self.assertEqual(filing.late_items, [])
            file_filing(filing, filed_date=d(month + 1, 5))
            pay_filing(filing, bank_account=self.harbour_bank, pay_date=d(month + 1, 10))
            self.assertEqual(filing.filing_status, TaxFilingStatus.PAID)
        self.assertEqual(self.balance(books, "2310"), 0)


class LateLineTests(_ReturnsFixture):
    """A line recorded after its month was filed goes on the next return, named by month."""

    def test_a_late_march_sale_appears_in_aprils_return_from_march(self):
        books = self.harbour_books
        self.sale(books, self.main, d(3, 10), 30_000)
        march = self.prepare(books, "VAT", 3)
        file_filing(march, filed_date=d(4, 5))

        late = self.sale(books, self.main, d(3, 25), 7_500)
        self.sale(books, self.main, d(4, 12), 15_000)
        april = self.prepare(books, "VAT", 4)

        self.assertEqual(april.gross_liability, 22_500)
        self.assertEqual(april.late_line_count, 1)
        self.assertEqual(april.late_items, [{
            "month": "2026-03", "label": "from March", "gross": 7_500,
            "recoverable": 0, "net": 7_500, "line_count": 1,
            "branches": {str(self.main.pk): {
                "gross": 7_500, "recoverable": 0, "net": 7_500, "line_count": 1,
            }},
        }])

        file_filing(april, filed_date=d(5, 5))
        declaration = late.lines.get(account__code="2200").tax_declaration
        self.assertEqual(declaration.filing_id, april.pk)
        self.assertTrue(declaration.is_late)
        march.refresh_from_db()
        self.assertEqual(march.gross_liability, 30_000)


class CarriedCreditTests(_ReturnsFixture):
    """Surplus input VAT is carried to the next return instead of being lost."""

    def test_excess_input_vat_carries_forward_and_reduces_the_next_return(self):
        books = self.harbour_books
        self.sale(books, self.main, d(5, 10), 9_000)
        self.purchase(books, self.main, d(5, 14), 30_000)
        may = self.prepare(books, "VAT", 5)
        self.assertEqual((may.amount_due, may.carried_forward_credit), (0, 21_000))
        file_filing(may, filed_date=d(6, 5))
        self.assertEqual(may.filing_status, TaxFilingStatus.FILED)

        self.sale(books, self.main, d(6, 10), 50_000)
        self.purchase(books, self.main, d(6, 11), 5_000)
        june = self.prepare(books, "VAT", 6)
        self.assertEqual(june.brought_forward_credit, 21_000)
        self.assertEqual((june.amount_due, june.carried_forward_credit), (24_000, 0))

        file_filing(june, filed_date=d(7, 5))
        self.assertEqual(june.credit_from_id, may.pk)
        pay_filing(june, bank_account=self.harbour_bank, pay_date=d(7, 21))
        self.assertEqual(june.filing_status, TaxFilingStatus.PAID)
        self.assertEqual(self.balance(books, "2200"), 0)
        self.assertEqual(self.balance(books, "1300"), 0)

        with self.assertRaises(TaxFilingError):
            unfile_filing(may)

    def test_a_nil_return_files_with_nothing_to_remit(self):
        nil = self.prepare(self.harbour_books, "WHT", 8)
        self.assertEqual((nil.amount_due, nil.declared_line_count), (0, 0))

        file_filing(nil, filed_date=d(9, 5), filing_reference="FIRS-NIL-AUG")

        self.assertEqual(nil.filing_status, TaxFilingStatus.FILED)
        self.assertEqual(nil.payment_status, InvoicePaymentStatus.PAID)
        self.assertFalse(nil.remittances.exists())
        self.assertFalse(JournalEntry.objects.filter(
            entity=self.harbour_books, source=JournalSource.TAX).exists())
        with self.assertRaises(TaxFilingError):
            pay_filing(nil, bank_account=self.harbour_bank, pay_date=d(9, 10))


class BranchShareTests(_ReturnsFixture):
    """One return per tenant, booked and paid branch by branch."""

    def june_vat(self):
        books = self.lagoon_books
        self.sale(books, self.ikeja, d(6, 10), 60_000)
        self.sale(books, self.lekki, d(6, 11), 40_000)
        self.purchase(books, self.lekki, d(6, 12), 10_000)
        filing = self.prepare(books, "VAT", 6)
        return filing

    def test_the_return_breaks_down_by_branch(self):
        filing = self.june_vat()
        shares = {s.branch_id: s for s in filing.shares.all()}

        self.assertEqual(set(shares), {self.ikeja.pk, self.lekki.pk})
        self.assertEqual(
            (shares[self.ikeja.pk].gross_liability, shares[self.ikeja.pk].amount_due), (60_000, 60_000))
        self.assertEqual(
            (shares[self.lekki.pk].gross_liability, shares[self.lekki.pk].recoverable_amount,
             shares[self.lekki.pk].amount_due),
            (40_000, 10_000, 30_000),
        )
        self.assertEqual(filing.amount_due, 90_000)

    def test_each_branch_pays_its_share_from_its_own_bank_and_the_return_is_paid_when_both_are(self):
        filing = self.june_vat()
        file_filing(filing, filed_date=d(7, 5))
        lekki_share = filing.shares.get(branch=self.lekki)
        self.assertEqual(lekki_share.filing_journal.branch_id, self.lekki.pk)
        self.assertIsNone(filing.shares.get(branch=self.ikeja).filing_journal)

        pay_filing(filing, bank_account=self.ikeja_bank, pay_date=d(7, 21))
        self.assertEqual(filing.filing_status, TaxFilingStatus.FILED)
        self.assertEqual(filing.payment_status, InvoicePaymentStatus.PARTIAL)

        pay_filing(filing, bank_account=self.lekki_bank, pay_date=d(7, 21))
        self.assertEqual(filing.filing_status, TaxFilingStatus.PAID)

        remittances = {r.branch_id: r for r in filing.remittances.select_related("journal")}
        self.assertEqual(set(remittances), {self.ikeja.pk, self.lekki.pk})
        for branch, bank, amount in ((self.ikeja, self.ikeja_bank, 60_000),
                                     (self.lekki, self.lekki_bank, 30_000)):
            journal = remittances[branch.pk].journal
            with self.subTest(branch=branch.name):
                self.assertEqual(journal.branch_id, branch.pk)
                self.assertEqual(journal.source, JournalSource.TAX)
                self.assertEqual(journal.date, d(7, 21))
                self.assertEqual(journal.lines.get(account=bank.gl_account).credit, amount)
        self.assertFalse(JournalEntry.objects.filter(
            entity=self.lagoon_books, source=JournalSource.TAX, branch__isnull=True).exists())

    def test_a_share_is_not_paid_from_another_branchs_bank(self):
        filing = self.june_vat()
        file_filing(filing, filed_date=d(7, 5))

        with self.assertRaises(TaxFilingError):
            pay_filing(filing, bank_account=self.ikeja_bank, pay_date=d(7, 21), branch=self.lekki)
        self.assertEqual(filing.amount_paid, 0)

    def test_a_tenant_wide_account_pays_no_share_at_two_branches(self):
        filing = self.june_vat()
        file_filing(filing, filed_date=d(7, 5))

        for branch in (ANY_SHARE, self.ikeja, self.lekki):
            with self.subTest(branch=getattr(branch, "name", "unnamed")), \
                    self.assertRaises(TaxFilingError):
                pay_filing(filing, bank_account=self.lagoon_bank, pay_date=d(7, 21), branch=branch)

        self.assertEqual(filing.amount_paid, 0)
        self.assertFalse(filing.remittances.exists())

    def test_the_only_unpaid_share_is_still_paid_only_from_its_own_branchs_bank(self):
        filing = self.june_vat()
        file_filing(filing, filed_date=d(7, 5))
        pay_filing(filing, bank_account=self.ikeja_bank, pay_date=d(7, 21))

        with self.assertRaises(TaxFilingError):
            pay_filing(filing, bank_account=self.ikeja_bank, pay_date=d(7, 22))
        self.assertEqual(filing.shares.get(branch=self.lekki).amount_paid, 0)

        pay_filing(filing, bank_account=self.lekki_bank, pay_date=d(7, 22))
        self.assertEqual(filing.filing_status, TaxFilingStatus.PAID)

    def test_a_share_outside_the_callers_reach_is_refused(self):
        filing = self.june_vat()
        file_filing(filing, filed_date=d(7, 5))

        with self.assertRaises(TaxFilingError):
            pay_filing(filing, bank_account=self.ikeja_bank, pay_date=d(7, 21),
                       reach=frozenset({self.lekki.pk}))
        pay_filing(filing, bank_account=self.lekki_bank, pay_date=d(7, 21),
                   reach=frozenset({self.lekki.pk}))

        self.assertEqual(list(filing.remittances.values_list("branch_id", flat=True)),
                         [self.lekki.pk])

    def test_an_unbranched_account_pays_for_the_only_branch_at_one_branch(self):
        books = self.harbour_books
        access = _bank(books, "1154", "Harbour Access", None)
        self.withholding(books, self.main, d(6, 10), 50_000)
        june = self.prepare(books, "WHT", 6)
        file_filing(june, filed_date=d(7, 5))

        pay_filing(june, bank_account=access, pay_date=d(7, 10))

        self.assertEqual(june.filing_status, TaxFilingStatus.PAID)
        self.assertEqual(june.remittances.get().journal.branch_id, self.main.pk)


class NarrowedReadTests(_ReturnsFixture):
    """A branch-bound reader sees the return narrowed to their branches' shares."""

    def test_the_return_shows_only_the_readers_shares_and_their_totals(self):
        from .serializers import TaxFilingSerializer

        books = self.lagoon_books
        self.sale(books, self.ikeja, d(5, 10), 30_000)
        self.sale(books, self.lekki, d(5, 20), 12_000)
        self.prepare(books, "VAT", 5)
        self.sale(books, self.ikeja, d(6, 10), 60_000)
        self.sale(books, self.lekki, d(6, 11), 40_000)
        self.purchase(books, self.lekki, d(6, 12), 10_000)
        filing = self.prepare(books, "VAT", 6)
        file_filing(filing, filed_date=d(7, 5))
        pay_filing(filing, bank_account=self.ikeja_bank, pay_date=d(7, 21))

        whole = TaxFilingSerializer(filing, context={"branch_ids": None}).data
        lekki = TaxFilingSerializer(filing, context={"branch_ids": frozenset({self.lekki.pk})}).data

        self.assertEqual(len(whole["branch_breakdown"]), 2)
        self.assertEqual(whole["amount_due"], 90_000 + 42_000)
        self.assertEqual([s["branch_id"] for s in lekki["branch_breakdown"]], [self.lekki.pk])
        self.assertEqual(
            (lekki["gross_liability"], lekki["recoverable_amount"], lekki["amount_due"],
             lekki["amount_paid"], lekki["balance_due"], lekki["payment_status"]),
            (52_000, 10_000, 42_000, 0, 42_000, "UNPAID"),
        )
        self.assertEqual(lekki["remittances"], [])
        self.assertEqual(
            [(i["label"], i["gross"], i["line_count"]) for i in lekki["late_items"]],
            [("from May", 12_000, 1)],
        )
        self.assertEqual(lekki["late_line_count"], 1)


class OneBranchBooksTests(_Phase4FixtureMixin, TestCase):
    """Books whose tenant owns one branch pay as one share, booked to that branch.

    Every tenant keeps a branch; the platform's own books belong to Lagos. A
    return on one-branch books needs no split: it is paid whole, from an
    account with no branch as readily as from Lagos's, and the remittance is
    booked to the only branch.
    """

    def test_one_branch_books_pay_their_one_share_to_that_branch(self):
        entity, _, periods = self.build_books()
        bank = self.make_bank(entity)
        self.post_wht(entity, periods[0])
        filing = prepare_filing(TaxObligation.objects.get(entity=entity, code="WHT"),
                                period_start=d(1, 1), period_end=d(1, 31))
        file_filing(filing, filed_date=d(2, 5))

        pay_filing(filing, bank_account=bank, pay_date=d(2, 10))

        self.assertEqual(filing.filing_status, TaxFilingStatus.PAID)
        only_branch = Branch.all_objects.get(tenant=entity.tenant)
        self.assertEqual(filing.remittances.get().journal.branch_id, only_branch.pk)

    def post_wht(self, entity, period):
        entry = JournalEntry.objects.create(entity=entity, date=d(1, 12), period=period,
                                            narration="Vendor withholding", source="PURCHASE")
        for line_no, (code, debit, credit) in enumerate(
                (("5300", 50_000, 0), ("2300", 0, 50_000)), start=1):
            JournalLine.objects.create(entry=entry, account=Account.objects.get(entity=entity, code=code),
                                       debit=debit, credit=credit, line_no=line_no)
        post_journal(entry)


class UnbranchedLineTests(_ReturnsFixture):
    """An entry with no branch is never guessed at a tenant with several."""

    def test_unbranched_lines_are_their_own_group_and_stop_the_filing_at_two_branches(self):
        books = self.lagoon_books
        self.sale(books, self.ikeja, d(6, 10), 60_000)
        self.sale(books, None, d(6, 15), 5_000)
        filing = self.prepare(books, "VAT", 6)

        pending = filing.shares.get(branch_pending=True)
        self.assertIsNone(pending.branch_id)
        self.assertEqual(pending.gross_liability, 5_000)

        with self.assertRaises(TaxFilingError) as refused:
            file_filing(filing, filed_date=d(7, 5))
        self.assertEqual(refused.exception.extra["unbranched_lines"], 1)
        self.assertEqual(filing.filing_status, TaxFilingStatus.DRAFT)
        self.assertFalse(TaxFilingLine.objects.filter(filing=filing).exists())

    def test_unbranched_lines_count_as_the_only_branch_at_one_branch(self):
        books = self.harbour_books
        entry = self.sale(books, None, d(6, 15), 5_000)
        filing = self.prepare(books, "VAT", 6)

        share = filing.shares.get()
        self.assertEqual((share.branch_id, share.branch_pending), (self.main.pk, False))
        file_filing(filing, filed_date=d(7, 5))
        declared = entry.lines.get(account__code="2200").tax_declaration
        self.assertEqual(declared.branch_id, self.main.pk)


class UndoTests(_ReturnsFixture):
    """Un-filing releases the lines; a wrong payment is reversed from the tax module."""

    def test_unfiling_releases_the_lines_to_the_next_return(self):
        books = self.harbour_books
        self.withholding(books, self.main, d(6, 10), 50_000)
        june = self.prepare(books, "WHT", 6)
        file_filing(june, filed_date=d(7, 5))
        self.assertEqual(TaxFilingLine.objects.filter(filing=june).count(), 1)

        unfile_filing(june)

        self.assertEqual(june.filing_status, TaxFilingStatus.DRAFT)
        self.assertFalse(TaxFilingLine.objects.filter(filing=june).exists())
        july = self.prepare(books, "WHT", 7)
        self.assertEqual(july.gross_liability, 50_000)
        self.assertEqual([i["label"] for i in july.late_items], ["from June"])

    def test_reversing_a_remittance_returns_the_filing_to_filed(self):
        books = self.harbour_books
        self.withholding(books, self.main, d(6, 10), 50_000)
        june = self.prepare(books, "WHT", 6)
        file_filing(june, filed_date=d(7, 5))
        pay_filing(june, bank_account=self.harbour_bank, pay_date=d(7, 10))
        self.assertEqual(june.filing_status, TaxFilingStatus.PAID)
        remittance = june.remittances.get()

        with self.assertRaises(PostingError):
            reverse_journal(remittance.journal)

        reverse_remittance(remittance, reason="The transfer bounced.")

        june.refresh_from_db()
        remittance.refresh_from_db()
        self.assertEqual(june.filing_status, TaxFilingStatus.FILED)
        self.assertEqual((june.amount_paid, june.payment_status), (0, InvoicePaymentStatus.UNPAID))
        self.assertTrue(remittance.is_reversed)
        self.assertEqual(remittance.journal.status, DocumentStatus.REVERSED)
        self.assertEqual(self.balance(books, "2300"), 50_000)

        pay_filing(june, bank_account=self.harbour_bank, pay_date=d(7, 12))
        self.assertEqual(june.filing_status, TaxFilingStatus.PAID)


class VatTreatmentTests(_Phase4FixtureMixin, TestCase):
    """Exempt and zero-rated codes are seeded, and an exempt line carries no VAT."""

    def test_the_starter_vat_codes_are_seeded(self):
        entity, _, _ = self.build_books()
        codes = {c.code: c for c in TaxCode.objects.filter(entity=entity).select_related(
            "collected_account", "paid_account")}

        self.assertEqual(
            {k: (c.treatment, c.rate_bps) for k, c in codes.items()},
            {"VAT-STD": (TaxTreatment.STANDARD, 750),
             "VAT-ZERO": (TaxTreatment.ZERO_RATED, 0),
             "VAT-EXEMPT": (TaxTreatment.EXEMPT, 0)},
        )
        self.assertEqual(codes["VAT-STD"].collected_account.code, "2200")
        self.assertEqual(codes["VAT-STD"].paid_account.code, "1300")

    def test_only_a_standard_code_may_carry_a_rate(self):
        entity, _, _ = self.build_books()
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaxCode.objects.create(
                entity=entity, code="VAT-EX2", name="Exempt at a rate", rate_bps=750,
                treatment=TaxTreatment.EXEMPT,
            )

    def test_an_exempt_line_adds_no_output_vat(self):
        entity, _, _ = self.build_books()
        customer = Customer.objects.create(
            entity=entity, code="CUST1", name="Adeyemi Family",
            receivable_account=Account.objects.get(entity=entity, code="1200"),
        )
        invoice = Invoice.objects.create(
            entity=entity, customer=customer, invoice_date=d(1, 10), due_date=d(1, 25),
        )
        for line_no, (price, code) in enumerate(((1_000_000, "VAT-EXEMPT"), (100_000, "VAT-STD")), 1):
            InvoiceLine.objects.create(
                invoice=invoice, revenue_account=Account.objects.get(entity=entity, code="4100"),
                quantity=1, unit_price=price, line_no=line_no,
                tax_code=TaxCode.objects.get(entity=entity, code=code),
            )
        post_invoice(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.tax_total, 7_500)

        vat = prepare_filing(
            TaxObligation.objects.get(entity=entity, code="VAT"),
            period_start=d(1, 1), period_end=d(1, 31),
        )
        self.assertEqual(vat.gross_liability, 7_500)

    def test_the_printed_invoice_names_each_lines_treatment(self):
        from .documents import invoice_document_context

        entity, _, _ = self.build_books()
        customer = Customer.objects.create(
            entity=entity, code="CUST1", name="Adeyemi Family",
            receivable_account=Account.objects.get(entity=entity, code="1200"),
        )
        invoice = Invoice.objects.create(
            entity=entity, customer=customer, invoice_date=d(1, 10), due_date=d(1, 25),
        )
        for line_no, code in enumerate(("VAT-EXEMPT", "VAT-ZERO", "VAT-STD", None), 1):
            InvoiceLine.objects.create(
                invoice=invoice, revenue_account=Account.objects.get(entity=entity, code="4100"),
                quantity=1, unit_price=100_000, line_no=line_no, description=code or "Blank",
                tax_code=TaxCode.objects.get(entity=entity, code=code) if code else None,
            )
        post_invoice(invoice)

        lines = invoice_document_context(invoice)["invoice"]["line_items"]

        self.assertEqual(
            [(line["tax_label"], line["is_exempt"]) for line in lines],
            [("Exempt", True), ("Zero rated", False), (None, False), ("No VAT", False)],
        )
        self.assertEqual(lines[1]["tax_amount"], "Zero rated")
        self.assertNotEqual(lines[2]["tax_amount"], "Exempt")

    # -- fee items: a blank tax code becomes the exempt code ------------------ #

    def api(self):
        """A platform super admin's client, which the finance routes let through."""
        from django.contrib.auth import get_user_model

        from core.test_utils import TenantAPIClient
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment

        from .tests import _platform_tenant

        platform = _platform_tenant()
        user = get_user_model().objects.create_user(
            tenant=platform, email="fee-vat@test.com", password="testpass123",
            status="ACTIVE", first_name="Fee", last_name="Admin",
        )
        role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=platform, key="xvs_super_admin",
            defaults={"name": "Super Admin", "status": "ACTIVE"},
        )
        TenantUserRoleAssignment.objects.create(
            tenant=platform, user=user, role=role, assignment_status="ACTIVE",
        )
        return TenantAPIClient(user=user)

    def fee_structure(self, client, entity, code, items):
        response = client.post(
            f"/v1/finance/fee-structures/?entity={entity.code}",
            {"code": code, "name": f"{code} fees", "items": items}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return FeeStructure.objects.get(entity=entity, code=code.upper())

    def item_codes(self, structure):
        return {
            item.description: item.tax_code.code if item.tax_code_id else None
            for item in structure.items.select_related("tax_code")
        }

    def test_a_fee_item_saved_with_no_code_is_vat_exempt(self):
        entity, _, _ = self.build_books()
        structure = self.fee_structure(self.api(), entity, "jss1t1", [
            {"description": "Term fee", "revenue_account": "4100", "amount": 10_000_000},
        ])

        self.assertEqual(self.item_codes(structure), {"Term fee": "VAT-EXEMPT"})
        self.assertEqual(structure.tax_total, 0)

    def test_a_fee_item_saved_with_vat_std_keeps_it(self):
        entity, _, _ = self.build_books()
        structure = self.fee_structure(self.api(), entity, "shop", [
            {"description": "Uniform", "revenue_account": "4100", "amount": 2_000_000,
             "tax_code": "VAT-STD"},
        ])

        self.assertEqual(self.item_codes(structure), {"Uniform": "VAT-STD"})
        self.assertEqual(structure.tax_total, 150_000)

    def test_a_missing_exempt_code_is_seeded_rather_than_refused(self):
        entity, _, _ = self.build_books()
        TaxCode.objects.filter(entity=entity, code="VAT-EXEMPT").delete()

        structure = self.fee_structure(self.api(), entity, "day", [
            {"description": "Day fee", "revenue_account": "4100", "amount": 100},
        ])

        exempt = TaxCode.objects.get(entity=entity, code="VAT-EXEMPT")
        self.assertEqual((exempt.treatment, exempt.rate_bps), (TaxTreatment.EXEMPT, 0))
        self.assertEqual(self.item_codes(structure), {"Day fee": "VAT-EXEMPT"})

    def test_a_clone_keeps_the_sources_codes_and_exempts_only_the_blank_ones(self):
        entity, _, _ = self.build_books()
        client = self.api()
        source = self.fee_structure(client, entity, "term", [
            {"description": "Term fee", "revenue_account": "4100", "amount": 10_000_000,
             "tax_code": "VAT-ZERO"},
            {"description": "Uniform", "revenue_account": "4100", "amount": 2_000_000,
             "tax_code": "VAT-STD"},
            {"description": "Older line", "revenue_account": "4100", "amount": 500_000},
        ])
        source.items.filter(description="Older line").update(tax_code=None)

        response = client.post(
            f"/v1/finance/fee-structures/{source.pk}/duplicate/?entity={entity.code}",
            {"code": "term-copy"}, format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        clone = FeeStructure.objects.get(entity=entity, code="TERM-COPY")
        self.assertEqual(self.item_codes(clone), {
            "Term fee": "VAT-ZERO", "Uniform": "VAT-STD", "Older line": "VAT-EXEMPT",
        })


@tag("slow")
class BlankFeeItemMigrationTests(RewoundSchemaTestCase):
    """0035 gives an existing fee item with no tax code its entity's exempt VAT code.

    Built through the historical models at 0034, where tax codes have no
    treatment and no starter codes exist yet. An item that already had a code
    keeps it.
    """

    APP = "vs_finance"
    BEFORE = "0034_a_closing_period_per_fiscal_year"
    AFTER = "0035_tax_returns_declare_source_lines"

    def test_a_blank_fee_item_takes_the_exempt_code_and_a_coded_one_keeps_its_own(self):
        seed_currencies()
        old = self.historical_apps(self.BEFORE)
        tenant = old.get_model("vs_tenants", "Tenant").objects.create(
            name="Migration Fees", slug="migration-fee-vat", kind="ORGANIZATION", status="ACTIVE",
        )
        entity = old.get_model("vs_finance", "LedgerEntity").objects.create(
            name="Migration Books", code="MIGFEE", number_code="977", kind="TENANT",
            tenant=tenant,
        )
        revenue = old.get_model("vs_finance", "Account").objects.create(
            entity=entity, code="4100", name="Operating Revenue",
            account_type="INCOME", normal_balance="CREDIT", is_postable=True,
        )
        standard = old.get_model("vs_finance", "TaxCode").objects.create(
            entity=entity, code="VAT75", name="VAT 7.5%", rate_bps=750,
        )
        structure = old.get_model("vs_finance", "FeeStructure").objects.create(
            entity=entity, code="TERM", name="Term fees",
        )
        FeeItem = old.get_model("vs_finance", "FeeItem")
        FeeItem.objects.create(structure=structure, description="Blank", line_no=1,
                               revenue_account=revenue, amount=100_000)
        FeeItem.objects.create(structure=structure, description="Coded", line_no=2,
                               revenue_account=revenue, amount=100_000, tax_code=standard)

        self.migrate_to(self.AFTER)

        new = self.historical_apps(self.AFTER)
        items = {
            item.description: (item.tax_code.code, item.tax_code.treatment)
            for item in new.get_model("vs_finance", "FeeItem").objects
            .filter(structure_id=structure.pk).select_related("tax_code")
        }
        self.assertEqual(items, {"Blank": ("VAT-EXEMPT", "EXEMPT"), "Coded": ("VAT75", "STANDARD")})
