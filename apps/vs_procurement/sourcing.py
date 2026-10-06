"""Sourcing services - competitive quotation before commitment.

The pre-PO funnel: a buyer issues a :class:`~vs_procurement.models.RequestForQuotation`
(optionally off an approved requisition), vendors reply with
:class:`~vs_procurement.models.VendorQuotation` s, and the winning quote is **awarded** -
which converts it into a DRAFT :class:`~vs_procurement.models.PurchaseOrder` ready to be
issued and received against. None of this touches the General Ledger; the first GL event
is still the goods receipt on the resulting PO. All money is integer kobo.
"""
from __future__ import annotations


from django.db import transaction

from vs_finance.audit import record
from vs_finance.constants import FinanceAuditAction
from vs_finance.money import format_naira
from vs_finance.receivables import compute_line_net, compute_tax
from vs_config.clock import branch_today
from vs_config.display import format_date

from .constants import QuotationStatus, RfqStatus
from .exceptions import SourcingError
from .purchasing import price_po, refuse_sourced_lines, vendor_purchase_block_reason
from vs_finance.wording import agrees, counted, state_word


# --------------------------------------------------------------------------- #
# RFQ invitations (the RFQ's addressee list)                                   #
# --------------------------------------------------------------------------- #

def set_rfq_invitations(rfq, vendors, *, actor_user=None):
    """Replace the invited-vendor set on a **DRAFT** RFQ.

    ``vendors`` is an iterable of :class:`Vendor` objects. Each must belong to the RFQ's
    entity and pass :func:`vendor_purchase_block_reason`; the list is de-duplicated. A
    vendor that has **already responded** (a quotation exists from it on this RFQ) may
    not be dropped - removing its invitation would strand that bid's history, so the call
    is rejected with a clear error rather than silently deleting it.

    Only a draft RFQ's addressee list is editable; once issued the invitations are the
    firm list of vendors the RFQ was sent to.
    """
    from .models import RfqInvitation, VendorQuotation

    if rfq.rfq_status != RfqStatus.DRAFT:
        raise SourcingError(
            f"RFQ {rfq.document_number or rfq.pk} is {state_word(rfq, 'rfq_status')}; "
            f"invited vendors can only be changed while it is a draft.",
        )

    from .settings import resolve_procurement_settings
    policy = resolve_procurement_settings(rfq.entity)
    # De-duplicate by vendor pk, preserving first-seen order, validating each vendor.
    wanted: dict[int, object] = {}
    for vendor in vendors:
        if vendor.entity_id != rfq.entity_id:
            raise SourcingError(f"Vendor {vendor.code} belongs to a different entity.")
        if reason := vendor_purchase_block_reason(vendor, policy=policy):
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
            f"Cannot remove {agrees(len(stranded), 'vendor', 'vendors')} {codes} - "
            f"{agrees(len(stranded), 'it has', 'they have')} already responded to this RFQ.",
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

@transaction.atomic
def issue_rfq(rfq, *, competition_exception_reason="", actor_user=None):
    """Move a DRAFT RFQ to ISSUED so vendors can quote against it.

    Requires at least one line **and** at least one invited vendor - an RFQ is a request
    for quotation *sent to vendors*, so issuing one with no addressees is meaningless.
    The RFQ row is re-read under lock so draft edits and issue cannot cross in flight.
    """
    from .models import RequestForQuotation

    supplied_rfq = rfq
    rfq = RequestForQuotation.objects.select_for_update(of=("self",)).get(pk=rfq.pk)
    if rfq.rfq_status != RfqStatus.DRAFT:
        raise SourcingError(
            f"RFQ {rfq.document_number or rfq.pk} is {state_word(rfq, 'rfq_status')}; "
            f"only a draft RFQ can be issued.",
        )
    line_count = rfq.lines.count()
    if not line_count:
        raise SourcingError("An RFQ needs at least one line before it can be issued.")
    from .settings import resolve_procurement_settings
    policy = resolve_procurement_settings(rfq.entity)
    invited_count = rfq.invitations.count()
    required_count = policy.minimum_rfq_invited_vendors
    exception_reason = str(competition_exception_reason or "").strip()
    # One addressee is a hard floor, not a policy minimum: an RFQ with nobody to
    # send it to cannot be answered, so no override reason makes it meaningful.
    # The competition exception only relaxes the *configured* minimum down to one.
    if not invited_count:
        raise SourcingError(
            "An RFQ must invite at least one vendor before it can be issued.",
        )
    competition_exception = invited_count < required_count
    if competition_exception and not exception_reason:
        raise SourcingError(
            f"This RFQ has {counted(invited_count, 'invited vendor')}, but policy requires "
            f"at least {required_count}. A user with the competition override permission "
            f"must provide an exception reason before it can be issued.",
        )
    rfq.rfq_status = RfqStatus.ISSUED
    rfq.save(update_fields=["rfq_status", "updated_at"])
    record(
        entity=rfq.entity, action=FinanceAuditAction.RFQ_ISSUED,
        actor_user=actor_user, target=rfq,
        message=f"Issued RFQ {rfq.document_number} ({counted(line_count, 'line')}).",
        invited_vendor_count=invited_count,
        minimum_invited_vendors=required_count,
        competition_exception=competition_exception,
        competition_exception_reason=exception_reason if competition_exception else "",
    )
    # Preserve the historical caller contract: mutate and return the object supplied.
    supplied_rfq.rfq_status = rfq.rfq_status
    supplied_rfq.document_number = rfq.document_number
    supplied_rfq.updated_at = rfq.updated_at
    return supplied_rfq


