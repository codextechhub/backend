"""Lookups that restate other documents answer for the caller's branches only.

Corona's two branches buy from the same school-wide vendor, Acme Supplies.
Ikeja ordered 5 desks at 20,000 on the cleaning contract; Lekki ordered 10
chairs at 10,000 and 4 lamps. The vendor, the catalogue entry and the contract
are shared, but the orders, bills and plans hanging off them each belong to one
branch, so a lookup that starts from the shared record must not carry another
branch's figures with it:

* the supplier-invoice reference check, when Lekki's Acme bill and Ikeja's
  Prime Uniforms bill both carry the number SHARED-1;
* the requisition budget check, when Ikeja's plan is 600,000 and Lekki's is
  300,000 for the same IT department;
* the catalogue item's purchasing history and the contract's linked orders.

A reader covering the whole school sees all of it; a school with one branch
sees all of it without the dimension being mentioned; another school sees
none of it.
"""
from __future__ import annotations

import datetime

from django.utils import timezone

from vs_finance.constants import BudgetStatus
from vs_finance.models import Budget, BudgetLine, CostCenter

from .models import (
    CatalogItem,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    VendorInvoice,
)
from .tests_dashboard_overview import _OverviewFixture


class _ReachFixture(_OverviewFixture):
    """The overview school with a department, plans, a catalogue entry and three readers."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        e = cls.multi.entity
        cls.cost_center = CostCenter.objects.create(entity=e, code="IT", name="IT department")
        cls.item = CatalogItem.objects.create(entity=e, code="DESK", name="Desk")
        for po in (cls.chairs, cls.desks):
            requisition = PurchaseRequisition.objects.create(
                entity=e, branch=po.branch, request_date=datetime.date(2026, 1, 2),
                status="APPROVED",
            )
            wanted = PurchaseRequisitionLine.objects.create(
                requisition=requisition, catalog_item=cls.item, description="item",
                quantity=1, estimated_unit_price=1_000, line_no=1,
            )
            line = po.lines.get()
            line.requisition_line = wanted
            line.cost_center = cls.cost_center
            line.save(update_fields=["requisition_line", "cost_center"])
        cls.ikeja_plan = cls.plan(cls.ikeja, "Ikeja plan", 600_000, days_ago=1)
        cls.plan(cls.ikeja, "Ikeja first draft", 500_000, days_ago=9)
        cls.lekki_plan = cls.plan(cls.lekki, "Lekki plan", 300_000, days_ago=2)

        # The bills that carry the number being checked.
        cls.lekki_bill = VendorInvoice.objects.get(
            vendor=cls.multi.vendor, branch=cls.lekki)
        cls.ikeja_other_bill = VendorInvoice.objects.get(vendor=cls.held)
        VendorInvoice.objects.filter(pk=cls.lekki_bill.pk).update(vendor_reference="SHARED-1")
        VendorInvoice.objects.filter(pk=cls.ikeja_other_bill.pk).update(
            vendor_reference="SHARED-1")

        cls.ikeja_reader = cls.reader("ikeja.reach@t.com", "reach-ikeja", cls.ikeja)
        cls.lekki_reader = cls.reader("lekki.reach@t.com", "reach-lekki", cls.lekki)
        cls.head = cls.reader("head.reach@t.com", "reach-head", None)

    @classmethod
    def plan(cls, branch, name, amount, *, days_ago):
        budget = Budget.objects.create(
            entity=cls.multi.entity, branch=branch, fiscal_year=cls.cost_center.entity.fiscal_years.get(),
            name=name, status=BudgetStatus.APPROVED,
            approved_at=timezone.now() - datetime.timedelta(days=days_ago),
        )
        BudgetLine.objects.create(
            budget=budget, account=cls.acc(cls.multi.entity, "5300"),
            cost_center=cls.cost_center, period_no=1, amount=amount,
        )
        return budget

    @classmethod
    def reader(cls, email, role_key, branch):
        user = cls.user_for(cls.multi_tenant, email)
        for key in (
            "procurement.vendor_invoice.view", "procurement.requisition.view",
            "procurement.report.view", "procurement.purchase_order.view",
            "procurement.vendor.view", "procurement.contract.view",
        ):
            cls.grant(user, key, tenant=cls.multi_tenant, role_key=role_key, branch=branch)
        return user

    def fetch(self, user, path, books=None):
        books = books or self.multi
        sep = "&" if "?" in path else "?"
        client = self.client_as(user)
        response = client.get(f"/v1/procurement/{path}{sep}entity={books.entity.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()["data"]


class VendorInvoiceReferenceCheckReachTests(_ReachFixture):
    """SHARED-1 is on Lekki's Acme bill and on Ikeja's Prime Uniforms bill."""

    def check(self, user, vendor="ACME", books=None):
        return self.fetch(
            user, f"vendor-invoices/reference-check/?vendor={vendor}&reference=shared-1", books)

    def test_a_branch_reader_is_told_only_about_bills_their_branch_holds(self):
        ikeja = self.check(self.ikeja_reader)
        lekki = self.check(self.lekki_reader)

        self.assertIsNone(ikeja["same_vendor_duplicate"])
        self.assertEqual([m["id"] for m in ikeja["other_vendor_matches"]],
                         [self.ikeja_other_bill.pk])
        self.assertEqual(ikeja["other_vendor_match_count"], 1)
        self.assertEqual(lekki["same_vendor_duplicate"]["id"], self.lekki_bill.pk)
        self.assertEqual(lekki["other_vendor_matches"], [])
        self.assertEqual(lekki["other_vendor_match_count"], 0)

    def test_a_whole_school_reader_is_told_about_every_branchs_bills(self):
        head = self.check(self.head)

        self.assertEqual(head["same_vendor_duplicate"]["id"], self.lekki_bill.pk)
        self.assertEqual(head["other_vendor_match_count"], 1)

    def test_a_bill_in_the_callers_branch_is_found_and_none_of_another_is_added(self):
        data = self.check(self.ikeja_reader, vendor="HELD")

        self.assertEqual(data["same_vendor_duplicate"]["id"], self.ikeja_other_bill.pk)
        self.assertEqual(data["other_vendor_matches"], [])
        self.assertEqual(data["other_vendor_match_count"], 0)

    def test_another_schools_bills_never_match(self):
        VendorInvoice.objects.create(
            entity=self.foreign.entity, vendor=self.foreign.vendor,
            invoice_date=datetime.date(2026, 1, 10), vendor_reference="SHARED-1",
        )

        self.assertEqual(self.check(self.head)["other_vendor_match_count"], 1)


class RequisitionBudgetReachTests(_ReachFixture):
    """Ikeja plans 600,000 and has committed 100,000; Lekki plans 300,000 and has committed 100,000."""

    def budget(self, user, extra="", books=None):
        return self.fetch(
            user,
            f"requisitions/budget-availability/?cost_center=IT&date=2026-01-15{extra}",
            books,
        )

    def test_a_branch_reader_is_measured_against_their_own_plan_and_orders(self):
        ikeja = self.budget(self.ikeja_reader)
        lekki = self.budget(self.lekki_reader)

        self.assertEqual((ikeja["budget"], ikeja["committed"], ikeja["available"]),
                         (600_000, 100_000, 500_000))
        self.assertEqual(ikeja["period"], "Ikeja plan")
        self.assertEqual((lekki["budget"], lekki["committed"], lekki["available"]),
                         (300_000, 100_000, 200_000))
        self.assertEqual(lekki["period"], "Lekki plan")

    def test_the_latest_approved_plan_of_a_branch_is_its_budget(self):
        self.assertEqual(self.budget(self.ikeja_reader)["budget"], 600_000)

    def test_a_whole_school_reader_adds_the_branches_up(self):
        head = self.budget(self.head)

        self.assertEqual((head["budget"], head["committed"], head["available"]),
                         (900_000, 200_000, 700_000))
        self.assertEqual(head["period"], "FY2026")
        self.assertTrue(head["has_budget"])

    def test_a_whole_school_reader_may_narrow_to_one_branch(self):
        head = self.budget(self.head, extra=f"&branch={self.lekki.pk}")

        self.assertEqual((head["budget"], head["committed"]), (300_000, 100_000))

    def test_a_branch_reader_cannot_widen_the_answer_with_the_filter(self):
        data = self.budget(self.ikeja_reader, extra=f"&branch={self.lekki.pk}")

        self.assertEqual((data["budget"], data["committed"]), (0, 0))
        self.assertFalse(data["has_budget"])

    def test_a_branch_without_a_plan_has_no_budget_not_a_neighbours(self):
        Budget.objects.filter(pk=self.lekki_plan.pk).delete()

        lekki = self.budget(self.lekki_reader)

        self.assertFalse(lekki["has_budget"])
        self.assertEqual((lekki["budget"], lekki["committed"], lekki["available"]),
                         (0, 100_000, -100_000))

    def test_another_schools_plan_is_never_reachable(self):
        response = self.client_as(self.head).get(
            f"/v1/procurement/requisitions/budget-availability/"
            f"?entity={self.foreign.entity.code}&cost_center=IT&date=2026-01-15")

        self.assertIn(response.status_code, (403, 404))

    def test_a_school_with_one_branch_reads_its_whole_plan_and_orders(self):
        from vs_rbac.tests.helpers import make_branch

        e = self.flat.entity
        only = make_branch(self.flat_school, name="Only Branch")
        center = CostCenter.objects.create(entity=e, code="IT", name="IT department")
        budget = Budget.objects.create(
            entity=e, fiscal_year=e.fiscal_years.get(), name="Flat plan",
            status=BudgetStatus.APPROVED, approved_at=timezone.now(),
        )
        BudgetLine.objects.create(
            budget=budget, account=self.acc(e, "5300"), cost_center=center,
            period_no=1, amount=400_000)
        po = PurchaseOrder.objects.create(
            entity=e, vendor=self.flat.vendor, order_date=datetime.date(2026, 1, 5),
            status="APPROVED")
        PurchaseOrderLine.objects.create(
            purchase_order=po, description="Desk", line_no=1, quantity=1, unit_price=50_000,
            net_amount=50_000, cost_center=center, expense_account=self.acc(e, "5300"))
        user = self.user_for(self.flat_tenant, "only.reach@t.com")
        self.grant(user, "procurement.requisition.view", tenant=self.flat_tenant,
                   role_key="reach-only", branch=only)

        data = self.budget(user, books=self.flat)

        self.assertEqual((data["budget"], data["committed"], data["available"]),
                         (400_000, 50_000, 350_000))


class CatalogItemInsightsReachTests(_ReachFixture):
    """Desks were ordered by Ikeja at 20,000; chairs by Lekki at 10,000."""

    def insights(self, user):
        return self.fetch(user, f"catalog-items/{self.item.pk}/insights/")

    def test_a_branch_reader_is_told_only_what_their_branch_bought(self):
        ikeja = self.insights(self.ikeja_reader)
        lekki = self.insights(self.lekki_reader)

        self.assertEqual([h["document_number"] for h in ikeja["purchase_history"]],
                         [self.desks.document_number])
        self.assertEqual([(p["minimum_unit_price"], p["order_count"]) for p in ikeja["vendor_pricing"]],
                         [(20_000, 1)])
        self.assertEqual([h["document_number"] for h in lekki["purchase_history"]],
                         [self.chairs.document_number])
        self.assertEqual([p["maximum_unit_price"] for p in lekki["vendor_pricing"]], [10_000])
        self.assertEqual((ikeja["usage"]["requisition_line_count"],
                          lekki["usage"]["requisition_line_count"]), (1, 1))

    def test_a_whole_school_reader_is_told_what_every_branch_bought(self):
        head = self.insights(self.head)

        self.assertEqual(len(head["purchase_history"]), 2)
        (pricing,) = head["vendor_pricing"]
        self.assertEqual((pricing["minimum_unit_price"], pricing["maximum_unit_price"],
                          pricing["order_count"]), (10_000, 20_000, 2))
        self.assertEqual(head["usage"]["requisition_line_count"], 2)

    def test_another_schools_catalogue_is_never_reachable(self):
        response = self.client_as(self.head).get(
            f"/v1/procurement/catalog-items/{self.item.pk}/insights/"
            f"?entity={self.foreign.entity.code}")

        self.assertEqual(response.status_code, 404)


class ContractLinkedOrdersReachTests(_ReachFixture):
    """The cleaning contract has Ikeja's desks on it; Lekki's chairs and lamps fall in its term."""

    def linked(self, user):
        rows = self.fetch(user, f"contracts/{self.contract.pk}/linked-pos/")
        return {row["id"]: row["link_type"] for row in rows}

    def test_a_branch_reader_is_shown_only_their_branchs_orders(self):
        self.assertEqual(self.linked(self.ikeja_reader), {self.desks.pk: "linked"})
        self.assertEqual(self.linked(self.lekki_reader), {
            self.chairs.pk: "association", self.lamps.pk: "association"})

    def test_a_whole_school_reader_is_shown_every_branchs_orders(self):
        self.assertEqual(self.linked(self.head), {
            self.desks.pk: "linked", self.chairs.pk: "association",
            self.lamps.pk: "association"})

    def test_another_schools_contract_is_never_reachable(self):
        response = self.client_as(self.head).get(
            f"/v1/procurement/contracts/{self.contract.pk}/linked-pos/"
            f"?entity={self.foreign.entity.code}")

        self.assertEqual(response.status_code, 404)
