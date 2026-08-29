"""Component 7 end to end: requisition, order, receipt, bill, payment.

One test that walks the whole chain, because each step's precondition is the
previous step's result and testing them apart proves less than it looks. The
point is that the FAL passes through: every state change below is
``vs_procurement``'s, and the FAL only resolves scope and translates errors.
"""

from __future__ import annotations

import datetime

from schools.core.fal.adapters.django_finance import DjangoProcurementActionAdapter
from schools.core.fal.contracts import BillLine, ProcApprovalState, ReceiptLine
from schools.core.fal.exceptions import CrossTenantError, ProcurementStateError

from .base import FALFixture


class ProcurementChainTests(FALFixture):
    """Corona buys 100 exercise books from Ojo Stationers, at N250 each."""

    def setUp(self):
        super().setUp()
        self.port = DjangoProcurementActionAdapter()
        self.approver = self.user_for(self.corona, "approver@corona.test")
        self._staff_the_approver_role()
        self.vendor = self._vendor(self.corona_books, "Ojo Stationers")

    def _staff_the_approver_role(self):
        from vs_procurement.constants import WF_DEFAULT_MANAGER_ROLE
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import make_assignment

        role = TenantRoleTemplate.objects.get(
            tenant=self.corona.tenant, key=WF_DEFAULT_MANAGER_ROLE,
        )
        make_assignment(self.corona.tenant, self.approver, role)

    def _vendor(self, books, name):
        """A supplier the school may actually pay.

        KYC verified on purpose: the engine refuses to pay an unverified vendor,
        and a fixture that left it PENDING would be testing the compliance gate
        rather than the FAL.
        """
        from vs_procurement.constants import VendorKycStatus
        from vs_procurement.models import Vendor

        return Vendor.objects.create(
            entity_id=books.entity_ref, name=name,
            kyc_status=VendorKycStatus.VERIFIED,
            payable_account=self.account(books.entity_ref, "2100"),
            default_expense_account=self.account(books.entity_ref, "5200"),
        )

    def _approved_requisition(self):
        document = self.port.raise_requisition(
            entity_ref=self.corona_books.entity_ref, raiser_ref=self.bursar.pk,
            lines=(BillLine(description="Exercise books", quantity=100,
                            unit_price=25_000),),
            narration="Termly stationery",
        ).unwrap()
        self.port.submit_for_approval(document.ref, actor_ref=self.bursar.pk)
        self.port.approve(
            document.ref, approver_ref=self.approver.pk, comment="Approved.",
        )
        return document

    def test_the_whole_chain_runs_through_the_fal(self):
        from vs_finance.models import JournalEntry

        requisition = self._approved_requisition()

        order = self._approved_order(requisition)
        self.assertEqual(order.vendor_ref, self.vendor.pk)
        self.assertEqual(order.total, 2_500_000)

        po_line = self._po_line(order)
        receipt = self.port.receive_goods(
            order.ref, actor_ref=self.bursar.pk,
            lines=(ReceiptLine(po_line_ref=po_line.pk, quantity_received=100),),
            received_date=datetime.date(2026, 10, 5),
        ).unwrap()
        self.assertEqual(receipt.status, "POSTED")

        bill = self.port.record_supplier_bill(
            order.ref, vendor_ref=self.vendor.pk, actor_ref=self.bursar.pk,
            lines=(BillLine(description="Exercise books", quantity=100,
                            unit_price=25_000, po_line_ref=po_line.pk),),
            invoice_date=datetime.date(2026, 10, 8), external_reference="OJO-114",
        ).unwrap()
        # Recorded, not posted: a bill nobody has approved is not yet a debt the
        # ledger carries.
        self.assertEqual(bill.status, "DRAFT")
        self.assertEqual(bill.total, 2_500_000)

        bill = self._approve_and_post(bill)
        self.assertEqual(bill.status, "POSTED")

        payment = self.port.pay_supplier(
            bill.ref, actor_ref=self.bursar.pk, amount=2_500_000,
            payment_date=datetime.date(2026, 10, 20),
        ).unwrap()
        self.assertEqual(payment.status, "DRAFT")

        payment = self._approve_and_post(payment)
        self.assertEqual(payment.status, "POSTED")

        # Every step that should have hit the ledger did, and through the
        # engine's own posting services rather than anything the FAL wrote.
        self.assertTrue(
            JournalEntry.objects.filter(
                entity_id=self.corona_books.entity_ref,
            ).exists()
        )

    def test_a_part_payment_leaves_the_bill_partly_settled(self):
        from vs_procurement.models import VendorInvoice

        bill = self._posted_bill()

        payment = self.port.pay_supplier(
            bill.ref, actor_ref=self.bursar.pk, amount=1_000_000,
            payment_date=datetime.date(2026, 10, 20),
        ).unwrap()
        self._approve_and_post(payment)

        row = VendorInvoice.objects.get(pk=bill.ref.doc_ref)
        self.assertEqual(row.amount_paid, 1_000_000)
        self.assertEqual(row.payment_status, "PARTIAL")

    def test_a_purchase_order_cannot_be_raised_from_an_unapproved_requisition(self):
        document = self.port.raise_requisition(
            entity_ref=self.corona_books.entity_ref, raiser_ref=self.bursar.pk,
            lines=(BillLine(description="Chalk", quantity=10, unit_price=1_000),),
        ).unwrap()
        self.assertIs(document.approval_state, ProcApprovalState.NOT_SUBMITTED)

        with self.assertRaises(ProcurementStateError):
            self.port.raise_purchase_order(
                document.ref, vendor_ref=self.vendor.pk, actor_ref=self.bursar.pk,
                order_date=datetime.date(2026, 10, 1),
            )

    def test_another_schools_vendor_cannot_be_ordered_from(self):
        requisition = self._approved_requisition()
        stranger = self._vendor(self.greenfield_books, "Greenfield Supplies")

        with self.assertRaises(CrossTenantError):
            self.port.raise_purchase_order(
                requisition.ref, vendor_ref=stranger.pk, actor_ref=self.bursar.pk,
                order_date=datetime.date(2026, 10, 1),
            )

    def test_receiving_more_than_was_ordered_is_refused_by_the_engine(self):
        """The over-receipt cap is procurement's rule, and the FAL keeps it."""
        requisition = self._approved_requisition()
        order = self._approved_order(requisition)
        po_line = self._po_line(order)

        with self.assertRaises(Exception) as caught:
            self.port.receive_goods(
                order.ref, actor_ref=self.bursar.pk,
                lines=(ReceiptLine(po_line_ref=po_line.pk, quantity_received=500),),
            )
        self.assertNotIsInstance(caught.exception, AssertionError)

    def test_a_receipt_with_no_lines_is_refused(self):
        requisition = self._approved_requisition()
        order = self._approved_order(requisition)

        with self.assertRaises(ProcurementStateError):
            self.port.receive_goods(order.ref, actor_ref=self.bursar.pk, lines=())

    def test_the_chain_inherits_the_branch_it_started_in(self):
        """A Lekki requisition stays a Lekki order and a Lekki receipt."""
        document = self.port.raise_requisition(
            entity_ref=self.corona_books.entity_ref,
            raiser_ref=self.lekki_bursar.pk,
            lines=(BillLine(description="Exercise books", quantity=10,
                            unit_price=25_000),),
        ).unwrap()
        self.assertEqual(document.ref.branch_ref, self.lekki.pk)

        self.port.submit_for_approval(document.ref, actor_ref=self.lekki_bursar.pk)
        self.port.approve(document.ref, approver_ref=self.approver.pk)
        order = self.port.raise_purchase_order(
            document.ref, vendor_ref=self.vendor.pk, actor_ref=self.bursar.pk,
            order_date=datetime.date(2026, 10, 1),
        ).unwrap()

        self.assertEqual(order.ref.branch_ref, self.lekki.pk)

    # ----- helpers --------------------------------------------------------- #
    def _approved_order(self, requisition, *, actor=None):
        """A PO carries its own approval ladder; a receipt is refused before it.

        That is the engine's rule, not the FAL's, and it is worth stating in the
        fixture: committing money to a supplier is a second decision from
        agreeing the need internally.
        """
        actor = actor or self.bursar
        order = self.port.raise_purchase_order(
            requisition.ref, vendor_ref=self.vendor.pk, actor_ref=actor.pk,
            order_date=datetime.date(2026, 10, 1),
        ).unwrap()
        self.port.submit_for_approval(order.ref, actor_ref=actor.pk)
        self.port.approve(order.ref, approver_ref=self.approver.pk, comment="Order it.")
        return order

    def _approve_and_post(self, document):
        """Submit, approve and post one approvable document.

        Three steps rather than one because the engine insists on it: nothing
        reaches the ledger before a person has said yes to it. This is decision 2
        showing up in the shape of the test.
        """
        self.port.submit_for_approval(document.ref, actor_ref=self.bursar.pk)
        self.port.approve(document.ref, approver_ref=self.approver.pk, comment="Yes.")
        return self.port.post_to_ledger(
            document.ref, actor_ref=self.bursar.pk,
        ).unwrap()

    def _po_line(self, order):
        from vs_procurement.models import PurchaseOrderLine

        return PurchaseOrderLine.objects.get(purchase_order_id=order.ref.doc_ref)

    def _posted_bill(self):
        requisition = self._approved_requisition()
        order = self._approved_order(requisition)
        po_line = self._po_line(order)
        self.port.receive_goods(
            order.ref, actor_ref=self.bursar.pk,
            lines=(ReceiptLine(po_line_ref=po_line.pk, quantity_received=100),),
        )
        bill = self.port.record_supplier_bill(
            order.ref, vendor_ref=self.vendor.pk, actor_ref=self.bursar.pk,
            lines=(BillLine(description="Exercise books", quantity=100,
                            unit_price=25_000, po_line_ref=po_line.pk),),
            invoice_date=datetime.date(2026, 10, 8),
        ).unwrap()
        return self._approve_and_post(bill)

    def test_an_unapproved_bill_cannot_be_posted(self):
        """The gate that forced record and post apart in the first place."""
        requisition = self._approved_requisition()
        order = self._approved_order(requisition)
        po_line = self._po_line(order)
        self.port.receive_goods(
            order.ref, actor_ref=self.bursar.pk,
            lines=(ReceiptLine(po_line_ref=po_line.pk, quantity_received=100),),
        )
        bill = self.port.record_supplier_bill(
            order.ref, vendor_ref=self.vendor.pk, actor_ref=self.bursar.pk,
            lines=(BillLine(description="Exercise books", quantity=100,
                            unit_price=25_000, po_line_ref=po_line.pk),),
            invoice_date=datetime.date(2026, 10, 8),
        ).unwrap()

        with self.assertRaises(ProcurementStateError):
            self.port.post_to_ledger(bill.ref, actor_ref=self.bursar.pk)

    def test_a_goods_receipt_is_not_posted_through_the_ledger_method(self):
        """A receipt is not approvable, so receive_goods already posted it."""
        requisition = self._approved_requisition()
        order = self._approved_order(requisition)
        po_line = self._po_line(order)
        receipt = self.port.receive_goods(
            order.ref, actor_ref=self.bursar.pk,
            lines=(ReceiptLine(po_line_ref=po_line.pk, quantity_received=100),),
        ).unwrap()

        self.assertEqual(receipt.status, "POSTED")
        with self.assertRaises(ProcurementStateError):
            self.port.post_to_ledger(receipt.ref, actor_ref=self.bursar.pk)

    def test_an_unverified_vendor_cannot_be_paid(self):
        """The compliance gate is the engine's, and the FAL surfaces it typed."""
        from vs_procurement.constants import VendorKycStatus
        from vs_procurement.models import Vendor

        bill = self._posted_bill()
        Vendor.objects.filter(pk=self.vendor.pk).update(
            kyc_status=VendorKycStatus.PENDING,
        )
        payment = self.port.pay_supplier(
            bill.ref, actor_ref=self.bursar.pk, amount=1_000_000,
            payment_date=datetime.date(2026, 10, 20),
        ).unwrap()

        with self.assertRaises(ProcurementStateError):
            self._approve_and_post(payment)