def _reject_live_quotations(rfq, *, actor_user=None):
    """Flip an RFQ's still-in-contention (DRAFT/SUBMITTED) quotations to REJECTED.

    Called when sourcing ends without those quotes winning (close/cancel), so no bid is
    left dangling in an active state on a finished RFQ. Each rejection is audited on the
    quotation so it shows in that quote's own activity feed.
    """
    # The caller already holds the RFQ lock. Lock live bids in id order before
    # transitioning them so every RFQ-changing action follows RFQ → quotation.
    live = list(rfq.quotations.select_for_update(of=("self",)).select_related("vendor").filter(
        quotation_status__in=(QuotationStatus.DRAFT, QuotationStatus.SUBMITTED),
    ).order_by("id"))
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


def supersede_vendor_portal_draft(rfq, vendor, *, actor_user=None):
    """Retire an abandoned portal workspace when a buyer captures the quote by hand.

    A ``vendor_managed`` DRAFT that has never been submitted is a workspace, not an
    offer, and it is deliberately hidden from the buyer's quotation lists. Left in
    place it blocked manual capture with a document the buyer could not open, see, or
    clear. The manual entry is the authoritative record, so the workspace is rejected
    rather than deleted - whatever the vendor typed stays auditable, and the portal
    turns read-only for them instead of running a second competing draft.

    A vendor-managed draft that *does* have submissions is a real bid part-way through
    a revision, so it is left untouched and both documents stand as separate bids.
    """
    # The caller already holds the RFQ lock, preserving the RFQ -> quotation order.
    drafts = rfq.quotations.select_for_update(of=("self",)).select_related("vendor").filter(
        vendor=vendor, vendor_managed=True, quotation_status=QuotationStatus.DRAFT,
    ).order_by("id")
    superseded = 0
    for quotation in drafts:
        if quotation.submissions.exists():
            continue
        quotation.quotation_status = QuotationStatus.REJECTED
        quotation.save(update_fields=["quotation_status", "updated_at"])
        record(
            entity=quotation.entity, action=FinanceAuditAction.QUOTATION_REJECTED,
            actor_user=actor_user, target=quotation,
            message=f"Unsubmitted vendor-portal draft {quotation.document_number} from "
                    f"{quotation.vendor.code} superseded by a quotation captured in the "
                    f"console (RFQ {rfq.document_number}).",
            rfq_id=rfq.pk,
        )
        superseded += 1
    return superseded


