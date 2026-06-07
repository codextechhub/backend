"""Sourcing services — competitive quotation before commitment.

The pre-PO funnel: a buyer issues a :class:`~vs_procurement.models.RequestForQuotation`
(optionally off an approved requisition), vendors reply with
:class:`~vs_procurement.models.VendorQuotation` s, and the winning quote is **awarded** —
which converts it into a DRAFT :class:`~vs_procurement.models.PurchaseOrder` ready to be
issued and received against. None of this touches the General Ledger; the first GL event
is still the goods receipt on the resulting PO. All money is integer kobo.
"""
from __future__ import annotations

import datetime

from django.db import transaction

from vs_finance.audit import record
from vs_finance.constants import FinanceAuditAction
from vs_finance.receivables import compute_line_net, compute_tax

from .constants import QuotationStatus, RfqStatus
from .exceptions import SourcingError
from .purchasing import price_po


# --------------------------------------------------------------------------- #
# RFQ lifecycle                                                                #
# --------------------------------------------------------------------------- #

def issue_rfq(rfq, *, actor_user=None):
    """Move a DRAFT RFQ to ISSUED so vendors can quote against it. Requires lines."""
    if rfq.rfq_status != RfqStatus.DRAFT:
        raise SourcingError(
            f"RFQ {rfq.document_number or rfq.pk} is '{rfq.rfq_status}'; "
            f"only a draft RFQ can be issued.",
        )
    if not rfq.lines.exists():
        raise SourcingError("An RFQ needs at least one line before it can be issued.")
    rfq.rfq_status = RfqStatus.ISSUED
    rfq.save(update_fields=["rfq_status", "updated_at"])
    record(
        entity=rfq.entity, action=FinanceAuditAction.RFQ_ISSUED,
        actor_user=actor_user, target=rfq,
        message=f"Issued RFQ {rfq.document_number} ({rfq.lines.count()} line(s)).",
    )
    return rfq


def cancel_rfq(rfq, *, reason="", actor_user=None):
    """Abandon an RFQ. Idempotent on terminal states (AWARDED/CLOSED/CANCELLED)."""
    if rfq.rfq_status in (RfqStatus.AWARDED, RfqStatus.CLOSED, RfqStatus.CANCELLED):
        if rfq.rfq_status == RfqStatus.AWARDED:
            raise SourcingError("An awarded RFQ cannot be cancelled.")
        return rfq
    rfq.rfq_status = RfqStatus.CANCELLED
    rfq.save(update_fields=["rfq_status", "updated_at"])
    record(
        entity=rfq.entity, action=FinanceAuditAction.RFQ_CANCELLED,
        actor_user=actor_user, target=rfq,
        message=f"Cancelled RFQ {rfq.document_number}."
                + (f" Reason: {reason}" if reason else ""),
    )
    return rfq


# --------------------------------------------------------------------------- #
# Quotation lifecycle                                                          #
# --------------------------------------------------------------------------- #

def price_quotation(quotation) -> None:
    """Compute each quotation line's ``net_amount``/``tax_amount`` and roll up totals."""
    from .models import VendorQuotationLine

    for line in quotation.lines.all():
        net = compute_line_net(line.quantity, line.unit_price)
        rate = line.tax_code.rate_bps if line.tax_code_id else 0
        tax = compute_tax(net, rate)
        if line.net_amount != net or line.tax_amount != tax:
            VendorQuotationLine.objects.filter(pk=line.pk).update(
                net_amount=net, tax_amount=tax,
            )
    quotation.recompute_totals(save=True)


