"""Procurement document lists narrowed to a fiscal year or a date range.

Bright Star School runs Ikeja and Lekki and keeps its books on a September to
August year, so FY2015 runs from 1 September 2014 to 31 August 2015. Mrs Bello
buys for both branches. She asks for Ikeja's 2015 requisitions and gets exactly
those: not Lekki's, not Ikeja's 2016 ones, and not the calendar year 2015.
Harbour Primary has one branch and no branch filter at all, and its years
narrow the same way.
"""
from __future__ import annotations

import datetime

from django.test import TestCase
from django.utils import timezone

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus
from vs_finance.models import FiscalYear, LedgerEntity
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

from .models import PurchaseOrder, PurchaseOrderLine, PurchaseRequisition, RequestForQuotation, Vendor


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


def _buyer(school, branch, email, *keys):
    role = make_role(school, name=f"Buyer {email}", key=f"buyer-{email.split('@')[0]}")
    for key in keys:
        make_role_permission(role, make_permission(key))
    user = make_school_admin(branch, email=email)
    make_assignment(school, user, role)
    return user


class DocumentPeriodListTests(TestCase):
    """A requisition list answers for one fiscal year or one range of dates."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.school = make_school(slug="bright-star-period", name="Bright Star School")
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=False)
        cls.books = _books(cls.school, "BSPRD")
        _year(cls.books, 2015, datetime.date(2014, 9, 1), datetime.date(2015, 8, 31))
        _year(cls.books, 2016, datetime.date(2015, 9, 1), datetime.date(2016, 8, 31))
        cls.bello = _buyer(
            cls.school, cls.ikeja, "bello@bright-star-period.example.com",
            "procurement.requisition.view",
        )

        def raise_(branch, title, day, status=DocumentStatus.DRAFT):
            return PurchaseRequisition.objects.create(
                entity=cls.books, branch=branch, title=title, request_date=day, status=status,
            )

        cls.ikeja_autumn_2014 = raise_(cls.ikeja, "Chalk", datetime.date(2014, 10, 2))
        cls.ikeja_summer_2015 = raise_(cls.ikeja, "Desks", datetime.date(2015, 8, 31))
        cls.ikeja_autumn_2015 = raise_(cls.ikeja, "Paper", datetime.date(2015, 9, 1))
        cls.lekki_spring_2015 = raise_(cls.lekki, "Chairs", datetime.date(2015, 3, 9))

    def listed(self, query="", *, user=None, status=200):
        response = TenantAPIClient(user=user or self.bello).get(
            f"/v1/procurement/requisitions/?entity={self.books.code}{query}",
        )
        self.assertEqual(response.status_code, status, getattr(response, "data", None))
        return {row["id"] for row in response.data["data"]} if status == 200 else response.data

    def test_a_fiscal_year_lists_the_documents_its_own_dates_cover(self):
        self.assertEqual(self.listed("&fiscal_year=2015"), {
            self.ikeja_autumn_2014.pk, self.ikeja_summer_2015.pk, self.lekki_spring_2015.pk,
        })

    def test_the_year_ends_on_its_own_last_day_not_on_the_calendar(self):
        self.assertEqual(self.listed("&fiscal_year=2016"), {self.ikeja_autumn_2015.pk})

    def test_branch_and_year_together_list_only_that_branch_in_that_year(self):
        self.assertEqual(
            self.listed(f"&fiscal_year=2015&branch={self.ikeja.pk}"),
            {self.ikeja_autumn_2014.pk, self.ikeja_summer_2015.pk},
        )

    def test_a_date_range_is_inclusive_at_both_ends(self):
        self.assertEqual(
            self.listed("&date_from=2015-03-09&date_to=2015-08-31"),
            {self.lekki_spring_2015.pk, self.ikeja_summer_2015.pk},
        )

    def test_either_end_of_a_range_may_be_left_open(self):
        self.assertEqual(self.listed("&date_from=2015-08-31"), {
            self.ikeja_summer_2015.pk, self.ikeja_autumn_2015.pk,
        })
        self.assertEqual(self.listed("&date_to=2014-12-31"), {self.ikeja_autumn_2014.pk})

    def test_no_period_still_lists_every_year(self):
        self.assertEqual(len(self.listed()), 4)

    def test_a_year_the_books_do_not_have_is_refused_not_answered_empty(self):
        body = self.listed("&fiscal_year=2009", status=400)
        self.assertIn("fiscal_year", str(body))

    def test_a_year_and_a_range_together_are_refused(self):
        body = self.listed("&fiscal_year=2015&date_from=2015-01-01", status=400)
        self.assertIn("not both", str(body))

    def test_a_date_that_is_not_a_date_is_refused(self):
        self.assertIn("YYYY-MM-DD", str(self.listed("&date_from=09/03/2015", status=400)))

    def test_a_range_that_ends_before_it_starts_is_refused(self):
        body = self.listed("&date_from=2015-08-31&date_to=2015-01-01", status=400)
        self.assertIn("on or after", str(body))

    def test_a_reader_without_the_list_permission_is_still_refused(self):
        outsider = _buyer(self.school, self.ikeja, "noread@bright-star-period.example.com")
        self.listed("&fiscal_year=2015", user=outsider, status=403)

    def test_the_summary_counts_the_same_period_the_list_shows(self):
        response = TenantAPIClient(user=self.bello).get(
            f"/v1/procurement/requisitions/summary/?entity={self.books.code}"
            f"&fiscal_year=2015&branch={self.ikeja.pk}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["draft"]["count"], 2)


class OrderAndSourcingPeriodTests(TestCase):
    """Orders and requests for quotation answer the same period, figures included.

    Mrs Bello's FY2015 Ikeja order is still awaiting delivery; Lekki's order is
    from the same year, and Ikeja's other order is from FY2016.
    """

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.school = make_school(slug="bright-star-orders", name="Bright Star School")
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=False)
        cls.books = _books(cls.school, "BSORD")
        _year(cls.books, 2015, datetime.date(2014, 9, 1), datetime.date(2015, 8, 31))
        _year(cls.books, 2016, datetime.date(2015, 9, 1), datetime.date(2016, 8, 31))
        cls.bello = _buyer(
            cls.school, cls.ikeja, "bello@bright-star-orders.example.com",
            "procurement.purchase_order.view", "procurement.rfq.view",
        )
        vendor = Vendor.objects.create(entity=cls.books, code="ADE", name="Ade Stationers")
        expense = cls.books.accounts.filter(account_type="EXPENSE").first()

        def order(branch, day):
            po = PurchaseOrder.objects.create(
                entity=cls.books, branch=branch, vendor=vendor, order_date=day,
                status=DocumentStatus.APPROVED,
            )
            PurchaseOrderLine.objects.create(
                purchase_order=po, description="Desks", expense_account=expense,
                quantity=1, unit_price=500_000, net_amount=500_000, line_no=1,
            )
            return po

        cls.ikeja_2015 = order(cls.ikeja, datetime.date(2015, 2, 3))
        cls.lekki_2015 = order(cls.lekki, datetime.date(2015, 4, 5))
        cls.ikeja_2016 = order(cls.ikeja, datetime.date(2015, 10, 6))
        for branch, day in ((cls.ikeja, datetime.date(2015, 1, 9)), (cls.ikeja, datetime.date(2016, 1, 9))):
            RequestForQuotation.objects.create(entity=cls.books, branch=branch, issue_date=day)

    def get(self, path):
        response = TenantAPIClient(user=self.bello).get(f"/v1/procurement/{path}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_the_order_list_answers_branch_and_year_together(self):
        data = self.get(f"purchase-orders/?entity={self.books.code}&fiscal_year=2015&branch={self.ikeja.pk}")
        self.assertEqual({row["id"] for row in data["data"]}, {self.ikeja_2015.pk})

    def test_the_order_figures_count_the_same_orders_as_the_list(self):
        data = self.get(f"purchase-orders/summary/?entity={self.books.code}&fiscal_year=2015")
        self.assertEqual(data["data"]["open"]["count"], 2)
        data = self.get(f"purchase-orders/summary/?entity={self.books.code}&fiscal_year=2015&branch={self.ikeja.pk}")
        self.assertEqual(data["data"]["open"]["count"], 1)

    def test_the_sourcing_figures_count_the_year_asked_for(self):
        data = self.get(f"rfqs/summary/?entity={self.books.code}&fiscal_year=2015")
        self.assertEqual(data["data"]["draft"], 1)
        data = self.get(f"rfqs/summary/?entity={self.books.code}")
        self.assertEqual(data["data"]["draft"], 2)


class ArchivedYearPeriodTests(TestCase):
    """Naming an archived year shows it; the default list still puts it away."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.school = make_school(slug="harbour-period", name="Harbour Primary")
        cls.main = make_branch(cls.school, name="Main Branch")
        cls.books = _books(cls.school, "HBPRD")
        _year(cls.books, 2015, datetime.date(2015, 1, 1), datetime.date(2015, 12, 31), archived=True)
        _year(cls.books, 2026, datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        cls.tolu = _buyer(
            cls.school, cls.main, "tolu@harbour-period.example.com", "procurement.requisition.view",
        )
        cls.old = PurchaseRequisition.objects.create(
            entity=cls.books, branch=cls.main, title="Old desks", request_date=datetime.date(2015, 6, 1),
        )
        cls.new = PurchaseRequisition.objects.create(
            entity=cls.books, branch=cls.main, title="New desks", request_date=datetime.date(2026, 6, 1),
        )

    def listed(self, query=""):
        response = TenantAPIClient(user=self.tolu).get(
            f"/v1/procurement/requisitions/?entity={self.books.code}{query}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in response.data["data"]}

    def test_the_default_list_leaves_the_archived_year_out(self):
        self.assertEqual(self.listed(), {self.new.pk})

    def test_naming_the_archived_year_lists_it(self):
        self.assertEqual(self.listed("&fiscal_year=2015"), {self.old.pk})

    def test_a_range_across_the_archived_year_lists_it_too(self):
        self.assertEqual(self.listed("&date_from=2015-01-01"), {self.old.pk, self.new.pk})


class CrossTenantPeriodTests(TestCase):
    """A year is looked up only in the caller's own books."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.ours = make_school(slug="ours-period", name="Greenfield School")
        cls.ours_main = make_branch(cls.ours, name="Main Branch")
        cls.our_books = _books(cls.ours, "GFPRD")
        cls.theirs = make_school(slug="theirs-period", name="Lagoon View School")
        lagoon_main = make_branch(cls.theirs, name="Main Branch")
        their_books = _books(cls.theirs, "LVPRD")
        _year(their_books, 2015, datetime.date(2015, 1, 1), datetime.date(2015, 12, 31))
        PurchaseRequisition.objects.create(
            entity=their_books, branch=lagoon_main, title="Theirs", request_date=datetime.date(2015, 5, 5),
        )
        cls.buyer = _buyer(
            cls.ours, cls.ours_main, "buyer@ours-period.example.com", "procurement.requisition.view",
        )

    def test_another_tenants_year_is_unknown_here(self):
        response = TenantAPIClient(user=self.buyer).get(
            f"/v1/procurement/requisitions/?entity={self.our_books.code}&fiscal_year=2015",
        )
        self.assertEqual(response.status_code, 400)

    def test_another_tenants_documents_never_answer_a_range(self):
        response = TenantAPIClient(user=self.buyer).get(
            f"/v1/procurement/requisitions/?entity={self.our_books.code}&date_from=2015-01-01",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], [])