def release_shared_lines(rfq):
    """End a shared RFQ's hold on its requisition lines, now that the RFQ has ended.

    Called by :func:`cancel_rfq` and :func:`close_rfq` under the RFQ lock, in the
    transaction that ends the RFQ, so the lines are free the moment the RFQ is
    cancelled or closed and never while it is still open. Called by
    :func:`award_quotation` too, once the branch orders exist: those orders hold
    the lines from then on, so cancelling one frees its branch's lines. The
    allocations stay as the record of which branches the RFQ covered; only
    ``released_at`` is set. An ordinary RFQ has no allocations and releases
    nothing. Returns how many requisition lines were released.

        Mrs Bello cancels the shared RFQ for Ikeja's 60 chairs and Lekki's 40 to
        fix a line. Both lines are offered again and can go on a new shared RFQ,
        on either branch's own RFQ, or straight onto a purchase order.
    """
    from django.utils import timezone

    from .models import SharedSourcingAllocation

    return SharedSourcingAllocation.objects.filter(
        group__rfq=rfq, released_at__isnull=True,
    ).update(released_at=timezone.now())


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
    # Abandoning the RFQ abandons every live bid on it - reject them so none stays open.
    _reject_live_quotations(rfq, actor_user=actor_user)
    rfq.rfq_status = RfqStatus.CANCELLED
    rfq.save(update_fields=["rfq_status", "updated_at"])
    released = release_shared_lines(rfq)
    record(
        entity=rfq.entity, action=FinanceAuditAction.RFQ_CANCELLED,
        actor_user=actor_user, target=rfq,
        message=f"Cancelled RFQ {rfq.document_number}."
                + (f" Reason: {reason}" if reason else ""),
        **({"released_requisition_lines": released} if released else {}),
    )
    return rfq


@transaction.atomic
def close_rfq(rfq, *, reason="", actor_user=None):
    """Finish an ISSUED RFQ without awarding it; rejects its live quotations.

    The deliberate "we sourced but chose no one" outcome (distinct from CANCELLED, which
    abandons before/around issue). Only an ISSUED RFQ can be closed - a draft was never
    open, and AWARDED/CLOSED/CANCELLED are terminal.
    """
    from .models import RequestForQuotation

    rfq = RequestForQuotation.objects.select_for_update(of=("self",)).get(pk=rfq.pk)
    if rfq.rfq_status != RfqStatus.ISSUED:
        raise SourcingError(
            f"RFQ {rfq.document_number or rfq.pk} is {state_word(rfq, 'rfq_status')}; "
            f"only an issued RFQ can be closed without award.",
        )
    _reject_live_quotations(rfq, actor_user=actor_user)
    rfq.rfq_status = RfqStatus.CLOSED
    rfq.save(update_fields=["rfq_status", "updated_at"])
    released = release_shared_lines(rfq)
    record(
        entity=rfq.entity, action=FinanceAuditAction.RFQ_CLOSED,
        actor_user=actor_user, target=rfq,
        message=f"Closed RFQ {rfq.document_number} without award."
                + (f" Reason: {reason}" if reason else ""),
        **({"released_requisition_lines": released} if released else {}),
    )
    return rfq


# --------------------------------------------------------------------------- #
# Quotation lifecycle                                                          #
# --------------------------------------------------------------------------- #

def price_quotation(quotation) -> None:
    """Reprice quotation lines and roll exact integer-kobo totals to the header.

    Shared finance helpers apply the same basis-point rounding later used by PO and bill
    pricing, preventing an awarded quote from changing value merely through conversion.
    """
    from .models import VendorQuotationLine
    from .constants import QuotationLineResponse

    for line in quotation.lines.all():
        net = 0 if line.response_type == QuotationLineResponse.NO_BID else compute_line_net(
            line.quantity, line.unit_price,
        )
        rate = line.tax_code.rate_bps if line.tax_code_id else 0
        tax = compute_tax(net, rate)
        if line.net_amount != net or line.tax_amount != tax:
            VendorQuotationLine.objects.filter(pk=line.pk).update(
                net_amount=net, tax_amount=tax,
            )
    quotation.recompute_totals(save=True)


