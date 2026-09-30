"""Correcting a posted supplier bill or goods receipt without breaking the close.

Corona runs Ikeja, Lekki and Yaba and buys from Chuks Stationery. Mrs Okafor, the
bursar, keys bills and receives goods. When one of those is wrong she has three
documents, each of which moves the AP sub-ledger and the AP control together:

* a vendor credit note against the bill, approved like the bill, which settles what
  is owed and turns anything already paid into vendor credit for that branch;
* a void, for a bill nothing has been paid or credited against;
* a goods return against the receipt, for goods that go back to the supplier.

Every scenario ends by asking the period close whether the AP sub-ledger still
equals its control account, because that is the check the missing corrections used
to break for good.
"""
from __future__ import annotations

import datetime
import itertools
from decimal import Decimal

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus, InvoicePaymentStatus
from vs_finance.exceptions import PostingError
from vs_finance.models import Account, FiscalPeriod, TaxCode
from vs_finance.posting import journal_reversal_action, reverse_journal
from vs_finance.reports import _account_gl_net
from vs_finance.tests_branch_scope import _FinanceBranchFixture

from .close_checks import ap_reconciled
from .constants import MatchStatus, ProcApprovalState, StockMovementType
from .corrections import (
    allocate_vendor_credit_note,
    post_vendor_credit_note,
    return_goods,
    void_vendor_credit_note,
    void_vendor_invoice,
    write_credit_note_lines,
)
from .exceptions import (
    GoodsReturnError,
    SettlementBranchError,
    VendorCreditNoteError,
    VendorInvoiceVoidError,
)
from .models import (
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    ProcurementSettings,
    PurchaseOrder,
    PurchaseOrderLine,
    StockItem,
    StockLocation,
    StockMovement,
    Vendor,
    VendorCreditNote,
    VendorInvoice,
    VendorInvoiceLine,
    VendorPayment,
)
from .payables import post_vendor_invoice, post_vendor_payment
from .purchasing import goods_arrived, post_grn, price_po
from .reports import grir_aging, reconcile_ap

JAN = datetime.date
_roles = itertools.count(1)


