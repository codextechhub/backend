"""Finance document lists, and the figures above them, narrowed to a branch and a year.

Bright Star School runs Ikeja and Lekki on a September to August year, so
FY2015 runs from 1 September 2014 to 31 August 2015. Mrs Bello keeps the books
for both branches. She asks for Ikeja's 2015 invoices, receipts, journals and
expense claims and gets exactly those, with the figures above each list
counting the same rows. Mrs Adeyemi works in Ikeja alone and cannot ask for
Lekki's. Harbour Primary has one branch, and its years narrow the same way.
"""
from __future__ import annotations

import datetime

from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.request import Request

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus
from vs_finance.models import (
    Account, Customer, ExpenseClaim, FiscalYear, Invoice, JournalEntry, LedgerEntity, Payment,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

VIEW_KEYS = (
    "finance.invoice.view", "finance.payment.view", "finance.journal.view",
    "finance.expenseclaim.view",
)


def _books(school, code):
    books = LedgerEntity.objects.create(
        name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=school.tenant,
    )
    seed_chart_of_accounts(books)
    return books


def _year(books, label, start, end, *, archived=False):
    return FiscalYear.objects.create(
        entity=books, year=label, start_date=start, end_date=end,
        archived_at=timezone.now() if archived else None,
    )


def _reader(school, branch, email, *keys, pinned_to=None):
    role = make_role(school, name=f"Bursar {email}", key=f"bursar-{email.split('@')[0]}")
    for key in keys:
        make_role_permission(role, make_permission(key))
    user = make_school_admin(branch, email=email)
    make_assignment(school, user, role, **({"branch": pinned_to} if pinned_to else {}))
    return user


def _customer(books, code="CUST1"):
    return Customer.objects.create(
        entity=books, code=code, name="Okafor family",
        receivable_account=Account.objects.get(entity=books, code="1200"),
    )


class FinanceListScopeTests(TestCase):
    """Invoices, receipts, journals and expense claims read one branch and one year."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.school = make_school(slug="bright-star-fin-scope", name="Bright Star School")
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=False)
        cls.books = _books(cls.school, "BSFSC")
        _year(cls.books, 2015, datetime.date(2014, 9, 1), datetime.date(2015, 8, 31))
        _year(cls.books, 2016, datetime.date(2015, 9, 1), datetime.date(2016, 8, 31))
        cls.bello = _reader(cls.school, cls.ikeja, "bello@bright-star-fin-scope.example.com", *VIEW_KEYS)
        cls.adeyemi = _reader(
            cls.school, cls.ikeja, "adeyemi@bright-star-fin-scope.example.com", *VIEW_KEYS,
            pinned_to=cls.ikeja,
        )
        customer = _customer(cls.books)

        def each(branch, day):
            return {
                "invoice": Invoice.objects.create(
                    entity=cls.books, customer=customer, branch=branch, invoice_date=day, due_date=day,
                ),
                "receipt": Payment.objects.create(
                    entity=cls.books, customer=customer, branch=branch, payment_date=day,
                    amount=100_000, status=DocumentStatus.POSTED,
                ),
                "journal": JournalEntry.objects.create(entity=cls.books, branch=branch, date=day),
                "claim": ExpenseClaim.objects.create(entity=cls.books, branch=branch, claim_date=day),
            }

        cls.ikeja_autumn_2014 = each(cls.ikeja, datetime.date(2014, 10, 2))
        cls.ikeja_summer_2015 = each(cls.ikeja, datetime.date(2015, 8, 31))
        cls.ikeja_autumn_2015 = each(cls.ikeja, datetime.date(2015, 9, 1))
        cls.lekki_spring_2015 = each(cls.lekki, datetime.date(2015, 3, 9))
        cls.no_branch_2015 = {
            "invoice": Invoice.objects.create(
                entity=cls.books, customer=customer, branch=None,
                invoice_date=datetime.date(2015, 5, 5), due_date=datetime.date(2015, 5, 5),
            ),
        }

    def get(self, path, query="", *, user=None, status=200):
        response = TenantAPIClient(user=user or self.bello).get(
            f"/v1/finance/{path}?entity={self.books.code}{query}",
        )
        self.assertEqual(response.status_code, status, getattr(response, "data", None))
        return response.data

    def ids(self, path, query="", **kw):
        return {row["id"] for row in self.get(path, query, **kw)["data"]}

    def picked(self, kind, *groups):
        return {group[kind].pk for group in groups}

    def test_each_list_answers_branch_and_year_together(self):
        query = f"&fiscal_year=2015&branch={self.ikeja.pk}"
        for path, kind in (
            ("invoices/", "invoice"), ("payments/", "receipt"),
            ("journals/", "journal"), ("expense-claims/", "claim"),
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.ids(path, query),
                    self.picked(kind, self.ikeja_autumn_2014, self.ikeja_summer_2015),
                )

    def test_a_year_ends_on_its_own_last_day_not_the_calendars(self):
        self.assertEqual(self.ids("invoices/", "&fiscal_year=2016"), self.picked("invoice", self.ikeja_autumn_2015))

    def test_a_date_range_is_inclusive_and_may_be_open_at_one_end(self):
        self.assertEqual(
            self.ids("journals/", "&date_from=2015-08-31"),
            self.picked("journal", self.ikeja_summer_2015, self.ikeja_autumn_2015),
        )

    def test_no_branch_and_no_year_still_lists_everything_in_reach(self):
        self.assertEqual(len(self.ids("invoices/")), 5)

    def test_unassigned_lists_the_invoices_not_yet_given_a_branch(self):
        self.assertEqual(self.ids("invoices/", "&branch=unassigned"), {self.no_branch_2015["invoice"].pk})

    def test_a_reader_cannot_ask_for_a_branch_outside_her_reach(self):
        self.get("invoices/", f"&branch={self.lekki.pk}", user=self.adeyemi, status=400)

    def test_a_malformed_date_on_the_journal_list_is_refused_not_ignored(self):
        self.get("journals/", "&date_from=31/08/2015", status=400)

    def test_the_figures_count_the_rows_the_list_shows(self):
        query = f"&fiscal_year=2015&branch={self.ikeja.pk}"
        self.assertEqual(self.get("invoices/summary/", query)["data"]["totals"]["count"], 2)
        self.assertEqual(self.get("payments/summary/", query)["data"]["count"], 2)
        self.assertEqual(self.get("journals/summary/", query)["data"]["total"], 2)
        self.assertEqual(self.get("invoices/summary/")["data"]["totals"]["count"], 5)

    def test_a_reader_without_the_list_permission_is_still_refused(self):
        outsider = _reader(self.school, self.ikeja, "noread@bright-star-fin-scope.example.com")
        self.get("invoices/", "&fiscal_year=2015", user=outsider, status=403)


class RegisterFiguresAndOwnBranchListTests(TestCase):
    """The fixed-asset figures and the payer-payment list read the branch and the year too.

    Bright Star's register holds a minibus bought for Ikeja in 2015 and one for
    Lekki in 2016; with nothing picked the figures still count both. A payer
    payment received in 2015 and one in 2016 are listed by the year they were
    received.
    """

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import BankAccount, FixedAsset, PayerPayment

        seed_currencies()
        cls.school = make_school(slug="bright-star-fin-register", name="Bright Star School")
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=False)
        cls.books = _books(cls.school, "BSFRG")
        _year(cls.books, 2015, datetime.date(2014, 9, 1), datetime.date(2015, 8, 31))
        cls.bello = _reader(
            cls.school, cls.ikeja, "bello@bright-star-fin-register.example.com",
            "finance.fixedasset.view", "finance.payment.view",
        )
        for branch, day in ((cls.ikeja, datetime.date(2015, 2, 1)),
                            (cls.lekki, datetime.date(2015, 10, 1))):
            FixedAsset.objects.create(
                entity=cls.books, branch=branch, name="Minibus", acquisition_date=day,
                useful_life_months=60, cost=12_000_000,
            )
        gl = Account.objects.create(
            entity=cls.books, code="1190", name="Cash Ikeja",
            account_type=Account.objects.get(entity=cls.books, code="1000").account_type,
            is_postable=True,
        )
        bank = BankAccount.objects.create(
            entity=cls.books, name="Ikeja Collections", branch=cls.ikeja, gl_account=gl,
        )
        payer = _customer(cls.books, code="PAYER1")
        cls.paid_2015, cls.paid_2016 = (
            PayerPayment.objects.create(
                entity=cls.books, branch=cls.ikeja, payer=payer, bank_account=bank,
                payment_date=day, split=PayerPayment.SPLIT_CHOICES[0][0], amount=100_000,
            )
            for day in (datetime.date(2015, 4, 1), datetime.date(2015, 11, 1))
        )

    def get(self, path, query=""):
        response = TenantAPIClient(user=self.bello).get(
            f"/v1/finance/{path}?entity={self.books.code}{query}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_the_register_figures_follow_the_branch_and_the_year(self):
        self.assertEqual(self.get("fixed-assets/summary/")["cost"], 24_000_000)
        self.assertEqual(self.get("fixed-assets/summary/", "&fiscal_year=2015")["cost"], 12_000_000)
        self.assertEqual(
            self.get("fixed-assets/summary/", f"&branch={self.lekki.pk}")["cost"], 12_000_000)

    def test_payer_payments_are_listed_by_the_year_they_were_received(self):
        self.assertEqual(
            {row["id"] for row in self.get("payer-payments/", "&fiscal_year=2015")},
            {self.paid_2015.pk},
        )
        self.assertEqual(len(self.get("payer-payments/")), 2)


class ArchivedYearFinanceListTests(TestCase):
    """The receipts list puts an archived year away like the invoice list, and shows it when named."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.school = make_school(slug="harbour-fin-scope", name="Harbour Primary")
        cls.main = make_branch(cls.school, name="Main Branch")
        cls.books = _books(cls.school, "HBFSC")
        _year(cls.books, 2015, datetime.date(2015, 1, 1), datetime.date(2015, 12, 31), archived=True)
        _year(cls.books, 2026, datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        cls.tolu = _reader(cls.school, cls.main, "tolu@harbour-fin-scope.example.com", "finance.payment.view")
        customer = _customer(cls.books)
        cls.old, cls.new = (
            Payment.objects.create(
                entity=cls.books, customer=customer, branch=cls.main, payment_date=day,
                amount=50_000, status=DocumentStatus.POSTED,
            )
            for day in (datetime.date(2015, 6, 1), datetime.date(2026, 6, 1))
        )

    def listed(self, query=""):
        response = TenantAPIClient(user=self.tolu).get(
            f"/v1/finance/payments/?entity={self.books.code}{query}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in response.data["data"]}

    def test_the_default_list_leaves_the_archived_year_out(self):
        self.assertEqual(self.listed(), {self.new.pk})

    def test_naming_the_archived_year_lists_it(self):
        self.assertEqual(self.listed("&fiscal_year=2015"), {self.old.pk})


class CrossTenantFinanceScopeTests(TestCase):
    """A year is looked up only in the caller's own books."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        ours = make_school(slug="ours-fin-scope", name="Greenfield School")
        ours_main = make_branch(ours, name="Main Branch")
        cls.our_books = _books(ours, "GFFSC")
        theirs = make_school(slug="theirs-fin-scope", name="Lagoon View School")
        make_branch(theirs, name="Main Branch")
        _year(_books(theirs, "LVFSC"), 2015, datetime.date(2015, 1, 1), datetime.date(2015, 12, 31))
        cls.reader = _reader(ours, ours_main, "bursar@ours-fin-scope.example.com", "finance.invoice.view")

    def test_another_tenants_year_is_unknown_here(self):
        response = TenantAPIClient(user=self.reader).get(
            f"/v1/finance/invoices/?entity={self.our_books.code}&fiscal_year=2015",
        )
        self.assertEqual(response.status_code, 400)


class OwnBranchReadingTests(SimpleTestCase):
    """A list that reads ``?branch=`` its own way is left to it."""

    def test_a_list_declaring_its_own_branch_reading_is_not_narrowed_again(self):
        from vs_finance.list_scope import narrow_finance_document_list

        class TransferList:
            filters_own_branch = True

        request = Request(RequestFactory().get("/", {"branch": "7"}))
        queryset = Invoice.objects.all()
        narrowed = narrow_finance_document_list(queryset, request, TransferList())
        self.assertNotIn("branch_id", str(narrowed.query.where))


class DocumentDateAccessTests(SimpleTestCase):
    """The documents whose own date is a Field Access switch, found through the registry."""

    def test_each_switchable_document_date_is_found(self):
        from django.apps import apps

        from vs_finance.period_filter import date_access_keys

        expected = {
            "vs_finance.BankTransaction": "finance.banktransaction.transaction_date",
            "vs_finance.BankTransfer": "finance.banktransfer.transfer_date",
            "vs_finance.InterBranchTransfer": "finance.interbranch.transaction_date",
            "vs_finance.HeldForBranchReceipt": "finance.interbranch.transaction_date",
            "vs_finance.InterBranchRecharge": "finance.interbranch.transaction_date",
            "vs_finance.PettyCashReturn": "finance.pettycash_return.return_date",
            "vs_finance.DoubtfulDebtProvision": "finance.provision.as_of",
            "vs_finance.PayerPayment": "finance.payer_payment.payment_date",
        }
        for label, key in expected.items():
            with self.subTest(model=label):
                keys = [k for k, _ in date_access_keys(apps.get_model(label))]
                self.assertEqual(keys, [key])
        self.assertEqual(date_access_keys(Invoice), ())


class RefundsAndWriteOffsScopeTests(TestCase):
    """The combined refunds and write-offs list reads the branch and the year.

    Refunds and write-off requests are placed by their own dates. A posted
    write-off is read from the ledger record, so its branch is the written-off
    invoice's and its year the day it posted. The dunning age buckets follow
    the branch only, because they age what is owed today.
    """

    @classmethod
    def setUpTestData(cls):
        from vs_finance.constants import (
            DocumentStatus, FinanceAuditAction, FinanceAuditStatus,
        )
        from vs_finance.models import FinanceAuditLog, Refund, WriteOffRequest

        seed_currencies()
        cls.school = make_school(slug="bright-star-fin-adjust", name="Bright Star School")
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=False)
        cls.books = _books(cls.school, "BSFAD")
        _year(cls.books, 2015, datetime.date(2014, 9, 1), datetime.date(2015, 8, 31))
        _year(cls.books, 2016, datetime.date(2015, 9, 1), datetime.date(2016, 8, 31))
        cls.bello = _reader(
            cls.school, cls.ikeja, "bello@bright-star-fin-adjust.example.com",
            "finance.refund.view", "finance.writeoff.view", "finance.dunning.view",
        )
        customer = _customer(cls.books)

        def refund(branch, day):
            return Refund.objects.create(
                entity=cls.books, customer=customer, branch=branch, refund_date=day, amount=10_000,
            )

        cls.refund_ikeja_2015 = refund(cls.ikeja, datetime.date(2015, 3, 1))
        cls.refund_lekki_2015 = refund(cls.lekki, datetime.date(2015, 3, 1))
        cls.refund_ikeja_2016 = refund(cls.ikeja, datetime.date(2015, 10, 1))
        cls.invoices = {}
        for branch in (cls.ikeja, cls.lekki):
            cls.invoices[branch.pk] = Invoice.objects.create(
                entity=cls.books, customer=customer, branch=branch,
                invoice_date=datetime.date(2014, 10, 1), due_date=datetime.date(2014, 10, 31),
            )
        cls.request_ikeja = WriteOffRequest.objects.create(
            entity=cls.books, invoice=cls.invoices[cls.ikeja.pk], branch=cls.ikeja,
            write_off_date=datetime.date(2015, 5, 1), status=DocumentStatus.PENDING_APPROVAL,
        )
        cls.request_lekki = WriteOffRequest.objects.create(
            entity=cls.books, invoice=cls.invoices[cls.lekki.pk], branch=cls.lekki,
            write_off_date=datetime.date(2015, 5, 1),
        )

        def posted(branch, when):
            entry = FinanceAuditLog.objects.create(
                entity=cls.books, action=FinanceAuditAction.INVOICE_WRITTEN_OFF,
                status=FinanceAuditStatus.SUCCESS, target_type="Invoice",
                target_id=str(cls.invoices[branch.pk].pk), metadata={"amount": 5_000},
                created_at=when,
            )
            return entry

        utc = datetime.timezone.utc
        cls.posted_ikeja_2015 = posted(cls.ikeja, datetime.datetime(2015, 6, 1, 12, tzinfo=utc))
        cls.posted_lekki_2015 = posted(cls.lekki, datetime.datetime(2015, 6, 1, 12, tzinfo=utc))
        cls.posted_ikeja_2016 = posted(cls.ikeja, datetime.datetime(2016, 1, 10, 12, tzinfo=utc))

    def get(self, path, query="", status=200):
        response = TenantAPIClient(user=self.bello).get(
            f"/v1/finance/{path}?entity={self.books.code}{query}",
        )
        self.assertEqual(response.status_code, status, getattr(response, "data", None))
        return response.data

    def keys(self, query=""):
        return {row["key"] for row in self.get("ar-adjustments/", query)["data"]}

    def test_branch_and_year_together_keep_that_branchs_rows_in_that_year(self):
        self.assertEqual(self.keys(f"&branch={self.ikeja.pk}&fiscal_year=2015"), {
            f"R{self.refund_ikeja_2015.pk}", f"WR{self.request_ikeja.pk}",
            f"W{self.posted_ikeja_2015.pk}",
        })

    def test_a_posted_write_off_is_placed_by_the_day_it_posted(self):
        self.assertEqual(self.keys("&fiscal_year=2016"), {
            f"R{self.refund_ikeja_2016.pk}", f"W{self.posted_ikeja_2016.pk}",
        })

    def test_a_posted_write_off_follows_its_invoices_branch(self):
        self.assertEqual(self.keys(f"&branch={self.lekki.pk}"), {
            f"R{self.refund_lekki_2015.pk}", f"WR{self.request_lekki.pk}",
            f"W{self.posted_lekki_2015.pk}",
        })

    def test_nothing_picked_still_lists_every_row(self):
        self.assertEqual(len(self.keys()), 8)

    def test_the_figures_follow_the_branch(self):
        self.assertEqual(self.get("ar-adjustments/", f"&branch={self.ikeja.pk}")["kpis"]["pending"], 1)
        self.assertEqual(self.get("ar-adjustments/", f"&branch={self.lekki.pk}")["kpis"]["pending"], 0)

    def test_the_written_off_figure_counts_the_picked_years_posted_write_offs(self):
        kpis = self.get("ar-adjustments/", "&fiscal_year=2015")["kpis"]
        self.assertEqual(kpis["written_off_ytd"], 10_000)

    def test_a_malformed_date_is_refused(self):
        self.get("ar-adjustments/", "&date_from=01/05/2015", status=400)

    def test_the_dunning_buckets_follow_the_branch_and_ignore_the_year(self):
        from django.utils import timezone

        from vs_finance.constants import DocumentStatus

        due = timezone.localdate() - datetime.timedelta(days=10)
        Invoice.objects.filter(pk__in=[i.pk for i in self.invoices.values()]).update(
            status=DocumentStatus.POSTED, total=50_000, due_date=due,
        )
        everyone = self.get("dunning/summary/")["data"]["overdue_1_30"]["count"]
        lekki = self.get("dunning/summary/", f"&branch={self.lekki.pk}")["data"]["overdue_1_30"]["count"]
        lekki_2015 = self.get(
            "dunning/summary/", f"&branch={self.lekki.pk}&fiscal_year=2015",
        )["data"]["overdue_1_30"]["count"]
        self.assertEqual((everyone, lekki, lekki_2015), (2, 1, 1))