@transaction.atomic
def submit_quotation(quotation, *, actor_user=None):
    """Lock RFQ → quotation → vendor, then record one authoritative submission."""
    from .models import RequestForQuotation, RfqInvitation, Vendor, VendorQuotation

    supplied_quotation = quotation
    # RFQ-first matches close/cancel/award. Quotation-second serializes against draft
    # PATCH, and vendor-last rechecks governance after any concurrent master-data edit.
    rfq = RequestForQuotation.objects.select_for_update(of=("self",)).get(
        pk=quotation.rfq_id,
    )
    quotation = VendorQuotation.objects.select_for_update(of=("self",)).get(
        pk=quotation.pk,
    )
    vendor = Vendor.objects.select_for_update(of=("self",)).get(pk=quotation.vendor_id)
    quotation.rfq = rfq
    quotation.vendor = vendor

    if quotation.quotation_status != QuotationStatus.DRAFT:
        raise SourcingError(
            f"Quotation {quotation.document_number or quotation.pk} is "
            f"{state_word(quotation, 'quotation_status')}; only a draft quotation can be submitted.",
        )
    if rfq.rfq_status != RfqStatus.ISSUED:
        raise SourcingError(
            f"RFQ {rfq.document_number} is {state_word(rfq, 'rfq_status')}; "
            f"quotations can only be submitted while it is ISSUED.",
        )
    if not quotation.lines.exists():
        raise SourcingError("A quotation needs at least one priced line.")
    # Invited-only: a quote may only be submitted by a vendor still invited on its RFQ.
    # Defensive - the create path already enforces this, but an invitation could have
    # been withdrawn while the draft sat around.
    if not RfqInvitation.objects.filter(rfq=rfq, vendor=vendor).exists():
        raise SourcingError(
            f"Vendor {vendor.code} is not invited to RFQ "
            f"{rfq.document_number or rfq.pk}.",
        )
    # Governance gate at submission too (not just award): a vendor that went on hold /
    # inactive / KYC-rejected after drafting cannot firm up a competing offer.
    if reason := vendor_purchase_block_reason(vendor):
        raise SourcingError(reason)

    price_quotation(quotation)
    quotation.quotation_status = QuotationStatus.SUBMITTED
    quotation.save(update_fields=["quotation_status", "updated_at"])
    record(
        entity=quotation.entity, action=FinanceAuditAction.QUOTATION_SUBMITTED,
        actor_user=actor_user, target=quotation,
        message=f"Quotation {quotation.document_number} from {vendor.code} "
                f"submitted ({format_naira(quotation.total)}).",
        rfq_id=quotation.rfq_id, total=quotation.total,
    )
    # Preserve mutation/identity for direct service callers while all decisions use
    # the locked authoritative row.
    supplied_quotation.quotation_status = quotation.quotation_status
    supplied_quotation.subtotal = quotation.subtotal
    supplied_quotation.tax_total = quotation.tax_total
    supplied_quotation.total = quotation.total
    supplied_quotation.updated_at = quotation.updated_at
    return supplied_quotation


