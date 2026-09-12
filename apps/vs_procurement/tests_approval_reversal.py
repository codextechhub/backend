"""An approval cannot be undone once the decision behind it has taken effect.

``vs_workflow`` can withdraw its own record of an approver's vote; it cannot
withdraw what that vote released. What approval releases differs by document type,
so each procurement handler answers ``reversal_block_reason`` for its own type and
the shared ``validate_reversal`` raises the refusal in one shape.

These tests hold every type to that answer, and to the other side of it: an order
that has reached its vendor or taken delivery, a requisition an order has been
raised from, and a bill or a payment that has reached the ledger are all refused,
while a document whose approval has released nothing yet is still reversible.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from vs_finance.constants import DocumentStatus
from vs_workflow.constants import (
    WorkflowInstanceStatus, WorkflowStageAction as ActionEnum,
)
from vs_workflow.exceptions import ReversalNotAllowedError
from vs_workflow.models import WorkflowStageAction
from vs_workflow.services import actions as wf_actions
from vs_workflow.services.approvers import EligibleApprover

from vs_procurement.approvals import (
    ensure_tenant_approval_templates, submit_for_approval,
)
from vs_procurement.constants import (
    PROCUREMENT_APPROVAL_TYPES,
    ProcApprovalState,
    PurchaseOrderVendorDeliverySource,
    PurchaseOrderVendorDeliveryStatus,
)
from vs_procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderVendorDelivery,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    VendorInvoice,
    VendorInvoiceLine,
    VendorPayment,
)
from vs_procurement.payables import (
    post_vendor_invoice, post_vendor_payment, price_vendor_invoice,
)
from vs_procurement.purchasing import create_po_from_requisition, post_grn, price_po
from vs_procurement.tests import _P2PFixtureMixin, _platform_tenant


class ProcurementApprovalReversalTests(_P2PFixtureMixin, TestCase):
    """One refusal case and, where it exists, one still-reversible case per type."""

    def setUp(self):
        self.entity, _period, self.vendor, _vat, _wht = self.build_p2p()
        ensure_tenant_approval_templates(self.entity.tenant)
        self.requester = self._user("requester@reversal.test")
        self.approver = self._user("approver@reversal.test")
        self.admin = self._user("admin@reversal.test")

    # -- people and documents ------------------------------------------------ #

    @staticmethod
    def _user(email):
        return get_user_model().objects.create_user(
            tenant=_platform_tenant(), email=email, first_name="T", last_name="U",
        )

    def _requisition(self, *, unit_price=100_000):
        requisition = PurchaseRequisition.objects.create(
            entity=self.entity, request_date=datetime.date(2026, 1, 3),
            requested_by=self.requester,
        )
        PurchaseRequisitionLine.objects.create(
            requisition=requisition, line_no=1, description="Chair", quantity=1,
            estimated_unit_price=unit_price,
            expense_account=self.acc(self.entity, "5300"),
        )
        requisition.recompute_total(save=True)
        return requisition

    def _purchase_order(self, *, unit_price=100_000):
        order = PurchaseOrder.objects.create(
            entity=self.entity, vendor=self.vendor,
            order_date=datetime.date(2026, 1, 5),
        )
        PurchaseOrderLine.objects.create(
            purchase_order=order, description="Chair",
            expense_account=self.acc(self.entity, "5300"),
            quantity=1, unit_price=unit_price, line_no=1,
        )
        price_po(order)
        order.refresh_from_db()
        return order

    def _bill(self, *, total=100_000):
        bill = VendorInvoice.objects.create(
            entity=self.entity, vendor=self.vendor,
            invoice_date=datetime.date(2026, 1, 10),
            due_date=datetime.date(2026, 1, 20),
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=bill, expense_account=self.acc(self.entity, "5300"),
            quantity=1, unit_price=total, line_no=1,
        )
        price_vendor_invoice(bill)
        bill.refresh_from_db()
        return bill

    def _payment(self, *, gross=100_000):
        return VendorPayment.objects.create(
            entity=self.entity, vendor=self.vendor,
            payment_date=datetime.date(2026, 1, 15), gross_amount=gross,
            payment_account=self.acc(self.entity, "1100"),
        )

    # -- running the ladder -------------------------------------------------- #

    def _approve(self, document):
        """Submit ``document`` and vote it through every stage it reaches."""
        with patch(
            "vs_workflow.services.approvers.resolve_approvers",
            return_value=[EligibleApprover(user=self.approver)],
        ):
            instance = submit_for_approval(document, actor_user=self.requester)
            for _attempt in range(3):
                instance.refresh_from_db()
                if instance.status == WorkflowInstanceStatus.APPROVED:
                    break
                wf_actions.record_action(
                    instance.id, self.approver, ActionEnum.APPROVED)
        instance.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)
        self.assertEqual(document.approval_state, ProcApprovalState.APPROVED)
        return instance

    def _live_vote(self, instance):
        """The first vote on the instance that has not itself been reversed."""
        return WorkflowStageAction.objects.filter(
            stage_instance__instance=instance, actor=self.approver,
            is_reversal_of__isnull=True, reversed_at__isnull=True,
        ).order_by("id").first()

    def _reverse(self, instance, reason="the wrong manager was asked"):
        return wf_actions.reverse_action(
            self._live_vote(instance).id, self.admin, reason=reason)

    def _assert_refused(self, instance, document, expected_phrase):
        """The reversal is refused and nothing about the decision is written."""
        with self.assertRaises(ReversalNotAllowedError) as caught:
            self._reverse(instance)
        self.assertIn(expected_phrase, str(caught.exception))

        instance.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)
        self.assertEqual(document.approval_state, ProcApprovalState.APPROVED)
        self.assertIsNone(self._live_vote(instance).reversed_at)

    # -- requisition --------------------------------------------------------- #

    def test_a_requisition_an_order_was_raised_from_cannot_be_reversed(self):
        """The order is a commitment to a vendor; the vote behind it is not.

        Undoing the vote would put the requisition back under review with an order
        already placed against it.
        """
        requisition = self._requisition()
        instance = self._approve(requisition)
        create_po_from_requisition(
            requisition, vendor=self.vendor, order_date=datetime.date(2026, 1, 6),
        )

        self._assert_refused(
            instance, requisition,
            "A purchase order has already been raised from this requisition",
        )

    def test_a_requisition_with_no_order_yet_is_still_reversible(self):
        """Approval that has released nothing is exactly what reversal is for."""
        requisition = self._requisition()
        instance = self._approve(requisition)

        self._reverse(instance)

        instance.refresh_from_db()
        requisition.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertEqual(requisition.approval_state, ProcApprovalState.PENDING)
        self.assertEqual(requisition.status, DocumentStatus.PENDING_APPROVAL)

    # -- purchase order ------------------------------------------------------ #

    def test_a_purchase_order_already_sent_to_its_vendor_cannot_be_reversed(self):
        order = self._purchase_order()
        instance = self._approve(order)
        PurchaseOrderVendorDelivery.objects.create(
            purchase_order=order,
            source=PurchaseOrderVendorDeliverySource.AUTOMATIC,
            status=PurchaseOrderVendorDeliveryStatus.SENT,
        )

        self._assert_refused(
            instance, order, "This purchase order has already gone to the vendor",
        )

    def test_a_purchase_order_the_goods_arrived_against_cannot_be_reversed(self):
        """An order whose email never went out can still have been placed by phone.

        Once the receipt is posted the stock and the GR/IR liability exist, and no
        reversal of the approval takes either of them back.
        """
        order = self._purchase_order()
        instance = self._approve(order)
        receipt = self.make_grn(
            self.entity, self.vendor, order, [(order.lines.first(), 1)],
        )
        post_grn(receipt)

        self._assert_refused(
            instance, order,
            "Goods have already been received against this purchase order",
        )

    def test_a_purchase_order_that_released_nothing_is_still_reversible(self):
        order = self._purchase_order()
        instance = self._approve(order)

        self._reverse(instance)

        instance.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertEqual(order.approval_state, ProcApprovalState.PENDING)

    # -- vendor invoice ------------------------------------------------------ #

    def test_a_posted_bill_cannot_be_reversed(self):
        """Approval is the permission to post, and the journal outlives the vote."""
        bill = self._bill()
        instance = self._approve(bill)
        post_vendor_invoice(bill)
        bill.refresh_from_db()
        self.assertEqual(bill.status, DocumentStatus.POSTED)

        self._assert_refused(instance, bill, "This bill is posted")

    def test_a_bill_still_in_draft_is_reversible(self):
        bill = self._bill()
        instance = self._approve(bill)

        self._reverse(instance)

        instance.refresh_from_db()
        bill.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertEqual(bill.approval_state, ProcApprovalState.PENDING)

    # -- vendor payment ------------------------------------------------------ #

    def test_a_posted_payment_cannot_be_reversed(self):
        """The money has gone. The ledger says paid; the approval must not say pending."""
        settled = self.make_bill(
            self.entity, self.vendor, [("5300", 1, 100_000, None, None)])
        post_vendor_invoice(settled)
        payment = self._payment()
        instance = self._approve(payment)
        post_vendor_payment(payment)
        payment.refresh_from_db()
        self.assertEqual(payment.status, DocumentStatus.POSTED)

        self._assert_refused(instance, payment, "This payment is posted")

    def test_a_payment_still_in_draft_is_reversible(self):
        payment = self._payment()
        instance = self._approve(payment)

        self._reverse(instance)

        instance.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertEqual(payment.approval_state, ProcApprovalState.PENDING)

    # -- the whole registry -------------------------------------------------- #

    def test_every_procurement_document_type_answers_the_reversal_check(self):
        """Enumerated rather than sampled, so a type added later is caught here.

        The defect this replaces was one type answering and the other three
        reversing silently while their effect stood.
        """
        from vs_workflow.handlers import get_handler
        from vs_procurement.workflow_handlers import _ProcApprovalHandler

        for document_type in PROCUREMENT_APPROVAL_TYPES:
            with self.subTest(document_type=document_type):
                handler = get_handler(document_type)
                self.assertNotEqual(
                    type(handler).reversal_block_reason,
                    _ProcApprovalHandler.reversal_block_reason,
                    f"{document_type} does not answer the reversal check for itself",
                )
