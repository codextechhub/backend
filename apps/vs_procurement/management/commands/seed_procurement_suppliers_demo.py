"""Add the supplier history the Spend & suppliers tab reads to a school's demo books.

A development aid, run after ``seed_procurement_dashboard_demo`` on the same
books. It adds what that command leaves out:

- quotes on the open RFQs, one of them fully answered and ready to award;
- an RFQ for bus tyres quoted by three vendors and awarded to the cheapest, so
  competition has saved something;
- a requisition approved and ordered, so buying has a measured approval and
  ordering time;
- an order delivered late with part of it rejected at the gate;
- a scorecard assessment for five vendors, and a new vendor still awaiting KYC;
- a diesel bill paid without an order, which takes non-PO spend over the
  school's 2% limit (the school is opted in to bills without an order);
- the school operating plan given a purchasing line of two million a month
  and approved, so spend can be read against it.

Everything goes through the procurement and budget services and is marked
``DEMO``; approver notices are off until after commit and no vendor is emailed.
Refuses to run twice and on production settings.

Usage::

    manage.py seed_procurement_suppliers_demo --entity HOLYCROSS
"""
from __future__ import annotations

import datetime
from unittest import mock

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from .seed_procurement_dashboard_demo import MARK, Seeder as DashboardSeeder


class Command(BaseCommand):
    help = "Add demo supplier history (quotes, awards, assessments, late delivery) to one school's books."

    def add_arguments(self, parser):
        parser.add_argument("--entity", required=True, help="LedgerEntity code, e.g. HOLYCROSS.")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data is for development databases only.")
        from vs_finance.models import LedgerEntity
        from vs_procurement.models import Vendor, VendorAssessment

        entity = LedgerEntity.objects.filter(code=options["entity"]).first()
        if entity is None:
            raise CommandError(f"No entity with code {options['entity']}.")
        if not Vendor.objects.filter(entity=entity, code__startswith=MARK).exists():
            raise CommandError("Run seed_procurement_dashboard_demo on these books first.")
        if VendorAssessment.objects.filter(entity=entity, notes__startswith=MARK).exists():
            raise CommandError(f"{entity.code} already holds the supplier demo data.")
        with mock.patch("vs_workflow.services.routing.notify", return_value=None), transaction.atomic():
            Seeder(entity, datetime.date.today(), None, self.stdout).run_suppliers()


