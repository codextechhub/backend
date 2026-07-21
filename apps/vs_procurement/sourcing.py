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
from vs_finance.money import format_naira
from vs_finance.receivables import compute_line_net, compute_tax

from .constants import QuotationStatus, RfqStatus
from .exceptions import SourcingError
from .purchasing import price_po, vendor_purchase_block_reason


# --------------------------------------------------------------------------- #
# RFQ invitations (the RFQ's addressee list)                                   #
# --------------------------------------------------------------------------- #

def set_rfq_invitations(rfq, vendors, *, actor_user=None):
    """Replace the invited-vendor set on a **DRAFT** RFQ.

    ``vendors`` is an iterable of :class:`Vendor` objects. Each must belong to the RFQ's
    entity and pass :func:`vendor_purchase_block_reason`; the list is de-duplicated. A
    vendor that has **already responded** (a quotation exists from it on this RFQ) may
    not be dropped — removing its invitation would strand that bid's history, so the call
    is rejected with a clear error rather than silently deleting it.

    Only a draft RFQ's addressee list is editable; once issued the invitations are the
    firm list of vendors the RFQ was sent to.
    """
    from .models import RfqInvitation, VendorQuotation

    if rfq.rfq_status != RfqStatus.DRAFT:
        raise SourcingError(
            f"RFQ {rfq.document_number or rfq.pk} is '{rfq.rfq_status}'; "
            f"invited vendors can only be changed while it is a draft.",
        )

    # De-duplicate by vendor pk, preserving first-seen order, validating each vendor.
    wanted: dict[int, object] = {}
    for vendor in vendors:
        if vendor.entity_id != rfq.entity_id:
            raise SourcingError(f"Vendor {vendor.code} belongs to a different entity.")
        if reason := vendor_purchase_block_reason(vendor):
            raise SourcingError(reason)
        wanted.setdefault(vendor.pk, vendor)
    wanted_ids = set(wanted)

    existing = {inv.vendor_id: inv for inv in rfq.invitations.select_related("vendor")}
    # "Responded" is derived: any quotation from that vendor on this RFQ.
    responded_ids = set(
        VendorQuotation.objects.filter(rfq=rfq).values_list("vendor_id", flat=True)
    )
    stranded = (responded_ids & set(existing)) - wanted_ids
    if stranded:
        codes = ", ".join(sorted(existing[vid].vendor.code for vid in stranded))
        raise SourcingError(
            f"Cannot remove vendor(s) {codes} — they have already responded to this RFQ.",
        )

    to_remove = set(existing) - wanted_ids
    if to_remove:
        rfq.invitations.filter(vendor_id__in=to_remove).delete()
    for vid, vendor in wanted.items():
        if vid not in existing:
            RfqInvitation.objects.create(rfq=rfq, vendor=vendor)
    return rfq


# --------------------------------------------------------------------------- #
# RFQ lifecycle                                                                #
# --------------------------------------------------------------------------- #

def issue_rfq(rfq, *, actor_user=None):
    """Move a DRAFT RFQ to ISSUED so vendors can quote against it.

    Requires at least one line **and** at least one invited vendor — an RFQ is a request
    for quotation *sent to vendors*, so issuing one with no addressees is meaningless.
    """
    if rfq.rfq_status != RfqStatus.DRAFT:
        raise SourcingError(
            f"RFQ {rfq.document_number or rfq.pk} is '{rfq.rfq_status}'; "
            f"only a draft RFQ can be issued.",
        )
    if not rfq.lines.exists():
        raise SourcingError("An RFQ needs at least one line before it can be issued.")
    if not rfq.invitations.exists():
        raise SourcingError("An RFQ must invite at least one vendor before it can be issued.")
    rfq.rfq_status = RfqStatus.ISSUED
    rfq.save(update_fields=["rfq_status", "updated_at"])
    record(
        entity=rfq.entity, action=FinanceAuditAction.RFQ_ISSUED,
        actor_user=actor_user, target=rfq,
        message=f"Issued RFQ {rfq.document_number} ({rfq.lines.count()} line(s)).",
    )
    return rfq


