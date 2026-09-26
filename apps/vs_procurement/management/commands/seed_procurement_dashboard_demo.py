"""Seed a school's books with the procurement activity the Procurement overview reads.

A development aid for a school tenant, run after ``seed_finance_dashboard_demo``
on the same books (it pays vendors from that command's operations account). It
builds one month of purchase-to-payment in school terms: requisitions waiting
and approved, RFQs out, orders at every receipt stage, bills matched, late,
disputed and priced above the order, payments, contracts ending soon, and a
vendor put on hold with a bill still open.

Orders placed and approved before this month stand as already approved, the way
they would arrive from the months before these books were kept here; everything
dated this month runs through the procurement services. Only the open period can
take receipts, bills and payments, so history before it is orders alone. A few
documents are submitted for approval by another member of staff so the
proprietor has a real queue waiting on her.

Everything is marked ``DEMO``. Approver notifications are switched off until
after commit, and no vendor is emailed: orders are never sent from here and no
RFQ invitation goes out to a portal. Refuses to run twice on the same books, and
on production settings.

Usage::

    manage.py seed_procurement_dashboard_demo --entity HOLYCROSS
"""
from __future__ import annotations

import datetime
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

MARK = "DEMO"


class Command(BaseCommand):
    help = "Seed demo procurement activity for the Procurement overview on one school's books."

    def add_arguments(self, parser):
        parser.add_argument("--entity", required=True, help="LedgerEntity code, e.g. HOLYCROSS.")
        parser.add_argument("--requester", help="Email of the staff member who raises and submits documents.")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data is for development databases only.")
        from vs_finance.models import BankAccount, LedgerEntity
        from vs_procurement.models import Vendor

        entity = LedgerEntity.objects.filter(code=options["entity"]).first()
        if entity is None:
            raise CommandError(f"No entity with code {options['entity']}.")
        if Vendor.objects.filter(entity=entity, code__startswith=MARK).exists():
            raise CommandError(f"{entity.code} already holds the procurement demo data.")
        if not BankAccount.objects.filter(entity=entity, name__startswith=MARK).exists():
            raise CommandError("Run seed_finance_dashboard_demo on these books first.")
        # Notices go out from on-commit hooks, so the switch wraps the transaction.
        with mock.patch("vs_workflow.services.routing.notify", return_value=None), transaction.atomic():
            Seeder(entity, datetime.date.today(), options.get("requester"), self.stdout).run()


