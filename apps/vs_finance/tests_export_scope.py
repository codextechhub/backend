"""An export answers for the branch and the year its list was showing.

Bright Star School runs Ikeja and Lekki on a September to August year, so
FY2015 runs from 1 September 2014 to 31 August 2015. Mrs Bello keeps the books
for both branches. She filters the invoice list to Ikeja and FY2015 and presses
Export: the file holds Ikeja's 2015 invoices and nothing else, and says so.
Mrs Adeyemi works in Ikeja alone and cannot export Lekki's. Harbour Primary has
one branch, and the builder offers it no Branch filter at all.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_exports.catalogue import (
    FilterError,
    ScopeContext,
    all_datasets,
    compile_filter,
    describe_filter,
    get_dataset,
)
from vs_finance.models import Invoice, JournalEntry
from vs_finance.period_filter import date_access_keys
from vs_finance.retention import document_date_field
from vs_finance.seed import seed_currencies
from vs_finance.tests_list_scope import _books, _customer, _reader, _year
from vs_rbac.tests.helpers import make_branch, make_school

KEYS = ("finance.invoice.view", "finance.journal.view", "exports.catalogue.view")


class ScreenExportScopeTests(TestCase):
    """The Export button carries the list's branch and year into the file."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.school = make_school(slug="bright-star-exp-scope", name="Bright Star School")
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=False)
        cls.books = _books(cls.school, "BSEXS")
        _year(cls.books, 2015, datetime.date(2014, 9, 1), datetime.date(2015, 8, 31))
        _year(cls.books, 2016, datetime.date(2015, 9, 1), datetime.date(2016, 8, 31))
        cls.bello = _reader(cls.school, cls.ikeja, "bello@bright-star-exp-scope.example.com", *KEYS)
        cls.adeyemi = _reader(
            cls.school, cls.ikeja, "adeyemi@bright-star-exp-scope.example.com", *KEYS,
            pinned_to=cls.ikeja,
        )
        customer = _customer(cls.books)

        def invoice(branch, day):
            return Invoice.objects.create(
                entity=cls.books, customer=customer, branch=branch, invoice_date=day, due_date=day,
            )

        cls.ikeja_2014 = invoice(cls.ikeja, datetime.date(2014, 10, 2))
        cls.ikeja_2015 = invoice(cls.ikeja, datetime.date(2015, 8, 31))
        cls.ikeja_2016 = invoice(cls.ikeja, datetime.date(2015, 9, 1))
        cls.lekki_2015 = invoice(cls.lekki, datetime.date(2015, 3, 9))
        cls.no_branch_2015 = invoice(None, datetime.date(2015, 5, 5))
        for day in (datetime.date(2015, 8, 30), datetime.date(2015, 9, 2)):
            JournalEntry.objects.create(entity=cls.books, branch=cls.ikeja, date=day)

    def from_screen(self, screen, query, *, user=None, status=200):
        response = TenantAPIClient(user=user or self.bello).get(
            f"/v1/exports/from-screen/?screen={screen}&entity={self.books.code}{query}",
        )
        self.assertEqual(response.status_code, status, getattr(response, "data", None))
        return response.data

    def listed(self, query):
        response = TenantAPIClient(user=self.bello).get(
            f"/v1/finance/invoices/?entity={self.books.code}{query}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in response.data["data"]}

    def test_the_file_holds_the_rows_the_list_shows_for_a_branch_and_a_year(self):
        query = f"&fiscal_year=2015&branch={self.ikeja.pk}"
        data = self.from_screen("finance.invoices", query)["data"]

        self.assertIn(
            {"id": "invoice_date", "start": "2014-09-01", "end": "2015-08-31"},
            data["config"]["filters"],
        )
        self.assertIn({"id": "branch", "values": [str(self.ikeja.pk)]}, data["config"]["filters"])
        self.assertTrue(data["exact"])
        self.assertIsNone(data["date_window"])
        self.assertEqual(data["matching_rows"], len(self.listed(query)))
        self.assertEqual(data["matching_rows"], 2)

    def test_the_documents_not_yet_given_a_branch_export_alone(self):
        data = self.from_screen("finance.invoices", "&branch=unassigned")["data"]
        self.assertEqual(data["matching_rows"], 1)

    def test_a_year_the_books_do_not_have_is_refused_in_the_lists_words(self):
        refused = self.from_screen("finance.invoices", "&fiscal_year=1999", status=400)
        self.assertIn("There is no financial year 1999", str(refused))

    def test_a_branch_outside_the_readers_reach_is_refused(self):
        self.from_screen(
            "finance.invoices", f"&branch={self.lekki.pk}", user=self.adeyemi, status=400,
        )

    def entry_dates(self, query):
        data = self.from_screen("finance.gl_postings", query)["data"]
        self.assertIsNone(data["date_window"])
        self.assertTrue(data["exact"])
        return next(f for f in data["config"]["filters"] if f["id"] == "entry_date")

    def test_an_open_end_on_the_ledger_runs_to_today_and_adds_no_window_of_its_own(self):
        from vs_config.clock import tenant_today

        self.assertEqual(self.entry_dates("&date_from=2015-09-01"), {
            "id": "entry_date", "start": "2015-09-01",
            "end": tenant_today(self.school.tenant).isoformat(),
        })

    def test_an_open_start_begins_at_the_first_document_in_reach(self):
        data = self.from_screen("finance.invoices", "&date_to=2015-06-30")["data"]
        self.assertIn(
            {"id": "invoice_date", "start": "2014-10-02", "end": "2015-06-30"},
            data["config"]["filters"],
        )
        self.assertEqual(data["matching_rows"], len(self.listed("&date_to=2015-06-30")))
        self.assertEqual(data["matching_rows"], 3)

    def test_a_malformed_date_on_the_ledger_is_refused_not_dropped(self):
        self.from_screen("finance.gl_postings", "&date_from=01/09/2015", status=400)

    def test_without_a_period_the_file_still_covers_every_row_the_list_shows(self):
        from vs_config.clock import tenant_today

        ahead = tenant_today(self.school.tenant) + datetime.timedelta(days=30)
        Invoice.objects.create(
            entity=self.books, customer=self.ikeja_2014.customer, branch=self.ikeja,
            invoice_date=ahead, due_date=ahead,
        )
        data = self.from_screen("finance.invoices", "")["data"]
        self.assertTrue(data["date_window"]["whole_list"])
        self.assertEqual(data["date_window"]["end"], ahead.isoformat())
        self.assertEqual(data["matching_rows"], 6)


class ArchivedYearExportTests(TestCase):
    """With no year picked, the file puts away the archived years the list puts away.

    Harbour Primary archived FY2015 (the calendar year). Its 2015 paid invoice
    is put away; the 2015 invoice still owed stays, as it does on the list.
    """

    @classmethod
    def setUpTestData(cls):
        from vs_finance.constants import DocumentStatus, InvoicePaymentStatus

        seed_currencies()
        school = make_school(slug="harbour-exp-archive", name="Harbour Primary")
        main = make_branch(school, name="Main Branch")
        cls.books = _books(school, "HBEXA")
        _year(cls.books, 2015, datetime.date(2015, 1, 1), datetime.date(2015, 12, 31), archived=True)
        _year(cls.books, 2026, datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        cls.tolu = _reader(school, main, "tolu@harbour-exp-archive.example.com", *KEYS)
        customer = _customer(cls.books)

        def invoice(day, payment_status):
            return Invoice.objects.create(
                entity=cls.books, customer=customer, branch=main, invoice_date=day, due_date=day,
                status=DocumentStatus.POSTED, payment_status=payment_status,
            )

        cls.paid_2015 = invoice(datetime.date(2015, 2, 1), InvoicePaymentStatus.PAID)
        cls.owed_2015 = invoice(datetime.date(2015, 6, 1), InvoicePaymentStatus.UNPAID)
        cls.paid_2026 = invoice(datetime.date(2026, 3, 1), InvoicePaymentStatus.PAID)

    def exported(self, query):
        response = TenantAPIClient(user=self.tolu).get(
            f"/v1/exports/from-screen/?screen=finance.invoices&entity={self.books.code}{query}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def listed(self, query):
        response = TenantAPIClient(user=self.tolu).get(
            f"/v1/finance/invoices/?entity={self.books.code}{query}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in response.data["data"]}

    def test_the_default_file_leaves_the_archived_year_out_as_the_list_does(self):
        data = self.exported("")
        self.assertIn({"id": "hide_archived", "value": True}, data["config"]["filters"])
        self.assertTrue(data["exact"])
        self.assertEqual(self.listed(""), {self.owed_2015.pk, self.paid_2026.pk})
        self.assertEqual(data["matching_rows"], 2)

    def test_the_window_opens_on_the_first_invoice_the_file_keeps(self):
        self.assertEqual(self.exported("")["date_window"]["start"], "2015-06-01")

    def test_show_archived_puts_the_year_back_in_the_file(self):
        data = self.exported("&include_archived=true")
        self.assertNotIn("hide_archived", {f["id"] for f in data["config"]["filters"]})
        self.assertEqual(data["matching_rows"], len(self.listed("&include_archived=true")))
        self.assertEqual(data["matching_rows"], 3)

    def test_naming_the_archived_year_exports_all_of_it(self):
        data = self.exported("&fiscal_year=2015")
        self.assertNotIn("hide_archived", {f["id"] for f in data["config"]["filters"]})
        self.assertEqual(data["matching_rows"], len(self.listed("&fiscal_year=2015")))
        self.assertEqual(data["matching_rows"], 2)


class BranchFilterInTheBuilderTests(TestCase):
    """The builder offers a Branch filter only where there is a branch to choose."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.school = make_school(slug="bright-star-exp-builder", name="Bright Star School")
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=False)
        cls.books = _books(cls.school, "BSEXB")
        cls.bello = _reader(cls.school, cls.ikeja, "bello@bright-star-exp-builder.example.com", *KEYS)
        cls.adeyemi = _reader(
            cls.school, cls.ikeja, "adeyemi@bright-star-exp-builder.example.com", *KEYS,
            pinned_to=cls.ikeja,
        )
        harbour = make_school(slug="harbour-exp-builder", name="Harbour Primary")
        main = make_branch(harbour, name="Main Branch")
        cls.harbour_books = _books(harbour, "HBEXB")
        cls.tolu = _reader(harbour, main, "tolu@harbour-exp-builder.example.com", *KEYS)

    def branch_choices(self, user, books):
        response = TenantAPIClient(user=user).get(
            f"/v1/exports/catalogue/finance.customer_invoices/?entity={books.code}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        branch = [f for f in response.data["data"]["filters"] if f["id"] == "branch"]
        return [c["label"] for c in branch[0]["choices"]] if branch else None

    def test_a_whole_school_reader_picks_any_branch_or_the_unassigned(self):
        self.assertEqual(
            self.branch_choices(self.bello, self.books),
            ["Ikeja Branch", "Lekki Branch", "Not yet given a branch"],
        )

    def test_a_reader_of_one_branch_is_offered_no_branch_filter(self):
        self.assertIsNone(self.branch_choices(self.adeyemi, self.books))

    def test_a_school_with_one_branch_is_offered_no_branch_filter(self):
        self.assertIsNone(self.branch_choices(self.tolu, self.harbour_books))

    def test_the_review_sentence_names_the_branch(self):
        dataset = get_dataset("finance.customer_invoices")
        spec = {"id": "branch", "values": [str(self.ikeja.pk)]}
        self.assertEqual(
            describe_filter(dataset, spec, tenant=self.school.tenant),
            "Branch is any of Ikeja Branch",
        )

    def test_a_saved_value_is_judged_by_its_shape_before_it_is_run(self):
        dataset = get_dataset("finance.customer_invoices")
        compile_filter(dataset, {"id": "branch", "values": ["12", "unassigned"]})
        with self.assertRaises(FilterError):
            compile_filter(dataset, {"id": "branch", "values": ["Ikeja"]})

    def test_another_schools_branch_is_refused_when_the_export_runs(self):
        dataset = get_dataset("finance.customer_invoices")
        harbour_branch = self.harbour_books.tenant.branches.get()
        scope = ScopeContext(tenant=self.school.tenant, entity=self.books, user=self.bello)
        with self.assertRaises(FilterError):
            compile_filter(dataset, {"id": "branch", "values": [str(harbour_branch.pk)]}, scope)


class ExportPeriodPlacesTheDocumentLikeItsListTests(TestCase):
    """Every dataset that reads a list's year cuts it on the date the list cuts it on."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        school = make_school(slug="greenfield-exp-period", name="Greenfield School")
        make_branch(school, name="Main Branch")
        cls.books = _books(school, "GFEXP")

    def test_each_period_dataset_filters_its_documents_own_date_and_its_branch(self):
        scope = ScopeContext(tenant=self.books.tenant, entity=self.books)
        period_datasets = [d for d in all_datasets() if d.screen_period is not None]
        self.assertTrue(
            {"finance.customer_invoices", "finance.gl_postings", "finance.customer_receipts",
             "finance.expense_claims", "procurement.purchase_orders",
             "procurement.vendor_invoices", "procurement.requisitions"}
            <= {d.key for d in period_datasets},
        )
        for dataset in period_datasets:
            with self.subTest(dataset=dataset.key):
                [spec] = dataset.screen_period({"date_from": "2015-01-01"}, scope)
                path = dataset.filter_def(spec["id"]).path
                model = dataset.base(scope).model
                *route, date = path.split("__")
                for step in route:
                    model = model._meta.get_field(step).related_model
                self.assertEqual(date, document_date_field(model))
                column = next(f for f in dataset.fields if f.path == path)
                self.assertEqual(
                    {key for key, _ in date_access_keys(model)}, {column.access} - {""},
                    "A switchable document date needs the same switch on its export column.",
                )
                self.assertIsNotNone(dataset.filter_def("hide_archived"))
                dataset.base(scope).filter(
                    compile_filter(dataset, {"id": "hide_archived", "value": True}, scope)).count()
                branch = dataset.filter_def("branch")
                self.assertIsNotNone(branch)
                self.assertEqual(branch.source, "__".join([*route, "branch"]))

    def test_the_customer_master_is_not_a_document_and_reads_no_year(self):
        self.assertIsNone(get_dataset("finance.customers").screen_period)
        self.assertIsNone(get_dataset("finance.customers").filter_def("branch"))