class _APCorrectionsFixture(_FinanceBranchFixture):
    """Corona's books, Chuks Stationery, input VAT and a store at Ikeja."""

    def setUp(self):
        super().setUp()
        e = self.books
        ProcurementSettings.objects.update_or_create(
            entity=e, defaults={"allow_non_po_invoices": True},
        )
        self.store = StockLocation.objects.create(
            entity=e, code="MAIN", name="Main store", is_default=True,
        )
        self.vendor = Vendor.objects.create(
            entity=e, code="CHUKS", name="Chuks Stationery",
            payable_account=self.acc("2100"), default_expense_account=self.acc("5300"),
            kyc_status="VERIFIED",
        )
        self.vat = TaxCode.objects.create(
            entity=e, code="VAT-IN", name="Input VAT 7.5%", rate_bps=750,
            paid_account=self.acc("1300"),
        )
        self.period = FiscalPeriod.objects.get(entity=e, period_no=1)

    # -- ledger ------------------------------------------------------------- #

    def acc(self, code):
        return Account.objects.get(entity=self.books, code=code)

    def balance(self, code):
        return _account_gl_net(self.acc(code))

    def assertAPReconciled(self):
        rec = reconcile_ap(self.books)
        self.assertEqual(rec.difference, 0, (rec.subledger_total, rec.control_total))
        self.assertTrue(ap_reconciled(self.books, self.period).passed)

    # -- documents ---------------------------------------------------------- #

    def non_po_bill(self, amount, *, branch=None, day=10, tax=True):
        """A posted stationery bill with no order behind it."""
        bill = VendorInvoice.objects.create(
            entity=self.books, vendor=self.vendor, branch=branch or self.ikeja,
            invoice_date=JAN(2026, 1, day), due_date=JAN(2026, 1, day),
            approval_state=ProcApprovalState.APPROVED,
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=bill, description="Exercise books",
            expense_account=self.acc("5300"), quantity=1, unit_price=amount,
            tax_code=self.vat if tax else None, line_no=1,
        )
        post_vendor_invoice(bill)
        bill.refresh_from_db()
        return bill

    def order(self, quantity, price, *, stock_item=None):
        po = PurchaseOrder.objects.create(
            entity=self.books, vendor=self.vendor, branch=self.ikeja,
            order_date=JAN(2026, 1, 5), status=DocumentStatus.APPROVED,
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po, description="Exercise books",
            expense_account=self.acc("5100"), quantity=quantity, unit_price=price,
            line_no=1,
        )
        price_po(po)
        return po

    def receive(self, po, quantity, *, stock_item=None, day=8):
        po_line = po.lines.get()
        grn = GoodsReceivedNote.objects.create(
            entity=self.books, vendor=self.vendor, purchase_order=po, branch=po.branch,
            received_date=JAN(2026, 1, day),
        )
        GoodsReceivedNoteLine.objects.create(
            grn=grn, po_line=po_line, expense_account=po_line.expense_account,
            stock_item=stock_item, accepted_qty=quantity,
            unit_price=po_line.unit_price, line_no=1, description="Exercise books",
        )
        return post_grn(grn)

    def po_bill(self, po, quantity, *, price=None, day=10):
        po_line = po.lines.get()
        bill = VendorInvoice.objects.create(
            entity=self.books, vendor=self.vendor, purchase_order=po, branch=po.branch,
            invoice_date=JAN(2026, 1, day), due_date=JAN(2026, 1, day),
            approval_state=ProcApprovalState.APPROVED,
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=bill, po_line=po_line, description="Exercise books",
            expense_account=po_line.expense_account, quantity=quantity,
            unit_price=price if price is not None else po_line.unit_price, line_no=1,
        )
        post_vendor_invoice(bill)
        bill.refresh_from_db()
        return bill

    def pay(self, bill, amount, *, day=12):
        payment = VendorPayment.objects.create(
            entity=self.books, vendor=self.vendor, branch=bill.branch,
            payment_date=JAN(2026, 1, day), gross_amount=amount,
            payment_account=self.acc("1100"), approval_state=ProcApprovalState.APPROVED,
        )
        post_vendor_payment(payment, allocations=[(bill, amount)])
        bill.refresh_from_db()
        return payment

    def credit_note(self, bill, *, day=15, approved=True, **instruction):
        note = VendorCreditNote.objects.create(
            entity=self.books, vendor=bill.vendor, vendor_invoice=bill, branch=bill.branch,
            note_date=JAN(2026, 1, day), reason="Keyed wrong",
            approval_state=ProcApprovalState.APPROVED if approved
            else ProcApprovalState.NOT_SUBMITTED,
        )
        write_credit_note_lines(note, **instruction)
        note.refresh_from_db()
        return note

    def journal_lines(self, entry):
        return {
            line.account.code: (line.debit, line.credit)
            for line in entry.lines.select_related("account")
        }