class Seeder:
    def __init__(self, entity, as_of, requester_email, out):
        from vs_finance.models import FiscalPeriod
        from vs_tenants.models import Branch

        self.entity, self.as_of, self.out = entity, as_of, out
        period = FiscalPeriod.objects.filter(
            entity=entity, status="OPEN", start_date__lte=as_of, end_date__gte=as_of,
        ).first()
        if period is None:
            raise CommandError(f"{entity.code} has no open period containing {as_of}.")
        self.start = period.start_date
        users = get_user_model().objects.filter(tenant=entity.tenant, is_active=True).order_by("id")
        self.actor = users.first()
        self.requester = (
            users.filter(email=requester_email).first() if requester_email else users.exclude(pk=self.actor.pk).first()
        ) or self.actor
        branches = list(Branch.objects.filter(tenant=entity.tenant).order_by("id"))
        self.main = branches[0] if branches else None
        self.annex = branches[1] if len(branches) > 1 else self.main

    def say(self, text):
        self.out.write(f"  {text}")

    def day(self, n):
        """``n`` days into the open period, never past today (negative reaches back before it)."""
        return min(self.start + datetime.timedelta(days=n), self.as_of)

    def account(self, code):
        from vs_finance.models import Account

        return Account.objects.get(entity=self.entity, code=code)

    def run(self):
        self.out.write(f"Seeding procurement demo data on {self.entity.code}:")
        self.expense, self.payable = self.account("5300"), self.account("2100")
        self.vendors = self.vendors_and_categories()
        self.contracts = self.contracts_ending()
        self.requisitions()
        self.rfqs()
        self.orders_to_payment()
        self.say(f"approvals submitted by {self.requester.email} for {self.actor.email} to decide")
        self.out.write("Done.")

    # -- master data ---------------------------------------------------------- #

    def vendors_and_categories(self):
        from vs_procurement.models import Vendor, VendorCategory

        cats = {
            code: VendorCategory.objects.create(entity=self.entity, code=f"{MARK}-{code}", name=name,
                                                default_expense_account=self.expense)
            for code, name in (("TEACH", "Teaching materials"), ("FUEL", "Fuel and power"),
                               ("FAC", "Facilities and repairs"), ("FEED", "Feeding"), ("UNIF", "Uniforms"))
        }
        specs = {
            "SCI": ("Scientific Supplies Ltd", "TEACH"), "OIL": ("Eterna Oil", "FUEL"),
            "BOOKS": ("Learn Africa Books", "TEACH"), "FOOD": ("Chellarams Foods", "FEED"),
            "KLEEN": ("Kleen Facility Services", "FAC"), "MOTORS": ("Nairaland Motors", "FAC"),
            "UNIF": ("Prime Uniforms", "UNIF"),
        }
        vendors = {
            key: Vendor.objects.create(
                entity=self.entity, code=f"{MARK}-{key}", name=name, category=cats[cat],
                payable_account=self.payable, default_expense_account=self.expense,
                kyc_status="VERIFIED", is_active=True,
            )
            for key, (name, cat) in specs.items()
        }
        self.say(f"{len(cats)} categories, {len(vendors)} vendors")
        return vendors

    def contracts_ending(self):
        from vs_procurement.contracts import activate_contract
        from vs_procurement.models import VendorContract

        out = {}
        for key, vendor, title, value, ends in (
            ("BUS", "MOTORS", "School bus maintenance", 4_500_000_00, 18),
            ("CLEAN", "KLEEN", "Cleaning services", 12_000_000_00, 65),
            ("DIESEL", "OIL", "Diesel supply", 30_000_000_00, 88),
        ):
            contract = VendorContract.objects.create(
                entity=self.entity, vendor=self.vendors[vendor], reference=f"{MARK}-CON-{key}", title=title,
                start_date=self.as_of - datetime.timedelta(days=300), end_date=self.as_of + datetime.timedelta(days=ends),
                contract_value=value, created_by=self.actor,
            )
            activate_contract(contract, actor_user=self.actor)
            out[key] = contract
        self.say(f"{len(out)} contracts ending within 90 days")
        return out

    # -- asking and sourcing -------------------------------------------------- #

    def requisition(self, title, branch, lines, *, submit):
        from vs_procurement.approvals import submit_for_approval
        from vs_procurement.models import PurchaseRequisition, PurchaseRequisitionLine

        req = PurchaseRequisition.objects.create(
            entity=self.entity, branch=branch, title=title, requested_by=self.requester,
            request_date=self.day(10), justification=title,
        )
        for n, (desc, qty, price) in enumerate(lines, start=1):
            PurchaseRequisitionLine.objects.create(requisition=req, line_no=n, description=desc, quantity=qty,
                                                   estimated_unit_price=price, expense_account=self.expense)
        req.recompute_total()
        if submit:
            submit_for_approval(req, actor_user=self.requester)
        return req

    def requisitions(self):
        waiting = [
            ("Term 1 exercise books", self.annex, [("80-leaf exercise books", 4000, 600_00)]),
            ("Generator servicing", self.main, [("Service and parts", 1, 380_000_00)]),
            ("Sports kit for inter-house", self.main, [("Jerseys", 120, 6_500_00)]),
        ]
        for title, branch, lines in waiting:
            self.requisition(title, branch, lines, submit=True)
        for title, branch, lines in (
            ("Laboratory glassware", self.main, [("Beakers and flasks", 60, 9_000_00)]),
            ("Classroom whiteboards", self.annex, [("Whiteboard 8x4", 12, 85_000_00)]),
        ):
            req = self.requisition(title, branch, lines, submit=False)
            req.status, req.approval_state = "APPROVED", "APPROVED"
            req.save(update_fields=["status", "approval_state", "updated_at"])
        self.say("5 requisitions: 3 waiting for approval, 2 approved and not yet ordered")

    def rfqs(self):
        from vs_procurement.models import RequestForQuotation, RfqLine
        from vs_procurement.sourcing import issue_rfq, set_rfq_invitations

        for title, due, budget, vendors in (
            ("Term 2 textbooks", 3, 9_600_000_00, ["BOOKS", "SCI"]),
            ("Kitchen equipment", 12, 2_300_000_00, ["FOOD", "KLEEN"]),
        ):
            rfq = RequestForQuotation.objects.create(
                entity=self.entity, branch=self.main, title=title, issue_date=self.day(12),
                response_due_date=self.as_of + datetime.timedelta(days=due), budget_estimate=budget,
                notes=f"{MARK} sourcing", created_by=self.actor,
            )
            RfqLine.objects.create(rfq=rfq, line_no=1, description=title, quantity=1, expense_account=self.expense)
            set_rfq_invitations(rfq, [self.vendors[v] for v in vendors], actor_user=self.actor)
            issue_rfq(rfq, competition_exception_reason="Demo data", actor_user=self.actor)
        self.say("2 RFQs out, one closing this week")

    # -- orders to payment ---------------------------------------------------- #

    def order(self, vendor, branch, desc, qty, price, ordered, *, contract=None, approve=True):
        from vs_procurement.models import PurchaseOrder, PurchaseOrderLine
        from vs_procurement.purchasing import approve_purchase_order, price_po

        po = PurchaseOrder.objects.create(
            entity=self.entity, vendor=self.vendors[vendor], branch=branch, order_date=ordered,
            expected_date=ordered + datetime.timedelta(days=10), reference=f"{MARK}-{desc[:20]}",
            contract=self.contracts.get(contract) if contract else None, created_by=self.actor,
        )
        PurchaseOrderLine.objects.create(purchase_order=po, line_no=1, description=desc, quantity=qty,
                                         unit_price=price, expense_account=self.expense)
        price_po(po)
        if approve:
            po.approval_state = "APPROVED"
            po.save(update_fields=["approval_state", "updated_at"])
            approve_purchase_order(po, actor_user=self.actor)
        return po

    def receive(self, po, qty, date):
        from vs_procurement.models import GoodsReceivedNote, GoodsReceivedNoteLine
        from vs_procurement.purchasing import post_grn

        line = po.lines.first()
        grn = GoodsReceivedNote.objects.create(
            entity=self.entity, vendor=po.vendor, purchase_order=po, branch=po.branch, received_date=date,
            received_by=self.actor, reference=f"{MARK}-GRN-{po.pk}", created_by=self.actor,
        )
        GoodsReceivedNoteLine.objects.create(grn=grn, po_line=line, expense_account=self.expense,
                                             accepted_qty=qty, unit_price=line.unit_price, line_no=1)
        post_grn(grn, actor_user=self.actor)
        return grn

    def bill(self, po, grn, qty, date, *, price=None, terms=14, post=True):
        from vs_procurement.models import VendorInvoice, VendorInvoiceLine
        from vs_procurement.payables import match_vendor_invoice, post_vendor_invoice, price_vendor_invoice

        line = po.lines.first()
        invoice = VendorInvoice.objects.create(
            entity=self.entity, vendor=po.vendor, purchase_order=po, branch=po.branch, invoice_date=date,
            due_date=date + datetime.timedelta(days=terms), vendor_reference=f"{MARK}-INV-{po.pk}",
            narration=f"{MARK} {line.description}", created_by=self.actor,
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=invoice, po_line=line, grn_line=grn.lines.first() if grn else None, line_no=1,
            description=line.description, expense_account=self.expense, quantity=qty,
            unit_price=price or line.unit_price,
        )
        price_vendor_invoice(invoice)
        match_vendor_invoice(invoice)
        if post:
            invoice.approval_state = "APPROVED"
            invoice.save(update_fields=["approval_state", "updated_at"])
            post_vendor_invoice(invoice, actor_user=self.actor, allow_variance=True)
        invoice.refresh_from_db()
        return invoice

    def pay(self, invoice, date, amount=None):
        from vs_finance.models import BankAccount
        from vs_procurement.models import VendorPayment, VendorPaymentAllocation
        from vs_procurement.payables import post_vendor_payment

        bank = BankAccount.objects.get(entity=self.entity, name__startswith=MARK, name__icontains="operations")
        amount = amount or invoice.total - invoice.amount_paid
        payment = VendorPayment.objects.create(
            entity=self.entity, vendor=invoice.vendor, branch=invoice.branch, payment_date=date,
            method="BANK_TRANSFER", payment_account=bank.gl_account, gross_amount=amount, net_amount=amount,
            reference=f"{MARK}-PAY-{invoice.pk}", created_by=self.actor, approval_state="APPROVED",
        )
        VendorPaymentAllocation.objects.create(payment=payment, vendor_invoice=invoice, amount=amount)
        post_vendor_payment(payment, actor_user=self.actor, auto_allocate=False)

    def orders_to_payment(self):
        from vs_procurement.approvals import submit_for_approval

        before = self.start - datetime.timedelta(days=6)
        # Ordered late last month, delivered, billed and paid this month.
        po = self.order("SCI", self.main, "Lab reagents", 40, 46_500_00, before)
        self.pay(self.bill(po, self.receive(po, 40, self.day(2)), 40, self.day(7)), self.day(19))
        po = self.order("BOOKS", self.annex, "Exercise books", 3000, 700_00, before + datetime.timedelta(days=3))
        self.bill(po, self.receive(po, 3000, self.day(4)), 3000, self.day(9))  # due and unpaid
        po = self.order("KLEEN", self.main, "Cleaning, September", 1, 1_210_000_00, self.day(0), contract="CLEAN")
        self.pay(self.bill(po, self.receive(po, 1, self.day(1)), 1, self.day(1), terms=7), self.day(9))
        # Diesel on the supply contract, billed above the order price.
        po = self.order("OIL", self.main, "Diesel, 3 deliveries", 3, 1_000_000_00, self.day(1), contract="DIESEL")
        self.bill(po, self.receive(po, 3, self.day(3)), 3, self.day(5), price=1_120_000_00, terms=30)
        po = self.order("FOOD", self.annex, "Kitchen provisions", 1, 1_940_000_00, self.day(7))
        invoice = self.bill(po, self.receive(po, 1, self.day(11)), 1, self.day(14))
        self.pay(invoice, self.day(21), amount=1_000_000_00)
        # Bus maintenance on its contract, half received and not yet billed.
        po = self.order("MOTORS", self.main, "Bus servicing", 4, 1_025_000_00, self.day(9), contract="BUS")
        self.receive(po, 2, self.day(17))
        # Ordered, waiting for delivery.
        self.order("SCI", self.main, "Microscopes", 6, 210_000_00, self.day(19))
        # Billed for more than was received: the match fails and the bill waits.
        po = self.order("FOOD", self.main, "Rice and beans", 10, 180_000_00, self.day(13))
        self.bill(po, self.receive(po, 5, self.day(16)), 8, self.day(18), post=False)
        # A vendor put on hold after its bill was posted.
        po = self.order("UNIF", self.annex, "Sports uniforms", 40, 5_500_00, self.day(4))
        self.bill(po, self.receive(po, 40, self.day(8)), 40, self.day(10), terms=30)
        self.vendors["UNIF"].on_hold = True
        self.vendors["UNIF"].save(update_fields=["on_hold", "updated_at"])
        self.say("9 orders from asking to paying: paid, owed, late, partly received, disputed, on hold")

        # Waiting on the proprietor.
        po = self.order("BOOKS", self.main, "Term 2 textbooks", 300, 6_200_00, self.day(20), approve=False)
        submit_for_approval(po, actor_user=self.requester)
        po = self.order("OIL", self.main, "Diesel, October", 3, 1_000_000_00, self.day(22), contract="DIESEL",
                        approve=False)
        submit_for_approval(po, actor_user=self.requester)