@transaction.atomic
def award_quotation(
    quotation, *, order_date=None, competition_exception_reason="", actor_user=None,
):
    """Award a SUBMITTED quotation: build a DRAFT PO from it and reject the losers.

    Sets the quotation AWARDED, links the new :class:`PurchaseOrder` it produced, marks
    the RFQ AWARDED, and flips every other still-in-contention quotation on the same RFQ
    to REJECTED. The PO carries each quoted line's price and expense account (falling back
    to the vendor's / category's default). Quotation, RFQ, vendor eligibility, PO creation,
    loser rejection, and audit all commit or roll back together. A shared RFQ returns
    one PO per participating branch; an ordinary RFQ returns its single PO.

    The order is this RFQ's sourcing carrying on, so the RFQ's own lines never
    refuse it; a requisition line some other live RFQ or order holds does
    (:func:`vs_procurement.purchasing.refuse_sourced_lines`). That catches two RFQs
    raised for the same line before one live sourcing per line was enforced: the
    first award orders the chairs, the second is refused. From the award on, the
    orders hold the requisition lines, and a shared RFQ's allocations are released.
    """
    from .models import (
        PurchaseOrder, PurchaseOrderLine, RequestForQuotation, SharedSourcingGroup,
        SharedSourcingOrder, Vendor, VendorQuotation,
    )

    # RFQ → quotation → vendor is the shared sourcing lock order. Two awards on the
    # same RFQ serialize at the parent row; the second then observes the committed
    # AWARDED/rejected state, preserving double-award protection without deadlocking
    # RFQ-first close/cancel/submit transitions.
    rfq = RequestForQuotation.objects.select_for_update(of=("self",)).select_related(
        "requisition",
    ).get(pk=quotation.rfq_id)
    quotation = (
        VendorQuotation.objects.select_for_update(of=("self",))
        .select_related("vendor", "currency", "branch")
        .get(pk=quotation.pk)
    )
    group = SharedSourcingGroup.objects.select_for_update().filter(rfq=rfq).first()

    if quotation.quotation_status != QuotationStatus.SUBMITTED:
        raise SourcingError(
            f"Quotation {quotation.document_number or quotation.pk} is "
            f"{state_word(quotation, 'quotation_status')}; only a submitted quotation can be awarded.",
        )
    if rfq.rfq_status != RfqStatus.ISSUED:
        raise SourcingError(
            f"RFQ {rfq.document_number} is {state_word(rfq, 'rfq_status')}; only an issued RFQ "
            f"can be awarded.",
        )
    from vs_rbac.scoping import only_branch_id, same_transaction_branch
    award_branch_id = rfq.branch_id or quotation.branch_id or only_branch_id(
        quotation.entity.tenant_id,
    )
    if award_branch_id is None:
        raise SourcingError(
            "Place this quotation and its RFQ in a branch before awarding a purchase order."
        )
    if not same_transaction_branch(
        quotation.entity.tenant_id, rfq.branch_id, quotation.branch_id, award_branch_id,
    ):
        raise SourcingError("The quotation and RFQ belong to different branches.")
    from .settings import resolve_procurement_settings
    policy = resolve_procurement_settings(quotation.entity)
    submitted_count = rfq.quotations.filter(
        quotation_status=QuotationStatus.SUBMITTED,
    ).count()
    required_count = policy.minimum_submitted_quotations_before_award
    exception_reason = str(competition_exception_reason or "").strip()
    competition_exception = submitted_count < required_count
    if competition_exception and not exception_reason:
        raise SourcingError(
            f"This RFQ has {counted(submitted_count, 'submitted quotation')}, but policy requires "
            f"at least {required_count}. A user with the competition override permission "
            f"must provide an exception reason before a quotation can be awarded.",
        )
    if not quotation.lines.exists():
        raise SourcingError("Cannot award a quotation with no lines.")
    # A lapsed offer is no longer a firm price - reject the award rather than commit to it.
    if (
        quotation.valid_until is not None
        and quotation.valid_until < branch_today(quotation.entity.tenant, award_branch_id)
    ):
        raise SourcingError(
            f"Quotation {quotation.document_number} validity lapsed on "
            f"{format_date(quotation.valid_until, quotation.entity.tenant)}; "
            f"it cannot be awarded.",
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

    # Load the complete source-line chain once: cost-centre ownership follows the
    # originating requisition line when present, then the RFQ header requisition.
    # Assigning the FK id avoids a separate CostCenter lookup for every awarded line.
    header_cost_center_id = rfq.requisition.cost_center_id if rfq.requisition_id else None
    quotation_lines = list(quotation.lines.exclude(
        response_type="NO_BID",
    ).select_related(
        "expense_account", "tax_code", "rfq_line__requisition_line__requisition",
    ).order_by("line_no", "id"))
    if group is None:
        sources = [
            qline.rfq_line.requisition_line_id for qline in quotation_lines
            if qline.rfq_line_id and qline.rfq_line.requisition_line_id
        ]
    else:
        sources = list(group.allocations.values_list("requisition_line_id", flat=True))
    refuse_sourced_lines(sources, rfq=rfq, error=SourcingError)
    if group is None:
        for qline in quotation_lines:
            source = qline.rfq_line.requisition_line if qline.rfq_line_id else None
            if source is not None and not same_transaction_branch(
                rfq.entity.tenant_id, award_branch_id, source.requisition.branch_id,
            ):
                raise SourcingError(
                    "An RFQ source line belongs to another branch; correct it before award."
                )
        po = PurchaseOrder.objects.create(
            entity=quotation.entity, branch_id=award_branch_id,
            vendor=vendor, requisition=rfq.requisition,
            order_date=order_date or branch_today(quotation.entity.tenant, award_branch_id),
            currency=quotation.currency, created_by=actor_user,
            payment_terms=vendor.payment_terms,
            reference=quotation.reference,
            narration=f"From quotation {quotation.document_number} (RFQ {rfq.document_number}).",
        )
        for qline in quotation_lines:
            expense = qline.expense_account or default_expense
            if expense is None:
                raise SourcingError(
                    f"Quotation line '{qline.description}' has no expense account and the "
                    f"vendor has no default - set one before awarding.",
                )
            requisition_line = (
                qline.rfq_line.requisition_line
                if qline.rfq_line_id and qline.rfq_line.requisition_line_id else None
            )
            cost_center_id = (
                requisition_line.requisition.cost_center_id
                if requisition_line is not None else header_cost_center_id
            )
            PurchaseOrderLine.objects.create(
                purchase_order=po,
                requisition_line=requisition_line,
                description=qline.description, expense_account=expense,
                quantity=qline.quantity, unit_price=qline.unit_price,
                tax_code=qline.tax_code, cost_center_id=cost_center_id, line_no=qline.line_no,
            )
        price_po(po)
        purchase_orders = [po]
    else:
        from vs_rbac.scoping import caller_may_change
        allocations = list(group.allocations.select_related(
            "rfq_line", "requisition_line__requisition",
        ).order_by("requisition_line__requisition__branch_id", "rfq_line__line_no", "id"))
        participant_ids = {
            allocation.requisition_line.requisition.branch_id for allocation in allocations
        }
        if None in participant_ids or len(participant_ids) < 2:
            raise SourcingError("Every shared sourcing participant must belong to a branch.")
        if actor_user is None or not caller_may_change(
            actor_user, quotation.entity.tenant, participant_ids,
        ):
            raise SourcingError(
                "The awarding buyer must have access to every participating branch."
            )
        allocations_by_line = {}
        for allocation in allocations:
            allocations_by_line.setdefault(allocation.rfq_line_id, []).append(allocation)
        lines_by_branch = {branch_id: [] for branch_id in participant_ids}
        for qline in quotation_lines:
            line_allocations = allocations_by_line.get(qline.rfq_line_id, [])
            if not line_allocations:
                raise SourcingError(
                    "Every awarded quotation line must match an allocated RFQ line."
                )
            if sum((allocation.quantity for allocation in line_allocations), 0) != qline.quantity:
                raise SourcingError(
                    f"Quotation line '{qline.description}' must price the full allocated quantity."
                )
            expense = qline.expense_account or default_expense
            if expense is None:
                raise SourcingError(
                    f"Quotation line '{qline.description}' has no expense account and the "
                    f"vendor has no default - set one before awarding.",
                )
            for allocation in line_allocations:
                source = allocation.requisition_line
                lines_by_branch[source.requisition.branch_id].append((qline, source, allocation.quantity, expense))
        purchase_orders = []
        for branch_id in sorted(lines_by_branch):
            branch_lines = lines_by_branch[branch_id]
            requisition_ids = {source.requisition_id for _, source, _, _ in branch_lines}
            po = PurchaseOrder.objects.create(
                entity=quotation.entity, branch_id=branch_id, vendor=vendor,
                requisition_id=next(iter(requisition_ids)) if len(requisition_ids) == 1 else None,
                order_date=order_date or branch_today(quotation.entity.tenant, branch_id),
                currency=quotation.currency, created_by=actor_user,
                payment_terms=vendor.payment_terms, reference=quotation.reference,
                narration=f"From quotation {quotation.document_number} (RFQ {rfq.document_number}).",
            )
            for line_no, (qline, source, quantity, expense) in enumerate(branch_lines, start=1):
                PurchaseOrderLine.objects.create(
                    purchase_order=po, requisition_line=source,
                    description=qline.description, expense_account=expense,
                    quantity=quantity, unit_price=qline.unit_price,
                    tax_code=qline.tax_code, cost_center_id=source.requisition.cost_center_id,
                    line_no=line_no,
                )
            price_po(po)
            SharedSourcingOrder.objects.create(
                group=group, branch_id=branch_id, purchase_order=po,
            )
            purchase_orders.append(po)
        group.awarded_quotation = quotation
        group.save(update_fields=["awarded_quotation", "updated_at"])
        release_shared_lines(rfq)
        po = purchase_orders[0]

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
        message=(
            f"Awarded quotation {quotation.document_number} from {vendor.code} to "
            f"{counted(len(purchase_orders), 'purchase order')} ({format_naira(sum(p.total for p in purchase_orders))})."
        ),
        rfq_id=rfq.pk, purchase_order_id=po.pk,
        purchase_order_ids=[row.pk for row in purchase_orders],
        total=sum(row.total for row in purchase_orders),
        submitted_quotation_count=submitted_count,
        minimum_submitted_quotations=required_count,
        competition_exception=competition_exception,
        competition_exception_reason=exception_reason if competition_exception else "",
    )
    return po if group is None else purchase_orders
