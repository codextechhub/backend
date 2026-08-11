"""Durable purchase-order email scheduling, rendering, delivery, and retry."""
from __future__ import annotations

import io
import logging
from decimal import Decimal
from html import escape

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from vs_finance.audit import record
from vs_finance.constants import DocumentStatus, FinanceAuditAction, FinanceAuditStatus
from vs_finance.documents import _issuer_block
from vs_notifications.notify import UnregisteredRecipient, send_notification
from vs_notifications.models import Notification, NotificationStatus

from .constants import (
    ProcApprovalState,
    PurchaseOrderVendorDeliverySource,
    PurchaseOrderVendorDeliveryStatus,
)
from .models import PurchaseOrder, PurchaseOrderVendorDelivery

logger = logging.getLogger(__name__)
MAX_BUYER_MESSAGE_LENGTH = 1000


class PurchaseOrderEmailError(Exception):
    """A user-correctable purchase-order delivery error."""


def _dedupe(values):
    result = []
    seen = set()
    for value in values:
        email = str(value or "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            result.append(email)
    return result


def procurement_cc(recipients=None):
    """Return the procurement-only CC list without duplicating a direct recipient."""
    direct = {value.lower() for value in recipients or []}
    return [
        value for value in _dedupe(getattr(settings, "PROCUREMENT_VENDOR_EMAIL_CC", []))
        if value not in direct
    ]


def resolve_recipients(po: PurchaseOrder) -> list[str]:
    """Resolve explicit PO contacts first, then the primary contact, then vendor email."""
    contacts = list(po.vendor.contacts.all())
    opted_in = [
        contact.email for contact in contacts
        if contact.is_active and contact.receives_purchase_orders and contact.email
    ]
    if opted_in:
        return _dedupe(opted_in)
    primary = next(
        (contact.email for contact in contacts if contact.is_active and contact.is_primary and contact.email),
        "",
    )
    return _dedupe([primary or po.vendor.email])


def _subject(po):
    return f"Purchase order {po.document_number} from {po.entity.name}"


def _user_name(user):
    return (getattr(user, "full_name", "") or getattr(user, "email", "")).strip() if user else ""


def preview(po: PurchaseOrder) -> dict:
    recipients = resolve_recipients(po)
    return {
        "recipients": recipients,
        "cc": procurement_cc(recipients),
        "subject": _subject(po),
        "can_schedule": po.status == DocumentStatus.DRAFT
        and po.approval_state not in (ProcApprovalState.PENDING, ProcApprovalState.APPROVED),
        "can_send": po.status == DocumentStatus.APPROVED
        and po.approval_state == ProcApprovalState.APPROVED,
    }


def _clean_message(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PurchaseOrderEmailError("The buyer note must be text.")
    value = value.strip()
    if len(value) > MAX_BUYER_MESSAGE_LENGTH:
        raise PurchaseOrderEmailError("Use 1,000 characters or fewer for the buyer note.")
    return value


def _audit(delivery, action, message, *, actor_user=None, status=FinanceAuditStatus.SUCCESS):
    return record(
        entity=delivery.purchase_order.entity,
        action=action,
        actor_user=actor_user or delivery.requested_by,
        target=delivery.purchase_order,
        status=status,
        message=message,
        delivery_id=delivery.pk,
        delivery_source=delivery.source,
        recipients=delivery.recipients,
        cc=delivery.cc,
        notification_ids=delivery.notification_ids,
    )


@transaction.atomic
def schedule_after_approval(po: PurchaseOrder, *, actor_user, buyer_message=""):
    """Create one active intent that can only be released by full PO approval."""
    po = PurchaseOrder.objects.select_for_update().select_related("vendor", "entity__tenant").get(pk=po.pk)
    if po.status != DocumentStatus.DRAFT or po.approval_state in (
        ProcApprovalState.PENDING, ProcApprovalState.APPROVED,
    ):
        raise PurchaseOrderEmailError("Only a draft purchase order can schedule an approval email.")
    recipients = resolve_recipients(po)
    if not recipients:
        raise PurchaseOrderEmailError("Add a vendor email or an active purchase-order contact first.")
    PurchaseOrderVendorDelivery.objects.filter(
        purchase_order=po,
        status=PurchaseOrderVendorDeliveryStatus.AWAITING_APPROVAL,
    ).update(
        status=PurchaseOrderVendorDeliveryStatus.CANCELLED,
        cancelled_at=timezone.now(),
        failure_reason="Replaced by a newer approval submission.",
    )
    delivery = PurchaseOrderVendorDelivery.objects.create(
        purchase_order=po,
        source=PurchaseOrderVendorDeliverySource.AUTOMATIC,
        status=PurchaseOrderVendorDeliveryStatus.AWAITING_APPROVAL,
        requested_by=actor_user,
        buyer_message=_clean_message(buyer_message),
        recipients=recipients,
        cc=procurement_cc(recipients),
    )
    _audit(
        delivery, FinanceAuditAction.PURCHASE_ORDER_EMAIL_SCHEDULED,
        f"Scheduled purchase order {po.document_number} for vendor email after full approval.",
        actor_user=actor_user,
    )
    return delivery


@transaction.atomic
def cancel_awaiting(po: PurchaseOrder, *, reason: str, actor_user=None):
    deliveries = list(PurchaseOrderVendorDelivery.objects.select_for_update().filter(
        purchase_order=po, status=PurchaseOrderVendorDeliveryStatus.AWAITING_APPROVAL,
    ))
    for delivery in deliveries:
        delivery.status = PurchaseOrderVendorDeliveryStatus.CANCELLED
        delivery.cancelled_at = timezone.now()
        delivery.failure_reason = str(reason or "Approval did not complete.")[:1000]
        delivery.save(update_fields=["status", "cancelled_at", "failure_reason", "updated_at"])
        _audit(
            delivery, FinanceAuditAction.PURCHASE_ORDER_EMAIL_CANCELLED,
            f"Cancelled scheduled vendor email for purchase order {po.document_number}: {delivery.failure_reason}",
            actor_user=actor_user,
        )
    return deliveries


def release_after_commit(po_id: int, *, actor_user=None):
    """Release an approval intent after commit without ever rolling approval back."""
    def _release():
        try:
            release_awaiting(po_id, actor_user=actor_user)
        except Exception as exc:
            logger.exception("Could not release approved purchase order %s for vendor email", po_id)
            _mark_release_failed(po_id, exc, actor_user=actor_user)
    transaction.on_commit(_release)


@transaction.atomic
def _mark_release_failed(po_id: int, exc: Exception, *, actor_user=None):
    """Turn an after-commit queueing error into a visible, retryable delivery."""
    delivery = PurchaseOrderVendorDelivery.objects.select_for_update().filter(
        purchase_order_id=po_id,
        status=PurchaseOrderVendorDeliveryStatus.AWAITING_APPROVAL,
    ).order_by("-created_at").first()
    if delivery is None:
        return
    delivery.status = PurchaseOrderVendorDeliveryStatus.FAILED
    delivery.failure_reason = str(exc)[:2000] or "Purchase-order email could not be queued."
    delivery.save(update_fields=["status", "failure_reason", "updated_at"])
    _audit(
        delivery, FinanceAuditAction.PURCHASE_ORDER_EMAIL_FAILED,
        f"Purchase order {delivery.purchase_order.document_number} vendor email could not be queued.",
        actor_user=actor_user, status=FinanceAuditStatus.FAILED,
    )


def _money(po, value):
    code = getattr(po.currency, "code", "") or "NGN"
    minor_unit = getattr(po.currency, "minor_unit", 2) if po.currency else 2
    amount = Decimal(int(value or 0)) / (Decimal(10) ** minor_unit)
    return f"{code} {amount:,.{minor_unit}f}"


def _pdf_bytes(po: PurchaseOrder, delivery: PurchaseOrderVendorDelivery) -> bytes:
    issuer = _issuer_block(po.entity, branch=po.branch)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=8.5, leading=11))
    styles.add(ParagraphStyle(name="Right", parent=styles["BodyText"], alignment=TA_RIGHT, fontSize=9))
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Purchase order {po.document_number}",
    )
    story = []
    header = Table([
        [Paragraph(f"<b>{escape(issuer.get('name') or po.entity.name)}</b>", styles["Title"]),
         Paragraph("<b>PURCHASE ORDER</b>", styles["Right"])],
        [Paragraph(escape(issuer.get("address") or ""), styles["Small"]),
         Paragraph(f"<b>{escape(po.document_number)}</b>", styles["Right"])],
    ], colWidths=[115 * mm, 47 * mm])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F7FB")),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#D5DFEA")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([header, Spacer(1, 8 * mm)])
    vendor = po.vendor
    meta = Table([
        [Paragraph("<b>Vendor</b>", styles["Small"]), Paragraph("<b>Order details</b>", styles["Small"])],
        [Paragraph(
            f"{escape(vendor.name)}<br/>{escape(vendor.address or '')}<br/>{escape(vendor.email or '')}",
            styles["Small"],
        ), Paragraph(
            f"Order date: {po.order_date:%d %b %Y}<br/>"
            f"Expected date: {po.expected_date:%d %b %Y}" if po.expected_date else
            f"Order date: {po.order_date:%d %b %Y}<br/>Expected date: Not specified",
            styles["Small"],
        )],
    ], colWidths=[81 * mm, 81 * mm])
    meta.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D5DFEA")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D5DFEA")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F2747")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([meta, Spacer(1, 6 * mm)])
    rows = [["#", "Description", "Qty", "Unit price", "Tax", "Line total"]]
    for line in po.lines.all():
        rows.append([
            str(line.line_no or ""),
            Paragraph(escape(line.description), styles["Small"]),
            f"{line.quantity:g}",
            _money(po, line.unit_price),
            _money(po, line.tax_amount),
            _money(po, int(line.net_amount or 0) + int(line.tax_amount or 0)),
        ])
    lines_table = Table(rows, repeatRows=1, colWidths=[9 * mm, 61 * mm, 17 * mm, 26 * mm, 23 * mm, 28 * mm])
    lines_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F2747")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D5DFEA")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([lines_table, Spacer(1, 5 * mm)])
    totals = Table([
        ["Subtotal", _money(po, po.subtotal)],
        ["Tax", _money(po, po.tax_total)],
        ["Total", _money(po, po.total)],
    ], colWidths=[38 * mm, 35 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#0F2747")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([totals, Spacer(1, 6 * mm)])
    details = [
        ("Delivery address", po.delivery_address or "Not specified"),
        ("Payment terms", po.payment_terms or "Not specified"),
    ]
    if delivery.buyer_message:
        details.append(("Buyer note", delivery.buyer_message))
    buyer = delivery.requested_by
    if buyer:
        buyer_name = _user_name(buyer)
        details.append(("Buyer contact", f"{buyer_name} ({getattr(buyer, 'email', '')})"))
    for label, value in details:
        story.extend([
            Paragraph(f"<b>{escape(label)}</b>", styles["Small"]),
            Paragraph(escape(str(value)).replace("\n", "<br/>"), styles["Small"]),
            Spacer(1, 3 * mm),
        ])
    document.build(story)
    return buffer.getvalue()


def _context(po, delivery):
    issuer = _issuer_block(po.entity, branch=po.branch)
    buyer = delivery.requested_by
    return {
        "vendor_name": po.vendor.name,
        "issuer_name": issuer.get("name") or po.entity.name,
        "po_number": po.document_number,
        "order_date": po.order_date.strftime("%d %b %Y"),
        "expected_date": po.expected_date.strftime("%d %b %Y") if po.expected_date else "Not specified",
        "delivery_address": po.delivery_address or "Not specified",
        "payment_terms": po.payment_terms or "Not specified",
        "total": _money(po, po.total),
        "buyer_message": delivery.buyer_message,
        "buyer_name": _user_name(buyer) if buyer else "Purchasing team",
        "buyer_email": getattr(buyer, "email", "") if buyer else "",
    }


@transaction.atomic
def _queue_delivery(delivery_id: int, *, actor_user=None):
    delivery = PurchaseOrderVendorDelivery.objects.select_for_update().select_related(
        "purchase_order__vendor", "purchase_order__entity__tenant",
        "purchase_order__currency", "purchase_order__branch", "requested_by",
    ).prefetch_related("purchase_order__lines", "purchase_order__vendor__contacts").get(pk=delivery_id)
    po = delivery.purchase_order
    if delivery.status not in (
        PurchaseOrderVendorDeliveryStatus.AWAITING_APPROVAL,
        PurchaseOrderVendorDeliveryStatus.PENDING,
    ) or delivery.notification_ids:
        return delivery
    if po.status != DocumentStatus.APPROVED or po.approval_state != ProcApprovalState.APPROVED:
        raise PurchaseOrderEmailError("The purchase order must be fully approved before it can be emailed.")
    if not delivery.recipients:
        raise PurchaseOrderEmailError("No vendor email recipient is available.")
    pdf = _pdf_bytes(po, delivery)
    filename = f"Purchase-Order-{po.document_number}.pdf"
    delivery.pdf_file.save(filename, ContentFile(pdf), save=False)
    delivery.status = PurchaseOrderVendorDeliveryStatus.PENDING
    delivery.queued_at = timezone.now()
    delivery.failure_reason = ""
    delivery.save(update_fields=["pdf_file", "status", "queued_at", "failure_reason", "updated_at"])
    notification_ids = send_notification(
        event_key="procurement.purchase_order_issued",
        context=_context(po, delivery),
        recipients=[],
        tenant=po.entity.tenant,
        unregistered_recipients=[UnregisteredRecipient(email=email, name=po.vendor.name) for email in delivery.recipients],
        metadata={
            "po_delivery_id": delivery.pk,
            "cc": delivery.cc,
            "attachments": [{
                "name": filename,
                "storage_name": delivery.pdf_file.name,
                "content_type": "application/pdf",
            }],
        },
    )
    delivery.notification_ids = notification_ids
    if not notification_ids:
        delivery.status = PurchaseOrderVendorDeliveryStatus.FAILED
        delivery.failure_reason = "No email notification could be queued."
    delivery.save(update_fields=["notification_ids", "status", "failure_reason", "updated_at"])
    _audit(
        delivery,
        FinanceAuditAction.PURCHASE_ORDER_EMAIL_QUEUED if notification_ids else FinanceAuditAction.PURCHASE_ORDER_EMAIL_FAILED,
        f"Queued purchase order {po.document_number} for {len(delivery.recipients)} vendor recipient(s)."
        if notification_ids else f"Could not queue purchase order {po.document_number} for vendor email.",
        actor_user=actor_user,
        status=FinanceAuditStatus.SUCCESS if notification_ids else FinanceAuditStatus.FAILED,
    )
    return delivery


def release_awaiting(po_id: int, *, actor_user=None):
    delivery = PurchaseOrderVendorDelivery.objects.filter(
        purchase_order_id=po_id,
        status=PurchaseOrderVendorDeliveryStatus.AWAITING_APPROVAL,
    ).order_by("-created_at").first()
    return _queue_delivery(delivery.pk, actor_user=actor_user) if delivery else None


@transaction.atomic
def send_approved(po: PurchaseOrder, *, actor_user, buyer_message="", parent=None):
    po = PurchaseOrder.objects.select_for_update().select_related("vendor", "entity__tenant").prefetch_related(
        "vendor__contacts",
    ).get(pk=po.pk)
    if po.status != DocumentStatus.APPROVED or po.approval_state != ProcApprovalState.APPROVED:
        raise PurchaseOrderEmailError("Only a fully approved purchase order can be emailed to its vendor.")
    recipients = resolve_recipients(po)
    if not recipients:
        raise PurchaseOrderEmailError("Add a vendor email or an active purchase-order contact first.")
    delivery = PurchaseOrderVendorDelivery.objects.create(
        purchase_order=po,
        source=PurchaseOrderVendorDeliverySource.RETRY if parent else PurchaseOrderVendorDeliverySource.MANUAL,
        status=PurchaseOrderVendorDeliveryStatus.PENDING,
        requested_by=actor_user,
        parent=parent,
        buyer_message=_clean_message(buyer_message),
        recipients=recipients,
        cc=procurement_cc(recipients),
    )
    return _queue_delivery(delivery.pk, actor_user=actor_user)


def retry(delivery: PurchaseOrderVendorDelivery, *, actor_user, buyer_message=None):
    if delivery.status != PurchaseOrderVendorDeliveryStatus.FAILED:
        raise PurchaseOrderEmailError("Only a failed purchase-order email can be retried.")
    message = delivery.buyer_message if buyer_message is None else buyer_message
    return send_approved(
        delivery.purchase_order, actor_user=actor_user, buyer_message=message, parent=delivery,
    )


@transaction.atomic
def update_from_notification(notification: Notification, *, success: bool):
    delivery_id = (notification.metadata or {}).get("po_delivery_id")
    if not delivery_id:
        return
    delivery = PurchaseOrderVendorDelivery.objects.select_for_update().filter(pk=delivery_id).first()
    if delivery is None or delivery.status not in (
        PurchaseOrderVendorDeliveryStatus.PENDING,
        PurchaseOrderVendorDeliveryStatus.FAILED,
    ):
        return
    statuses = list(Notification.objects.filter(
        id__in=delivery.notification_ids,
    ).values_list("status", flat=True))
    if not statuses or any(value == NotificationStatus.PENDING for value in statuses):
        return
    now = timezone.now()
    if all(value == NotificationStatus.SENT for value in statuses):
        delivery.status = PurchaseOrderVendorDeliveryStatus.SENT
        delivery.sent_at = now
        delivery.failure_reason = ""
        action = FinanceAuditAction.PURCHASE_ORDER_EMAIL_SENT
        audit_status = FinanceAuditStatus.SUCCESS
        message = f"Sent purchase order {delivery.purchase_order.document_number} to its vendor."
    else:
        failures = Notification.objects.filter(
            id__in=delivery.notification_ids, status=NotificationStatus.FAILED,
        ).values_list("failure_reason", flat=True)
        delivery.status = PurchaseOrderVendorDeliveryStatus.FAILED
        delivery.failure_reason = "; ".join(value for value in failures if value)[:2000] or "Email delivery failed."
        action = FinanceAuditAction.PURCHASE_ORDER_EMAIL_FAILED
        audit_status = FinanceAuditStatus.FAILED
        message = f"Purchase order {delivery.purchase_order.document_number} vendor email failed."
    delivery.save(update_fields=["status", "sent_at", "failure_reason", "updated_at"])
    _audit(delivery, action, message, status=audit_status)