class VendorCreditNoteTests(_APCorrectionsFixture):
    """Mrs Okafor keys Chuks's N1,850,000 bill instead of N185,000."""

    def test_a_partial_credit_on_an_unpaid_bill_reduces_what_is_owed(self):
        bill = self.non_po_bill(1_850_000_00)
        self.assertEqual(bill.total, 1_988_750_00)

        note = self.credit_note(bill, amount=1_789_875_00)
        post_vendor_credit_note(note)

        note.refresh_from_db()
        bill.refresh_from_db()
        self.assertEqual(note.status, DocumentStatus.POSTED)
        self.assertEqual((note.subtotal, note.tax_total), (1_665_000_00, 124_875_00))
        self.assertEqual(bill.amount_credited, 1_789_875_00)
        self.assertEqual(bill.balance_due, 198_875_00)
        self.assertEqual(bill.payment_status, InvoicePaymentStatus.PARTIAL)
        self.assertEqual(self.journal_lines(note.journal), {
            "2100": (1_789_875_00, 0),
            "5300": (0, 1_665_000_00),
            "1300": (0, 124_875_00),
        })
        self.assertEqual(note.advance_remaining, 0)
        self.assertAPReconciled()

    def test_a_full_credit_on_a_billed_order_gives_gr_ir_and_the_order_back(self):
        po = self.order(10, 50_000)
        self.receive(po, 10)
        bill = self.po_bill(po, 10)
        self.assertEqual(self.balance("2150"), 0)

        note = self.credit_note(bill, full=True)
        post_vendor_credit_note(note)

        bill.refresh_from_db()
        self.assertEqual(bill.balance_due, 0)
        self.assertEqual(bill.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(po.lines.get().invoiced_qty, 0)
        self.assertEqual(self.balance("2150"), 500_000)
        self.assertAPReconciled()

    def test_a_credit_on_a_paid_bill_becomes_branch_credit_that_settles_a_later_bill(self):
        bill = self.non_po_bill(100_000, tax=False)
        self.pay(bill, 100_000)

        note = self.credit_note(bill, amount=40_000)
        post_vendor_credit_note(note)

        note.refresh_from_db()
        self.assertEqual(note.advance_remaining, 40_000)
        self.assertEqual(self.journal_lines(note.journal), {
            "1240": (40_000, 0), "5300": (0, 40_000),
        })
        self.assertEqual(self.balance("1240"), 40_000)
        self.assertAPReconciled()

        lekki_bill = self.non_po_bill(30_000, branch=self.lekki, day=20, tax=False)
        with self.assertRaisesMessage(
                SettlementBranchError, "Apply it to an Ikeja Branch bill."):
            allocate_vendor_credit_note(note, allocations=[(lekki_bill, 30_000)])

        later = self.non_po_bill(30_000, day=20, tax=False)
        rows = allocate_vendor_credit_note(note)
        later.refresh_from_db()
        note.refresh_from_db()
        self.assertEqual([row.vendor_invoice_id for row in rows], [later.pk])
        self.assertEqual(rows[0].effective_date, JAN(2026, 1, 20))
        self.assertEqual(later.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(note.advance_remaining, 10_000)
        self.assertEqual(self.balance("1240"), 10_000)
        lekki_bill.refresh_from_db()
        self.assertEqual(lekki_bill.amount_credited, 0)
        self.assertAPReconciled()

        void_vendor_credit_note(note, date=JAN(2026, 1, 25))
        later.refresh_from_db()
        self.assertEqual(later.balance_due, 30_000)
        self.assertEqual(self.balance("1240"), 0)
        self.assertAPReconciled()

    def branch_credit(self, amount=40_000):
        """N400 of Chuks's credit left on a paid bill, at the fixture's branch."""
        bill = self.non_po_bill(100_000, branch=self.branch_for_credit, tax=False)
        self.pay(bill, 100_000)
        note = self.credit_note(bill, amount=amount)
        post_vendor_credit_note(note)
        note.refresh_from_db()
        return note

    def unbranched_bill(self, amount):
        """A bill keyed before bills carried a branch."""
        bill = self.non_po_bill(amount, branch=self.branch_for_credit, day=20, tax=False)
        VendorInvoice.objects.filter(pk=bill.pk).update(branch=None)
        bill.refresh_from_db()
        return bill

    def test_ikejas_credit_does_not_settle_a_bill_not_yet_given_a_branch(self):
        """At a school with three branches nobody knows whose that bill's debt is."""
        self.branch_for_credit = self.ikeja
        note = self.branch_credit()
        unplaced = self.unbranched_bill(30_000)

        with self.assertRaisesMessage(SettlementBranchError, "has not been given a branch"):
            allocate_vendor_credit_note(note, allocations=[(unplaced, 30_000)])
        self.assertEqual(allocate_vendor_credit_note(note), [])
        unplaced.refresh_from_db()
        self.assertEqual(unplaced.amount_credited, 0)

    def test_at_a_one_branch_school_an_unbranched_bill_is_its_branchs(self):
        """Single Site's Main credit settles a bill keyed before bills carried a branch."""
        self.books = self.solo_books
        ProcurementSettings.objects.update_or_create(
            entity=self.books, defaults={"allow_non_po_invoices": True},
        )
        self.vendor = Vendor.objects.create(
            entity=self.books, code="CHUKS", name="Chuks Stationery",
            payable_account=self.acc("2100"), default_expense_account=self.acc("5300"),
            kyc_status="VERIFIED",
        )
        self.branch_for_credit = self.solo_main
        note = self.branch_credit()
        unplaced = self.unbranched_bill(30_000)

        rows = allocate_vendor_credit_note(note)

        self.assertEqual([row.vendor_invoice_id for row in rows], [unplaced.pk])
        unplaced.refresh_from_db()
        self.assertEqual(unplaced.payment_status, InvoicePaymentStatus.PAID)

    def test_a_credit_note_must_be_approved_before_it_posts(self):
        bill = self.non_po_bill(100_000, tax=False)
        note = self.credit_note(bill, amount=10_000, approved=False)

        with self.assertRaisesMessage(VendorCreditNoteError, "must be approved"):
            post_vendor_credit_note(note)
        note.refresh_from_db()
        self.assertEqual(note.status, DocumentStatus.DRAFT)
        self.assertIsNone(note.journal_id)

    def test_the_seeded_ladder_routes_a_credit_note_to_approval(self):
        from vs_workflow.constants import WorkflowInstanceStatus

        from .approvals import ensure_tenant_approval_templates, submit_for_approval

        ensure_tenant_approval_templates(self.tenant)
        bill = self.non_po_bill(100_000, tax=False)
        note = self.credit_note(bill, amount=10_000, approved=False)
        user = self.user_for(self.tenant, "okafor@corona.test")

        instance = submit_for_approval(note, actor_user=user)

        note.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertEqual(note.approval_state, ProcApprovalState.PENDING)
        with self.assertRaisesMessage(VendorCreditNoteError, "must be approved"):
            post_vendor_credit_note(note)

    def test_two_notes_cannot_credit_more_than_the_bill_booked(self):
        bill = self.non_po_bill(100_000, tax=False)
        first = self.credit_note(bill, amount=70_000)
        second = self.credit_note(bill, amount=70_000)
        post_vendor_credit_note(first)

        with self.assertRaises(VendorCreditNoteError):
            post_vendor_credit_note(second)
        self.assertAPReconciled()


class VendorInvoiceVoidTests(_APCorrectionsFixture):
    def test_an_unpaid_bill_is_voided_and_the_order_gets_its_quantities_back(self):
        po = self.order(10, 50_000)
        self.receive(po, 10)
        bill = self.po_bill(po, 10)
        self.assertEqual(po.lines.get().invoiced_qty, 10)

        void_vendor_invoice(bill, date=JAN(2026, 1, 20))

        bill.refresh_from_db()
        self.assertEqual(bill.status, DocumentStatus.REVERSED)
        self.assertEqual(bill.match_status, MatchStatus.NOT_MATCHED)
        self.assertEqual(bill.journal.status, DocumentStatus.REVERSED)
        self.assertEqual(po.lines.get().invoiced_qty, 0)
        self.assertEqual(self.balance("2150"), 500_000)
        self.assertEqual(self.balance("2100"), 0)
        self.assertAPReconciled()

    def test_a_paid_bill_cannot_be_voided_only_credited(self):
        bill = self.non_po_bill(100_000, tax=False)
        self.pay(bill, 40_000)

        with self.assertRaisesMessage(VendorInvoiceVoidError, "credit note"):
            void_vendor_invoice(bill)
        bill.refresh_from_db()
        self.assertEqual(bill.status, DocumentStatus.POSTED)
        self.assertAPReconciled()

    def test_a_credited_bill_cannot_be_voided(self):
        bill = self.non_po_bill(100_000, tax=False)
        post_vendor_credit_note(self.credit_note(bill, amount=10_000))

        with self.assertRaises(VendorInvoiceVoidError):
            void_vendor_invoice(bill)


class GoodsReturnTests(_APCorrectionsFixture):
    """200 exercise books were received twice by mistake."""

    def stock_item(self):
        return StockItem.objects.create(
            entity=self.books, code="EXB", name="Exercise book",
            inventory_account=self.acc("1400"), default_expense_account=self.acc("5100"),
        )

    def test_a_partial_return_takes_stock_and_gr_ir_back_out_at_receipt_cost(self):
        item = self.stock_item()
        po = self.order(10, 50_000)
        grn = self.receive(po, 10, stock_item=item)
        line = grn.lines.get()
        self.assertEqual(self.balance("2150"), 500_000)
        self.assertEqual(self.balance("1400"), 500_000)

        goods_return = return_goods(
            grn, lines=[(line.pk, Decimal("4"))], return_date=JAN(2026, 1, 9),
            reason="Delivered twice",
        )

        item.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(goods_return.status, DocumentStatus.POSTED)
        self.assertEqual(goods_return.branch_id, grn.branch_id)
        self.assertEqual(goods_return.total_value, 200_000)
        self.assertEqual((item.on_hand_qty, item.stock_value), (6, 300_000))
        self.assertEqual(line.returned_qty, 4)
        self.assertEqual(po.lines.get().received_qty, 6)
        self.assertEqual(self.balance("2150"), 300_000)
        self.assertEqual(self.balance("1400"), 300_000)
        movement = StockMovement.objects.get(movement_type=StockMovementType.RETURN)
        self.assertEqual((movement.quantity, movement.value_amount), (-4, -200_000))
        self.assertEqual(movement.location_id, self.store.pk)
        report = grir_aging(self.books)
        self.assertEqual(report.total_open, 300_000)
        self.assertEqual(report.difference, 0)
        self.assertAPReconciled()

    def test_billed_goods_go_back_only_after_the_bill_is_credited(self):
        po = self.order(10, 50_000)
        grn = self.receive(po, 10)
        bill = self.po_bill(po, 10)
        line = grn.lines.get()

        with self.assertRaisesMessage(GoodsReturnError, "Credit the bill"):
            return_goods(grn, lines=[(line.pk, Decimal("2"))],
                         return_date=JAN(2026, 1, 20), reason="Damaged")

        note = self.credit_note(bill, lines=[{"invoice_line": bill.lines.get().pk,
                                              "quantity": Decimal("2")}])
        post_vendor_credit_note(note)
        return_goods(grn, lines=[(line.pk, Decimal("2"))],
                     return_date=JAN(2026, 1, 20), reason="Damaged")

        po_line = po.lines.get()
        self.assertEqual((po_line.received_qty, po_line.invoiced_qty), (8, 8))
        self.assertEqual(self.balance("2150"), 0)
        self.assertAPReconciled()

    def test_a_receipt_returned_in_full_no_longer_counts_as_arrived(self):
        po = self.order(10, 50_000)
        grn = self.receive(po, 10)
        self.assertTrue(goods_arrived(po.goods_receipts))

        return_goods(grn, return_date=JAN(2026, 1, 9), reason="Wrong order")

        self.assertFalse(goods_arrived(po.goods_receipts))
        self.assertEqual(self.balance("2150"), 0)


class JournalScreenActionTests(_APCorrectionsFixture):
    def test_each_payables_journal_names_the_document_action_that_undoes_it(self):
        po = self.order(10, 50_000)
        grn = self.receive(po, 10)
        bill = self.po_bill(po, 8)
        note = self.credit_note(bill, amount=10_000)
        post_vendor_credit_note(note)
        note.refresh_from_db()
        goods_return = return_goods(grn, lines=[(grn.lines.get().pk, Decimal("2"))],
                                    return_date=JAN(2026, 1, 20), reason="Spare")

        for document, document_type in (
            (bill, "VENDOR_INVOICE"), (grn, "GOODS_RECEIVED_NOTE"),
            (note, "VENDOR_CREDIT_NOTE"),
        ):
            with self.subTest(document_type=document_type):
                self.assertEqual(journal_reversal_action(document.journal), {
                    "kind": "VOID_DOCUMENT", "document_type": document_type,
                    "document_id": document.pk,
                    "document_number": document.document_number,
                })
        self.assertEqual(
            journal_reversal_action(goods_return.journal)["kind"], "SOURCE_DOCUMENT_ACTION",
        )

        with self.assertRaises(PostingError) as caught:
            reverse_journal(bill.journal)
        self.assertIn(f"/procurement/vendor-invoices/{bill.pk}/void/", str(caught.exception))


class CorrectionEndpointTests(_APCorrectionsFixture):
    """The endpoints hold the keys and the branch reach of the documents they correct."""

    KEYS = (
        "procurement.vendor_invoice.view", "procurement.vendor_invoice.reverse",
        "procurement.vendor_credit_note.view", "procurement.vendor_credit_note.create",
        "procurement.goods_receipt.view", "procurement.goods_receipt.reverse",
    )

    def officer(self, email, *branches, keys=KEYS):
        user = self.user_for(self.tenant, email)
        for branch in branches:
            self.grant(user, *keys, tenant=self.tenant,
                       role_key=f"ap-officer-{next(_roles)}", branch=branch)
        return TenantAPIClient(user=user)

    def url(self, path):
        return f"/v1/procurement/{path}?entity={self.books.code}"

    def test_voiding_needs_the_reverse_key(self):
        bill = self.non_po_bill(100_000, tax=False)
        viewer = self.officer("viewer@corona.test", self.ikeja,
                              keys=("procurement.vendor_invoice.view",))

        response = viewer.post(self.url(f"vendor-invoices/{bill.pk}/void/"), {}, format="json")

        self.assertEqual(response.status_code, 403)
        bill.refresh_from_db()
        self.assertEqual(bill.status, DocumentStatus.POSTED)

    def test_another_branch_cannot_void_or_credit_ikejas_bill(self):
        bill = self.non_po_bill(100_000, tax=False)
        lekki = self.officer("lekki@corona.test", self.lekki)

        voided = lekki.post(self.url(f"vendor-invoices/{bill.pk}/void/"), {}, format="json")
        credited = lekki.post(self.url("vendor-credit-notes/"), {
            "vendor_invoice": bill.pk, "note_date": "2026-01-15",
            "reason": "Keyed wrong", "amount": 10_000,
        }, format="json")

        self.assertEqual(voided.status_code, 404)
        self.assertEqual(credited.status_code, 400)
        bill.refresh_from_db()
        self.assertEqual(bill.status, DocumentStatus.POSTED)
        self.assertFalse(VendorCreditNote.objects.exists())

    def test_ikeja_drafts_a_credit_note_that_carries_the_bills_branch(self):
        bill = self.non_po_bill(100_000, tax=False)
        ikeja = self.officer("okafor@corona.test", self.ikeja)

        response = ikeja.post(self.url("vendor-credit-notes/"), {
            "vendor_invoice": bill.pk, "note_date": "2026-01-15",
            "reason": "Keyed wrong", "amount": 10_000,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        note = VendorCreditNote.objects.get()
        self.assertEqual((note.branch_id, note.total, note.status),
                         (self.ikeja.pk, 10_000, DocumentStatus.DRAFT))

    def test_a_paid_bill_void_is_refused_with_a_conflict(self):
        bill = self.non_po_bill(100_000, tax=False)
        self.pay(bill, 100_000)
        ikeja = self.officer("okafor2@corona.test", self.ikeja)

        response = ikeja.post(self.url(f"vendor-invoices/{bill.pk}/void/"), {}, format="json")

        self.assertEqual(response.status_code, 409, response.data)

    def test_ikeja_reverses_its_own_receipt(self):
        po = self.order(10, 50_000)
        grn = self.receive(po, 10)
        ikeja = self.officer("store@corona.test", self.ikeja)
        lekki = self.officer("lekkistore@corona.test", self.lekki)

        refused = lekki.post(self.url(f"goods-receipts/{grn.pk}/reverse/"),
                             {"reason": "Wrong order", "return_date": "2026-01-09"},
                             format="json")
        accepted = ikeja.post(self.url(f"goods-receipts/{grn.pk}/reverse/"),
                              {"reason": "Wrong order", "return_date": "2026-01-09"},
                              format="json")

        self.assertEqual(refused.status_code, 404)
        self.assertEqual(accepted.status_code, 200, accepted.data)
        self.assertEqual(accepted.data["data"]["goods_return"]["total_value"], 500_000)
        self.assertEqual(self.balance("2150"), 0)
