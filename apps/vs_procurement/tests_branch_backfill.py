"""The branch backfill over procurement: each document reads the one it came from.

Built on finance's branch fixture (Ikeja is the main branch, then Lekki and
Yaba, plus a one-branch school). Rows are stripped of their branch with a
queryset update, the state a row written before the rule is in.
"""
from __future__ import annotations

import datetime

from django.contrib.auth import get_user_model

from vs_finance.branch_derivation import ONLY_BRANCH, apply_plan, plan_entity
from vs_finance.models import Account, BankAccount, JournalEntry
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_procurement.models import (
    GoodsReceivedNote,
    PurchaseOrder,
    PurchaseRequisition,
    RequestForQuotation,
    StockLocation,
    Vendor,
    VendorInvoice,
    VendorPayment,
    VendorPaymentAllocation,
    VendorQuotation,
)

DAY = datetime.date(2026, 1, 12)


def blank(row):
    type(row)._base_manager.filter(pk=row.pk).update(branch=None)
    row.refresh_from_db()
    return row


def label(row):
    return f"{type(row)._meta.app_label}.{type(row).__name__}"


class ProcurementBackfillTests(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        self.vendor = Vendor.objects.create(entity=self.books, code="V1", name="Stationers")

    def plan(self, entity=None):
        return plan_entity(entity or self.books)

    def assigned(self, plan, row):
        return next(p for p in plan.targets if p.target.model_label == label(row)).assign.get(row.pk)

    def flag(self, plan, row):
        target_plan = next(p for p in plan.targets if p.target.model_label == label(row))
        return next((f for f in target_plan.flags if f.pk == row.pk), None)

    def requisition(self, branch, requester=None):
        row = PurchaseRequisition.objects.create(
            entity=self.books, request_date=DAY, branch=branch, requested_by=requester,
        )
        return blank(row) if branch is None else row

    def order(self, requisition=None, branch=None):
        row = PurchaseOrder.objects.create(
            entity=self.books, vendor=self.vendor, order_date=DAY, requisition=requisition, branch=branch,
        )
        return blank(row) if branch is None else row

    def bill(self, order=None, branch=None):
        row = VendorInvoice.objects.create(
            entity=self.books, vendor=self.vendor, invoice_date=DAY, purchase_order=order, branch=branch,
        )
        return blank(row) if branch is None else row

    def test_a_requisition_takes_its_requesters_branch(self):
        requester = get_user_model().objects.create_user(
            email="req@x.test", password="pw", tenant=self.tenant, branch=self.lekki,
            status="ACTIVE", first_name="Ada", last_name="Obi",
        )
        row = self.requisition(None, requester)
        self.assertEqual(self.assigned(self.plan(), row), (self.lekki.pk, "the requester"))

    def test_the_sourcing_chain_follows_the_requisition_through_planned_branches(self):
        requester = get_user_model().objects.create_user(
            email="req@x.test", password="pw", tenant=self.tenant, branch=self.yaba,
            status="ACTIVE", first_name="Ada", last_name="Obi",
        )
        requisition = self.requisition(None, requester)
        rfq = blank(RequestForQuotation.objects.create(entity=self.books, issue_date=DAY, requisition=requisition))
        quote = blank(VendorQuotation.objects.create(entity=self.books, rfq=rfq, vendor=self.vendor, quote_date=DAY))
        order = self.order(requisition)
        receipt = blank(GoodsReceivedNote.objects.create(
            entity=self.books, vendor=self.vendor, received_date=DAY, purchase_order=order,
        ))
        bill = self.bill(order)
        plan = self.plan()
        self.assertEqual(self.assigned(plan, rfq), (self.yaba.pk, "the requisition"))
        self.assertEqual(self.assigned(plan, quote), (self.yaba.pk, "the RFQ"))
        self.assertEqual(self.assigned(plan, order), (self.yaba.pk, "the requisition"))
        self.assertEqual(self.assigned(plan, receipt), (self.yaba.pk, "the purchase order"))
        self.assertEqual(self.assigned(plan, bill), (self.yaba.pk, "the purchase order"))

    def test_an_order_without_a_requisition_reads_the_quotation_it_awarded(self):
        order = self.order()
        rfq = RequestForQuotation.objects.create(entity=self.books, issue_date=DAY, branch=self.lekki)
        VendorQuotation.objects.create(
            entity=self.books, rfq=rfq, vendor=self.vendor, quote_date=DAY, branch=self.lekki, awarded_po=order,
        )
        self.assertEqual(self.assigned(self.plan(), order), (self.lekki.pk, "the awarded quotation"))

    def test_a_supplier_payment_reads_its_bills_then_its_bank(self):
        gl = Account.objects.create(
            entity=self.books, code="1181", name="Cash 81",
            account_type=Account.objects.get(entity=self.books, code="1100").account_type, is_postable=True,
        )
        BankAccount.objects.create(entity=self.books, name="Lekki Ops", branch=self.lekki, gl_account=gl)
        by_bills = blank(VendorPayment.objects.create(entity=self.books, vendor=self.vendor, payment_date=DAY, gross_amount=100))
        split = blank(VendorPayment.objects.create(
            entity=self.books, vendor=self.vendor, payment_date=DAY, payment_account=gl, gross_amount=100,
        ))
        VendorPaymentAllocation.objects.create(payment=by_bills, vendor_invoice=self.bill(branch=self.yaba), amount=1)
        VendorPaymentAllocation.objects.create(payment=split, vendor_invoice=self.bill(branch=self.ikeja), amount=1)
        VendorPaymentAllocation.objects.create(payment=split, vendor_invoice=self.bill(branch=self.yaba), amount=1)
        plan = self.plan()
        self.assertEqual(self.assigned(plan, by_bills), (self.yaba.pk, "the settled bills"))
        self.assertEqual(self.assigned(plan, split), (self.lekki.pk, "the paying bank account"))

    def test_a_bill_journal_reads_the_bill(self):
        entry = blank(JournalEntry.objects.create(entity=self.books, date=DAY))
        bill = self.bill(branch=self.lekki)
        VendorInvoice._base_manager.filter(pk=bill.pk).update(journal=entry)
        self.assertEqual(self.assigned(self.plan(), entry), (self.lekki.pk, "the document that raised it"))

    def test_a_central_store_is_flagged_at_a_multi_branch_school_and_filed_at_a_one_branch_one(self):
        store = blank(StockLocation.objects.create(entity=self.books, code="MAIN", name="Main store"))
        solo_store = blank(StockLocation.objects.create(entity=self.solo_books, code="MAIN", name="Main store"))
        plan = self.plan()
        self.assertIsNone(self.assigned(plan, store))
        self.assertIn("a central store belongs to one branch", self.flag(plan, store).reason)
        solo_plan = self.plan(self.solo_books)
        self.assertEqual(self.assigned(solo_plan, solo_store), (self.solo_main.pk, ONLY_BRANCH))
        apply_plan(plan)
        store.refresh_from_db()
        self.assertIsNone(store.branch_id)

    def test_an_order_with_no_upstream_branch_names_the_requisition(self):
        order = self.order(self.requisition(None))
        self.assertEqual(self.flag(self.plan(), order).reason, "no branch on the requisition")