def _reject_live_quotations(rfq, *, actor_user=None):
    """Flip an RFQ's still-in-contention (DRAFT/SUBMITTED) quotations to REJECTED.

    Called when sourcing ends without those quotes winning (close/cancel), so no bid is
    left dangling in an active state on a finished RFQ. Each rejection is audited on the
    quotation so it shows in that quote's own activity feed.
    """
    live = list(rfq.quotations.filter(
        quotation_status__in=(QuotationStatus.DRAFT, QuotationStatus.SUBMITTED),
    ))
    for quotation in live:
        quotation.quotation_status = QuotationStatus.REJECTED
        quotation.save(update_fields=["quotation_status", "updated_at"])
        record(
            entity=quotation.entity, action=FinanceAuditAction.QUOTATION_REJECTED,
            actor_user=actor_user, target=quotation,
            message=f"Quotation {quotation.document_number} from {quotation.vendor.code} "
                    f"rejected (RFQ {rfq.document_number} closed without award).",
            rfq_id=rfq.pk,
        )
    return len(live)


@transaction.atomic
def cancel_rfq(rfq, *, reason="", actor_user=None):
    """Abandon an RFQ. Idempotent on terminal states (AWARDED/CLOSED/CANCELLED)."""
    from .models import RequestForQuotation

    # Lock and re-read the authoritative status so a concurrent award/close/cancel can't
    # race this decision (and so a stale in-memory status can't drive the transition).
    rfq = RequestForQuotation.objects.select_for_update(of=("self",)).get(pk=rfq.pk)
    if rfq.rfq_status in (RfqStatus.AWARDED, RfqStatus.CLOSED, RfqStatus.CANCELLED):
        if rfq.rfq_status == RfqStatus.AWARDED:
            raise SourcingError("An awarded RFQ cannot be cancelled.")
        return rfq
    # Abandoning the RFQ abandons every live bid on it — reject them so none stays open.
    _reject_live_quotations(rfq, actor_user=actor_user)
    rfq.rfq_status = RfqStatus.CANCELLED
    rfq.save(update_fields=["rfq_status", "updated_at"])
    record(
        entity=rfq.entity, action=FinanceAuditAction.RFQ_CANCELLED,
        actor_user=actor_user, target=rfq,
        message=f"Cancelled RFQ {rfq.document_number}."
                + (f" Reason: {reason}" if reason else ""),
    )
    return rfq


