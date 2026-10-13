"""Procurement's finance-trail entries name the document and carry no figure or address.

A purchase order emailed to a vendor is audited with the vendor's addresses, a
posted bill and a goods receipt with their values. The sentence names the
document, the vendor and the act; the addresses and values live in the entry's
metadata, which no screen serves, and the central platform trail copies none of
it. The activity feeds on a document serve the sentence an entry carries now, a
rebuilt one for an entry written before sentences were figure-free, and the
free-text reason behind the act beside it.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from vs_audit.models import AuditEvent
from vs_finance.audit import mirror_summary
from vs_finance.constants import DocumentStatus, FinanceAuditAction
from vs_finance.models import FinanceAuditLog

from .constants import ProcApprovalState, PurchaseOrderVendorDeliverySource, RfqStatus
from .models import RequestForQuotation, VendorContact
from .tests import _P2PFixtureMixin

RECIPIENT = "orders@vendor.test"
FALLBACK = "fallback@vendor.test"


class PurchaseOrderEmailAuditTests(_P2PFixtureMixin, TestCase):

    def setUp(self):
        self.entity, _, self.vendor, _, _ = self.build_p2p()
        self.vendor.email = FALLBACK
        self.vendor.address = "12 Vendor Road"
        self.vendor.save(update_fields=["email", "address", "updated_at"])
        self.user = get_user_model().objects.create_user(
            email="buyer-audit@test.com", password="pw", tenant=self.entity.tenant,
            status="ACTIVE", first_name="Purchase", last_name="Buyer",
        )

    def order(self, *, approved):
        po = self.make_po(self.entity, self.vendor, [("5300", 2, 250_000, None)])
        po.approval_state = ProcApprovalState.APPROVED if approved else ProcApprovalState.NOT_SUBMITTED
        po.status = DocumentStatus.APPROVED if approved else DocumentStatus.DRAFT
        po.save(update_fields=["approval_state", "status", "updated_at"])
        return po

    def contact(self):
        VendorContact.objects.create(
            vendor=self.vendor, name="Orders", email=RECIPIENT, receives_rfqs=False,
            receives_purchase_orders=True,
        )

    def test_scheduling_and_cancelling_name_the_order_and_no_address_or_reason(self):
        from vs_procurement import po_email

        self.contact()
        po = self.order(approved=False)
        po_email.schedule_after_approval(po, actor_user=self.user)
        po_email.cancel_awaiting(
            po, reason="Mr Eze asked us to hold it until his cheque clears", actor_user=self.user,
        )

        scheduled = FinanceAuditLog.objects.get(action=FinanceAuditAction.PURCHASE_ORDER_EMAIL_SCHEDULED)
        cancelled = FinanceAuditLog.objects.get(action=FinanceAuditAction.PURCHASE_ORDER_EMAIL_CANCELLED)
        for row in (scheduled, cancelled):
            self.assertIn(po.document_number, row.message)
            self.assertNotIn("@", row.message)
        self.assertNotIn("cheque", cancelled.message)
        self.assertEqual(scheduled.metadata["recipients"], [RECIPIENT])
        self.assertIn("cheque", cancelled.metadata["reason"])

    @patch("vs_procurement.po_email.send_notification", return_value=["notification-id"])
    def test_a_sent_order_reaches_the_central_trail_without_its_addresses(self, _notify):
        from vs_procurement import po_email

        self.contact()
        po = self.order(approved=True)
        delivery = po_email.send_approved(po, actor_user=self.user)
        self.addCleanup(delivery.pdf_file.delete, False)

        row = FinanceAuditLog.objects.get(action=FinanceAuditAction.PURCHASE_ORDER_EMAIL_QUEUED)
        self.assertEqual(row.metadata["recipients"], [RECIPIENT])
        self.assertEqual(delivery.source, PurchaseOrderVendorDeliverySource.MANUAL)
        event = AuditEvent.objects.get(
            entity_type="vs_finance.PurchaseOrder", entity_id=str(po.pk),
            metadata__finance_action=FinanceAuditAction.PURCHASE_ORDER_EMAIL_QUEUED.value,
        )
        for key in ("recipients", "bcc", "notification_ids", "delivery_id"):
            self.assertNotIn(key, event.metadata)
        self.assertNotIn("@", event.summary)
        self.assertEqual(
            event.summary,
            mirror_summary(FinanceAuditAction.PURCHASE_ORDER_EMAIL_QUEUED, po.document_number),
        )


class ProcurementSentenceTests(_P2PFixtureMixin, TestCase):

    def setUp(self):
        self.entity, _, self.vendor, _, _ = self.build_p2p()

    def test_a_posted_bill_names_the_vendor_and_no_total(self):
        from vs_procurement.payables import post_vendor_invoice

        invoice = self.make_bill(
            self.entity, self.vendor, [("5300", 1, 600_000, None, None)],
            date=datetime.date(2026, 1, 20),
        )
        post_vendor_invoice(invoice)

        row = FinanceAuditLog.objects.get(action=FinanceAuditAction.VENDOR_INVOICE_POSTED)
        self.assertEqual(row.message, f"Posted bill from {self.vendor.code}.")
        self.assertEqual(row.metadata["total"], invoice.total)

    def test_the_activity_feed_serves_an_older_sentence_rebuilt_without_its_amount(self):
        from vs_procurement.serializers import _sourcing_activity

        FinanceAuditLog.objects.create(
            entity=self.entity, action=FinanceAuditAction.QUOTATION_SUBMITTED, target_type="Quotation",
            target_id="4", document_number="QT-1",
            message="Quotation QT-1 from V001 submitted (₦2,500.00).",
        )

        (item,) = _sourcing_activity(self.entity, "Quotation", 4)

        self.assertEqual(item["message"], "Vendor quotation submitted: QT-1")
        self.assertEqual(item["reason"], "")
        self.assertNotIn("₦", item["message"])

    def test_the_cancellation_reasons_are_served_beside_the_sentence(self):
        from vs_procurement.serializers import _sourcing_activity
        from vs_procurement.sourcing import cancel_rfq, close_rfq

        user = get_user_model().objects.create_user(
            email="rfq-reason@test.com", password="pw", tenant=self.entity.tenant,
            status="ACTIVE", first_name="Rfq", last_name="Buyer",
        )
        cancelled = RequestForQuotation.objects.create(
            entity=self.entity, title="Chalk", issue_date=datetime.date(2026, 1, 10),
        )
        cancel_rfq(cancelled, reason="Requirement withdrawn", actor_user=user)
        closed = RequestForQuotation.objects.create(
            entity=self.entity, title="Markers", issue_date=datetime.date(2026, 1, 10),
            rfq_status=RfqStatus.ISSUED,
        )
        close_rfq(closed, reason="No bid was acceptable", actor_user=user)

        (cancel_item,) = _sourcing_activity(self.entity, "RequestForQuotation", cancelled.pk)
        (close_item,) = _sourcing_activity(self.entity, "RequestForQuotation", closed.pk)

        self.assertEqual(cancel_item["reason"], "Requirement withdrawn")
        self.assertEqual(close_item["reason"], "No bid was acceptable")
        self.assertNotIn("withdrawn", cancel_item["message"])
        self.assertNotIn("acceptable", close_item["message"])

    def test_a_terminated_contracts_reason_is_served_beside_the_sentence(self):
        from .constants import ContractStatus
        from .contracts import terminate_contract
        from .models import VendorContract
        from .serializers import _contract_activity

        contract = VendorContract.objects.create(
            entity=self.entity, vendor=self.vendor, reference="C-9", title="Cleaning",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
            contract_value=12_000_000, status=ContractStatus.ACTIVE,
        )
        terminate_contract(contract, reason="Vendor lost its licence")

        (item,) = _contract_activity(self.entity, contract.pk)

        self.assertEqual(item["reason"], "Vendor lost its licence")
        self.assertEqual(item["message"], "Terminated contract C-9.")