class Seeder(DashboardSeeder):
    def vendor(self, key):
        from vs_procurement.models import Vendor

        return Vendor.objects.get(entity=self.entity, code=f"{MARK}-{key}")

    def run_suppliers(self):
        self.out.write(f"Adding supplier demo data on {self.entity.code}:")
        self.expense, self.payable = self.account("5300"), self.account("2100")
        from vs_procurement.models import Vendor

        self.vendors = {v.code.removeprefix(f"{MARK}-"): v for v in Vendor.objects.filter(entity=self.entity, code__startswith=MARK)}
        self.contracts = {}
        self.quotes()
        self.award()
        self.requisition_to_order()
        self.late_delivery()
        self.assessments()
        self.direct_bill()
        self.approve_plan()
        self.out.write("Done.")

    def quote(self, rfq, vendor_key, unit_price, *, lead=7):
        from vs_procurement.models import VendorQuotation, VendorQuotationLine
        from vs_procurement.sourcing import price_quotation, submit_quotation

        line = rfq.lines.first()
        quote = VendorQuotation.objects.create(
            entity=self.entity, rfq=rfq, vendor=self.vendors[vendor_key], quote_date=self.as_of,
            valid_until=self.as_of + datetime.timedelta(days=30), lead_time_days=lead,
            reference=f"{MARK}-Q-{rfq.pk}-{vendor_key}", created_by=self.actor,
        )
        VendorQuotationLine.objects.create(quotation=quote, rfq_line=line, line_no=1, description=line.description,
                                           expense_account=self.expense, quantity=line.quantity, unit_price=unit_price)
        price_quotation(quote)
        submit_quotation(quote, actor_user=self.actor)
        return quote

    def quotes(self):
        from vs_procurement.models import RequestForQuotation

        books = RequestForQuotation.objects.get(entity=self.entity, title="Term 2 textbooks", notes__startswith=MARK)
        kitchen = RequestForQuotation.objects.get(entity=self.entity, title="Kitchen equipment", notes__startswith=MARK)
        self.quote(books, "BOOKS", 9_300_000_00)
        self.quote(kitchen, "FOOD", 2_150_000_00)
        self.quote(kitchen, "KLEEN", 2_420_000_00)
        self.say("quotes in: textbooks 1 of 2, kitchen equipment 2 of 2 (ready to award)")

    def award(self):
        from vs_procurement.models import RequestForQuotation, RfqLine
        from vs_procurement.sourcing import award_quotation, issue_rfq, set_rfq_invitations

        rfq = RequestForQuotation.objects.create(
            entity=self.entity, branch=self.main, title="Bus tyres, 12 units", issue_date=self.day(3),
            response_due_date=self.day(12), budget_estimate=900_000_00, notes=f"{MARK} sourcing", created_by=self.actor,
        )
        RfqLine.objects.create(rfq=rfq, line_no=1, description="Bus tyre", quantity=12, expense_account=self.expense)
        set_rfq_invitations(rfq, [self.vendors[k] for k in ("MOTORS", "OIL", "KLEEN")], actor_user=self.actor)
        issue_rfq(rfq, competition_exception_reason="Demo data", actor_user=self.actor)
        cheapest = self.quote(rfq, "MOTORS", 58_000_00)
        self.quote(rfq, "OIL", 66_500_00)
        self.quote(rfq, "KLEEN", 71_000_00)
        award_quotation(cheapest, order_date=self.day(14), actor_user=self.actor)
        self.say("bus tyres RFQ awarded to the cheapest of 3 quotes")

    def requisition_to_order(self):
        from vs_procurement.purchasing import approve_requisition

        req = self.requisition("Chemistry practical kits", self.main,
                               [("Practical kit", 30, 18_000_00)], submit=False)
        req.request_date = self.day(2)
        req.save(update_fields=["request_date", "updated_at"])
        approve_requisition(req, actor_user=self.actor)
        po = self.order("SCI", self.main, "Chemistry practical kits", 30, 18_000_00, self.as_of)
        po.requisition = req
        po.save(update_fields=["requisition", "updated_at"])
        self.say("a requisition approved and ordered")

    def late_delivery(self):
        from vs_procurement.models import GoodsReceivedNote, GoodsReceivedNoteLine
        from vs_procurement.purchasing import post_grn

        po = self.order("UNIF", self.annex, "PE shorts", 60, 2_500_00, self.day(2))
        po.expected_date = self.day(5)
        po.save(update_fields=["expected_date", "updated_at"])
        line = po.lines.first()
        grn = GoodsReceivedNote.objects.create(
            entity=self.entity, vendor=po.vendor, purchase_order=po, branch=po.branch, received_date=self.day(15),
            received_by=self.actor, reference=f"{MARK}-GRN-LATE", created_by=self.actor,
        )
        GoodsReceivedNoteLine.objects.create(grn=grn, po_line=line, expense_account=self.expense, line_no=1,
                                             accepted_qty=52, rejected_qty=8, unit_price=line.unit_price)
        post_grn(grn, actor_user=self.actor)
        self.say("PE shorts delivered 10 days late, 8 of 60 rejected")

    def assessments(self):
        from vs_procurement.models import Vendor, VendorAssessment

        for key, scores in (("SCI", (95, 98, 92, 90)), ("OIL", (88, 100, 85, 90)), ("BOOKS", (80, 96, 82, 78)),
                            ("FOOD", (84, 88, 80, 82)), ("UNIF", (60, 72, 70, 65))):
            VendorAssessment.objects.create(
                entity=self.entity, vendor=self.vendors[key], assessor=self.actor, assessment_date=self.day(20),
                on_time_delivery=scores[0], quality_acceptance=scores[1], invoice_accuracy=scores[2],
                responsiveness=scores[3], notes=f"{MARK} termly review",
            )
        Vendor.objects.create(
            entity=self.entity, code=f"{MARK}-SOLAR", name="Sunrise Solar", payable_account=self.payable,
            default_expense_account=self.expense, kyc_status="PENDING", is_active=True,
        )
        self.say("5 vendor assessments; 1 new vendor awaiting KYC")

    def direct_bill(self):
        from vs_procurement.models import ProcurementSettings, VendorInvoice, VendorInvoiceLine
        from vs_procurement.payables import post_vendor_invoice, price_vendor_invoice

        ProcurementSettings.objects.update_or_create(entity=self.entity, defaults={"allow_non_po_invoices": True})
        bill = VendorInvoice.objects.create(
            entity=self.entity, vendor=self.vendors["OIL"], branch=self.main, invoice_date=self.day(21),
            due_date=self.day(21) + datetime.timedelta(days=14), vendor_reference=f"{MARK}-INV-DIRECT",
            narration=f"{MARK} emergency diesel", created_by=self.actor, approval_state="APPROVED",
        )
        VendorInvoiceLine.objects.create(vendor_invoice=bill, line_no=1, description="Emergency diesel",
                                         expense_account=self.expense, quantity=1, unit_price=240_000_00)
        price_vendor_invoice(bill)
        post_vendor_invoice(bill, actor_user=self.actor)
        self.say("a diesel bill paid without an order")

    def approve_plan(self):
        from vs_finance.budgets import approve_budget
        from vs_finance.models import Budget

        from vs_finance.models import BudgetLine

        plan = Budget.objects.filter(entity=self.entity, branch__isnull=True, status="DRAFT").order_by("-id").first()
        if plan is not None:
            # Purchasing's line of the plan: two million a month on the account bills post to.
            for period_no in range(1, 13):
                BudgetLine.objects.update_or_create(
                    budget=plan, account=self.expense, cost_center=None, period_no=period_no,
                    defaults={"amount": 2_000_000_00},
                )
            approve_budget(plan, actor_user=self.actor)
            self.say(f"'{plan.name}' approved, with purchasing planned at 2M a month")