@transaction.atomic
def close_rfq(rfq, *, reason="", actor_user=None):
    """Finish an ISSUED RFQ without awarding it; rejects its live quotations.

    The deliberate "we sourced but chose no one" outcome (distinct from CANCELLED, which
    abandons before/around issue). Only an ISSUED RFQ can be closed — a draft was never
    open, and AWARDED/CLOSED/CANCELLED are terminal.
    """
    from .models import RequestForQuotation

    rfq = RequestForQuotation.objects.select_for_update(of=("self",)).get(pk=rfq.pk)
    if rfq.rfq_status != RfqStatus.ISSUED:
        raise SourcingError(
            f"RFQ {rfq.document_number or rfq.pk} is '{rfq.rfq_status}'; "
            f"only an issued RFQ can be closed without award.",
        )
    _reject_live_quotations(rfq, actor_user=actor_user)
    rfq.rfq_status = RfqStatus.CLOSED
    rfq.save(update_fields=["rfq_status", "updated_at"])
    record(
        entity=rfq.entity, action=FinanceAuditAction.RFQ_CLOSED,
        actor_user=actor_user, target=rfq,
        message=f"Closed RFQ {rfq.document_number} without award."
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
    # Invited-only: a quote may only be submitted by a vendor still invited on its RFQ.
    # Defensive — the create path already enforces this, but an invitation could have
    # been withdrawn while the draft sat around.
    from .models import RfqInvitation

    if not RfqInvitation.objects.filter(rfq=quotation.rfq, vendor=quotation.vendor).exists():
        raise SourcingError(
            f"Vendor {quotation.vendor.code} is not invited to RFQ "
            f"{quotation.rfq.document_number or quotation.rfq_id}.",
        )
    # Governance gate at submission too (not just award): a vendor that went on hold /
    # inactive / KYC-rejected after drafting cannot firm up a competing offer.
    if reason := vendor_purchase_block_reason(quotation.vendor):
        raise SourcingError(reason)

    price_quotation(quotation)
    quotation.quotation_status = QuotationStatus.SUBMITTED
    quotation.save(update_fields=["quotation_status", "updated_at"])
    record(
        entity=quotation.entity, action=FinanceAuditAction.QUOTATION_SUBMITTED,
        actor_user=actor_user, target=quotation,
        message=f"Quotation {quotation.document_number} from {quotation.vendor.code} "
                f"submitted ({format_naira(quotation.total)}).",
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
    from .models import (
        PurchaseOrder, PurchaseOrderLine, RequestForQuotation, Vendor, VendorQuotation,
    )

    # Lock the quotation and its RFQ up front so two concurrent awards on the same RFQ
    # serialise: the second waits here, then re-reads the RFQ as AWARDED and is rejected
    # below — closing the double-award race that a lock-free read/modify/write allowed.
    quotation = (
        VendorQuotation.objects.select_for_update(of=("self",))
        .select_related("vendor", "currency", "branch")
        .get(pk=quotation.pk)
    )
    rfq = RequestForQuotation.objects.select_for_update(of=("self",)).select_related(
        "requisition").get(pk=quotation.rfq_id)

    if quotation.quotation_status != QuotationStatus.SUBMITTED:
        raise SourcingError(
            f"Quotation {quotation.document_number or quotation.pk} is "
            f"'{quotation.quotation_status}'; only a submitted quotation can be awarded.",
        )
    if rfq.rfq_status != RfqStatus.ISSUED:
        raise SourcingError(
            f"RFQ {rfq.document_number} is '{rfq.rfq_status}'; only an issued RFQ "
            f"can be awarded.",
        )
    if not quotation.lines.exists():
        raise SourcingError("Cannot award a quotation with no lines.")
    # A lapsed offer is no longer a firm price — reject the award rather than commit to it.
    if quotation.valid_until is not None and quotation.valid_until < datetime.date.today():
        raise SourcingError(
            f"Quotation {quotation.document_number} validity lapsed on "
            f"{quotation.valid_until:%Y-%m-%d}; it cannot be awarded.",
        )

    if quotation.vendor.entity_id != quotation.entity_id:
        raise SourcingError("The quotation vendor must belong to the same entity.")
    # Prevent a simultaneous vendor hold/KYC edit from racing the award commitment.
    vendor = Vendor.objects.select_for_update(of=("self",)).get(pk=quotation.vendor_id)
    if reason := vendor_purchase_block_reason(vendor):
        raise SourcingError(reason)
    default_expense = (
        vendor.default_expense_account
        # Inactive taxonomy remains visible historically but must not seed new commitments.
        or (vendor.category.default_expense_account
            if vendor.category_id and vendor.category.is_active else None)
    )

    po = PurchaseOrder.objects.create(
        entity=quotation.entity, branch=quotation.branch,
        vendor=vendor, requisition=rfq.requisition,
        order_date=order_date or datetime.date.today(),
        currency=quotation.currency, created_by=actor_user,
        # Awarded POs inherit the vendor's configured terms when no buyer form is involved.
        payment_terms=vendor.payment_terms,
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
                f"PO {po.document_number} ({format_naira(po.total)}).",
        rfq_id=rfq.pk, purchase_order_id=po.pk, total=po.total,
    )
    return po