def submit_quotation(quotation, *, actor_user=None):
    """Record a vendor's quotation as a firm SUBMITTED offer against an issued RFQ."""
    if quotation.quotation_status != QuotationStatus.DRAFT:
        raise SourcingError(
            f"Quotation {quotation.document_number or quotation.pk} is "
            f"'{quotation.quotation_status}'; only a draft quotation can be submitted.",
        )
    if quotation.rfq.rfq_status != RfqStatus.ISSUED:
        raise SourcingError(
            f"RFQ {quotation.rfq.document_number} is '{quotation.rfq.rfq_status}'; "
            f"quotations can only be submitted while it is ISSUED.",
        )
    if not quotation.lines.exists():
        raise SourcingError("A quotation needs at least one priced line.")

    price_quotation(quotation)
    quotation.quotation_status = QuotationStatus.SUBMITTED
    quotation.save(update_fields=["quotation_status", "updated_at"])
    record(
        entity=quotation.entity, action=FinanceAuditAction.QUOTATION_SUBMITTED,
        actor_user=actor_user, target=quotation,
        message=f"Quotation {quotation.document_number} from {quotation.vendor.code} "
                f"submitted ({quotation.total} kobo).",
        rfq_id=quotation.rfq_id, total=quotation.total,
    )
    return quotation


@transaction.atomic
def award_quotation(quotation, *, order_date=None, actor_user=None):
    """Award a SUBMITTED quotation: build a DRAFT PO from it and reject the losers.

    Sets the quotation AWARDED, links the new :class:`PurchaseOrder` it produced, marks
    the RFQ AWARDED, and flips every other still-in-contention quotation on the same RFQ
    to REJECTED. The PO carries each quoted line's price and expense account (falling back
    to the vendor's / category's default). Returns the created PO.
    """
    from .models import PurchaseOrder, PurchaseOrderLine

    if quotation.quotation_status != QuotationStatus.SUBMITTED:
        raise SourcingError(
            f"Quotation {quotation.document_number or quotation.pk} is "
            f"'{quotation.quotation_status}'; only a submitted quotation can be awarded.",
        )
    rfq = quotation.rfq
    if rfq.rfq_status != RfqStatus.ISSUED:
        raise SourcingError(
            f"RFQ {rfq.document_number} is '{rfq.rfq_status}'; only an issued RFQ "
            f"can be awarded.",
        )
    if not quotation.lines.exists():
        raise SourcingError("Cannot award a quotation with no lines.")

    vendor = quotation.vendor
    default_expense = (
        vendor.default_expense_account
        or (vendor.category.default_expense_account if vendor.category_id else None)
    )

    po = PurchaseOrder.objects.create(
        entity=quotation.entity, branch=quotation.branch,
        vendor=vendor, requisition=rfq.requisition,
        order_date=order_date or datetime.date.today(),
        currency=quotation.currency, created_by=actor_user,
        reference=quotation.reference,
        narration=f"From quotation {quotation.document_number} (RFQ {rfq.document_number}).",
    )
    for qline in quotation.lines.all().order_by("line_no", "id"):
        expense = qline.expense_account or default_expense
        if expense is None:
            raise SourcingError(
                f"Quotation line '{qline.description}' has no expense account and the "
                f"vendor has no default — set one before awarding.",
            )
        PurchaseOrderLine.objects.create(
            purchase_order=po,
            requisition_line=qline.rfq_line.requisition_line if qline.rfq_line_id else None,
            description=qline.description, expense_account=expense,
            quantity=qline.quantity, unit_price=qline.unit_price,
            tax_code=qline.tax_code, line_no=qline.line_no,
        )
    price_po(po)

    quotation.quotation_status = QuotationStatus.AWARDED
    quotation.awarded_po = po
    quotation.save(update_fields=["quotation_status", "awarded_po", "updated_at"])

    # Reject the remaining contenders on this RFQ.
    rfq.quotations.exclude(pk=quotation.pk).filter(
        quotation_status=QuotationStatus.SUBMITTED,
    ).update(quotation_status=QuotationStatus.REJECTED)

    rfq.rfq_status = RfqStatus.AWARDED
    rfq.save(update_fields=["rfq_status", "updated_at"])

    record(
        entity=quotation.entity, action=FinanceAuditAction.QUOTATION_AWARDED,
        actor_user=actor_user, target=quotation,
        message=f"Awarded quotation {quotation.document_number} from {vendor.code} → "
                f"PO {po.document_number} ({po.total} kobo).",
        rfq_id=rfq.pk, purchase_order_id=po.pk, total=po.total,
    )
    return po
