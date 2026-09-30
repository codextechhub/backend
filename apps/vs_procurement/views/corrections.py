"""Correcting posted payables: vendor credit notes, bill voids and goods returns.

Every endpoint reaches the document it corrects through ``_document_or_404``, so a
branch-bound caller can correct only what they could open, and a new document takes
its parent's branch through ``_inherited_branch_id``: a credit note is its bill's, a
goods return its receipt's. The services in :mod:`vs_procurement.corrections` own the
accounting and the audit rows; these views own request parsing and reach.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import ValidationError

from core.response import success_response
from vs_config.clock import branch_today
from vs_finance.constants import DocumentStatus
from vs_finance.money import format_naira
from vs_finance.views import resolve_entity

from .. import approvals, corrections
from ..constants import ProcApprovalState
from ..exceptions import VendorCreditNoteError
from ..models import GoodsReceivedNote, VendorCreditNote, VendorInvoice
from ..serializers import (
    GoodsReceivedNoteSerializer,
    GoodsReturnSerializer,
    VendorCreditNoteListSerializer,
    VendorCreditNoteSerializer,
    VendorInvoiceSerializer,
)
from .base import (
    _ProcBase,
    _raised_branch,
    _resolve_vendor,
    _branch_q,
    _branch_scoped,
    _branch_visible,
    _date,
    _document_or_404,
    _inherited_branch_id,
    _quantity,
    _strict_kobo,
    _text,
)

__all__ = [
    "GoodsReceiptReverseView",
    "VendorOpeningBillImportView",
    "VendorCreditNoteAllocateView",
    "VendorCreditNoteDetailView",
    "VendorCreditNoteListCreateView",
    "VendorCreditNotePostView",
    "VendorCreditNoteSubmitView",
    "VendorCreditNoteVoidView",
    "VendorInvoiceVoidView",
]


def _action_date(request, document_branch_id, entity, field="date"):
    """The date a correction takes effect: the one given, else today at the document's branch."""
    return (
        _date((request.data or {}).get(field), field)
        or branch_today(entity.tenant, document_branch_id)
    )


# --------------------------------------------------------------------------- #
# Vendor credit notes                                                         #
# --------------------------------------------------------------------------- #

def _note_queryset(entity):
    """Eager-loaded source for credit note detail."""
    return VendorCreditNote.objects.filter(entity=entity).select_related(
        "vendor", "vendor_invoice", "branch",
    ).prefetch_related("lines", "allocations__vendor_invoice")


def _credit_instruction(body):
    """Read what to credit: ``full``, ``amount`` (kobo) or ``lines``, exactly one of them.

    Returns keyword arguments for :func:`vs_procurement.corrections.write_credit_note_lines`,
    or ``None`` when the body names none of them (an edit that leaves the lines alone).
    """
    full = body.get("full", False)
    if not isinstance(full, bool):
        raise ValidationError({"full": "Expected a JSON boolean."})
    amount = body.get("amount")
    raw_lines = body.get("lines")
    given = [full, amount is not None, raw_lines is not None]
    if sum(given) == 0:
        return None
    if sum(given) > 1:
        raise ValidationError({
            "lines": "Credit the whole bill, an amount, or named lines: one of them, not several.",
        })
    if full:
        return {"full": True}
    if amount is not None:
        return {"amount": _strict_kobo(amount, "amount")}
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ValidationError({"lines": "Name at least one bill line to credit."})
    lines = []
    for item in raw_lines:
        if not isinstance(item, dict) or not str(item.get("invoice_line") or "").isdigit():
            raise ValidationError({"lines": "Each line needs the id of the bill line it credits."})
        quantity = item.get("quantity")
        net = item.get("net_amount")
        lines.append({
            "invoice_line": int(item["invoice_line"]),
            "quantity": _quantity(quantity, "quantity") if quantity not in (None, "", 0) else None,
            "net_amount": _strict_kobo(net, "net_amount") if net is not None else None,
        })
    return {"lines": lines}


def _write_lines(note, instruction):
    """Write the draft's lines, reporting a refusal as a field error on the request."""
    try:
        corrections.write_credit_note_lines(note, **instruction)
    except VendorCreditNoteError as exc:
        raise ValidationError({"lines": exc.message})


class VendorCreditNoteListCreateView(_ProcBase):
    """GET (list) / POST (draft a credit note against one posted bill).

    POST body: ``vendor_invoice`` (id), ``note_date``, ``reason``, optional
    ``vendor_reference``, and exactly one of ``full: true``, ``amount`` (gross kobo,
    spread across the bill as a price allowance) or ``lines``
    (``[{invoice_line, quantity?, net_amount?}]``). The note takes the bill's vendor,
    currency and branch.

    docstring-name: Vendor credit notes
    """

    @property
    def rbac_permission(self):
        """Require credit note creation for POST and view for GET."""
        return "procurement.vendor_credit_note.create" if self.request.method == "POST" \
            else "procurement.vendor_credit_note.view"

    def get(self, request):
        """List the credit notes the caller can reach, newest first."""
        entity = resolve_entity(request)
        qs = _branch_scoped(
            request, entity,
            VendorCreditNote.objects.filter(entity=entity).select_related(
                "vendor", "vendor_invoice", "branch",
            ),
            request.query_params,
        )
        params = request.query_params
        if (status_ := params.get("status")):
            qs = qs.filter(status=status_)
        if (vendor := params.get("vendor")):
            qs = qs.filter(vendor_id=vendor) if str(vendor).isdigit() else qs.filter(vendor__code=vendor)
        if (bill := params.get("vendor_invoice")) and str(bill).isdigit():
            qs = qs.filter(vendor_invoice_id=int(bill))
        return self.paginate(request, qs.order_by("-id"), VendorCreditNoteListSerializer)

    @transaction.atomic
    def post(self, request):
        """Draft the note and its lines; nothing posts until it is approved."""
        entity = resolve_entity(request)
        body = request.data or {}
        bill_id = body.get("vendor_invoice")
        bill = None
        if str(bill_id or "").isdigit():
            bill = _branch_visible(
                request, VendorInvoice.objects.filter(entity=entity),
            ).filter(pk=int(bill_id)).select_related("vendor").first()
        if bill is None:
            raise ValidationError({"vendor_invoice": "No such vendor invoice in this entity."})
        if bill.status != DocumentStatus.POSTED:
            raise ValidationError({"vendor_invoice": "Only a posted vendor invoice can be credited."})
        instruction = _credit_instruction(body)
        if instruction is None:
            raise ValidationError({"lines": "Say what to credit: full, amount or lines."})
        note = VendorCreditNote.objects.create(
            entity=entity, vendor=bill.vendor, vendor_invoice=bill,
            branch_id=_inherited_branch_id(request, bill),
            note_date=_date(body.get("note_date"), "note_date", required=True),
            currency=bill.currency,
            vendor_reference=_text(body.get("vendor_reference"), "vendor_reference", 64),
            reason=_text(body.get("reason"), "reason", 255, required=True),
            created_by=request.user if request.user.is_authenticated else None,
        )
        _write_lines(note, instruction)
        return success_response(
            "Vendor credit note created.",
            data=VendorCreditNoteSerializer(_note_queryset(entity).get(pk=note.pk)).data,
            status=201,
        )


class VendorCreditNoteDetailView(_ProcBase):
    """Read a credit note, or rewrite an unsubmitted or rejected draft."""

    @property
    def rbac_permission(self):
        """Separate draft correction from visibility."""
        return "procurement.vendor_credit_note.update" if self.request.method == "PATCH" \
            else "procurement.vendor_credit_note.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        note = _document_or_404(
            request, _note_queryset(entity), pk, "No such vendor credit note in this entity.",
        )
        return success_response(
            "Vendor credit note retrieved.", data=VendorCreditNoteSerializer(note).data,
        )

    @transaction.atomic
    def patch(self, request, pk):
        """Edit the header or rewrite the lines; the edit returns it to unsubmitted."""
        entity = resolve_entity(request)
        note = _document_or_404(
            request, VendorCreditNote.objects.select_for_update().filter(entity=entity),
            pk, "No such vendor credit note in this entity.",
        )
        if note.status != DocumentStatus.DRAFT or note.approval_state not in (
                ProcApprovalState.NOT_SUBMITTED, ProcApprovalState.REJECTED):
            raise ValidationError({
                "status": "Only an unsubmitted or rejected draft credit note can be edited.",
            })
        body = request.data or {}
        if "note_date" in body:
            note.note_date = _date(body.get("note_date"), "note_date", required=True)
        if "reason" in body:
            note.reason = _text(body.get("reason"), "reason", 255, required=True)
        if "vendor_reference" in body:
            note.vendor_reference = _text(body.get("vendor_reference"), "vendor_reference", 64)
        note.approval_state = ProcApprovalState.NOT_SUBMITTED
        note.save(update_fields=[
            "note_date", "reason", "vendor_reference", "approval_state", "updated_at",
        ])
        instruction = _credit_instruction(body)
        if instruction is not None:
            _write_lines(note, instruction)
        return success_response(
            "Vendor credit note draft updated.",
            data=VendorCreditNoteSerializer(_note_queryset(entity).get(pk=note.pk)).data,
        )


class VendorCreditNoteSubmitView(_ProcBase):
    """Submit a draft credit note for approval, through the same ladder rules as bills."""

    rbac_permission = "procurement.vendor_credit_note.submit"

    def post(self, request, pk):
        from .requisitions import _approval_response

        entity = resolve_entity(request)
        note = _document_or_404(
            request, VendorCreditNote.objects.filter(entity=entity), pk,
            "No such vendor credit note in this entity.",
        )
        if note.status != DocumentStatus.DRAFT:
            raise ValidationError({"status": "Only a draft credit note can be submitted for approval."})
        instance = approvals.submit_for_approval(
            note, actor_user=request.user,
            confirm_without_approval=bool((request.data or {}).get("confirm_without_approval")),
            confirmation_reason=str((request.data or {}).get("reason") or "").strip(),
        )
        return _approval_response(
            "Vendor credit note submitted for approval.", note, instance,
            VendorCreditNoteSerializer,
        )


class VendorCreditNotePostView(_ProcBase):
    """POST - post an approved credit note (Dr AP / vendor advances, Cr what the bill booked).

    docstring-name: Post a vendor credit note
    """

    rbac_permission = "procurement.vendor_credit_note.post"

    def post(self, request, pk):
        entity = resolve_entity(request)
        note = _document_or_404(
            request, VendorCreditNote.objects.filter(entity=entity), pk,
            "No such vendor credit note in this entity.",
        )
        corrections.post_vendor_credit_note(note, actor_user=request.user)
        note = _note_queryset(entity).get(pk=pk)
        return success_response(
            f"Vendor credit note {note.document_number} posted.",
            data=VendorCreditNoteSerializer(note).data,
        )


class VendorCreditNoteAllocateView(_ProcBase):
    """POST - apply a note's leftover vendor credit to later bills of its branch.

    Body ``{allocations:[{vendor_invoice, amount}]}`` for an explicit split, or
    ``{auto_allocate:true}`` for the vendor's open bills of the note's branch that the
    caller can reach, oldest due first. Bills outside the caller's reach are answered
    exactly like bills that do not exist.

    docstring-name: Apply vendor credit
    """

    rbac_permission = "procurement.vendor_credit_note.allocate"

    @transaction.atomic
    def post(self, request, pk):
        entity = resolve_entity(request)
        note = _document_or_404(
            request, VendorCreditNote.objects.filter(entity=entity), pk,
            "No such vendor credit note in this entity.",
        )
        if note.status != DocumentStatus.POSTED:
            raise ValidationError({"status": "Only a posted credit note holds credit to apply."})
        if note.advance_remaining <= 0:
            raise ValidationError({"allocations": "This credit note has no credit left to apply."})
        body = request.data or {}
        raw = body.get("allocations")
        before = note.advance_remaining
        if raw:
            if not isinstance(raw, list):
                raise ValidationError({"allocations": "Expected a list of bills and amounts."})
            ids = [item.get("vendor_invoice") if isinstance(item, dict) else None for item in raw]
            if any(not str(value or "").isdigit() for value in ids) or len(set(ids)) != len(ids):
                raise ValidationError({"allocations": "Name each vendor invoice once, by id."})
            bills = {
                bill.pk: bill for bill in _branch_visible(
                    request, VendorInvoice.objects.filter(
                        entity=entity, vendor_id=note.vendor_id, status=DocumentStatus.POSTED,
                    ),
                ).filter(pk__in=[int(value) for value in ids])
            }
            if len(bills) != len(ids):
                raise ValidationError({
                    "allocations": "Every invoice must be posted and belong to this vendor.",
                })
            plan = [
                (bills[int(item["vendor_invoice"])], _strict_kobo(item.get("amount"), "amount"))
                for item in raw
            ]
            corrections.allocate_vendor_credit_note(
                note, allocations=plan, actor_user=request.user,
            )
        elif body.get("auto_allocate") is True:
            corrections.allocate_vendor_credit_note(
                note, actor_user=request.user, bill_scope=_branch_q(request),
            )
        else:
            raise ValidationError({"allocations": "Provide allocations or auto_allocate=true."})
        note = _note_queryset(entity).get(pk=pk)
        applied = before - note.advance_remaining
        message = (
            f"Applied {format_naira(applied)} of {note.document_number} to open bills."
            if applied > 0 else f"No open bill could take {note.document_number}'s credit."
        )
        return success_response(message, data=VendorCreditNoteSerializer(note).data)


class VendorCreditNoteVoidView(_ProcBase):
    """POST - void a posted credit note, reversing it and every application of its credit.

    docstring-name: Void a vendor credit note
    """

    rbac_permission = "procurement.vendor_credit_note.reverse"

    def post(self, request, pk):
        entity = resolve_entity(request)
        note = _document_or_404(
            request, VendorCreditNote.objects.filter(entity=entity), pk,
            "No such vendor credit note in this entity.",
        )
        corrections.void_vendor_credit_note(
            note, actor_user=request.user, date=_action_date(request, note.branch_id, entity),
        )
        note = _note_queryset(entity).get(pk=pk)
        return success_response(
            f"Vendor credit note {note.document_number} voided.",
            data=VendorCreditNoteSerializer(note).data,
        )


# --------------------------------------------------------------------------- #
# Bill void                                                                   #
# --------------------------------------------------------------------------- #

class VendorInvoiceVoidView(_ProcBase):
    """POST - void a posted bill nothing has been paid or credited against.

    Optional body ``date`` dates the reversal; it defaults to today at the bill's
    branch. A paid or partly paid bill is refused with a 409 and corrected with a
    vendor credit note instead.

    docstring-name: Void a vendor invoice
    """

    rbac_permission = "procurement.vendor_invoice.reverse"

    def post(self, request, pk):
        entity = resolve_entity(request)
        bill = _document_or_404(
            request, VendorInvoice.objects.filter(entity=entity), pk,
            "No such vendor invoice in this entity.",
        )
        corrections.void_vendor_invoice(
            bill, actor_user=request.user, date=_action_date(request, bill.branch_id, entity),
        )
        bill.refresh_from_db()
        return success_response(
            f"Vendor invoice {bill.document_number} voided.",
            data=VendorInvoiceSerializer(bill).data,
        )


# --------------------------------------------------------------------------- #
# Goods receipt reversal (return to vendor)                                   #
# --------------------------------------------------------------------------- #

class GoodsReceiptReverseView(_ProcBase):
    """POST - send goods on a posted receipt back to the vendor.

    Body: ``reason`` (required), optional ``return_date`` (defaults to today at the
    receipt's branch) and optional ``lines`` (``[{grn_line, quantity}]``). Without
    lines, everything on the receipt that can still go back goes, which reverses a
    receipt entered in error. Goods already billed must be credited on the bill first.

    docstring-name: Reverse a goods receipt
    """

    rbac_permission = "procurement.goods_receipt.reverse"

    def post(self, request, pk):
        entity = resolve_entity(request)
        grn = _document_or_404(
            request, GoodsReceivedNote.objects.filter(entity=entity), pk,
            "No such goods receipt in this entity.",
        )
        body = request.data or {}
        raw = body.get("lines")
        lines = None
        if raw is not None:
            if not isinstance(raw, list) or not raw:
                raise ValidationError({"lines": "Name at least one receipt line to return."})
            lines = []
            for item in raw:
                if not isinstance(item, dict) or not str(item.get("grn_line") or "").isdigit():
                    raise ValidationError({"lines": "Each line needs the id of the receipt line."})
                lines.append((int(item["grn_line"]), _quantity(item.get("quantity"), "quantity")))
        goods_return = corrections.return_goods(
            grn, lines=lines, actor_user=request.user,
            reason=_text(body.get("reason"), "reason", 255, required=True),
            return_date=_action_date(request, grn.branch_id, entity, field="return_date"),
        )
        from .receiving import _read_grn_for_response

        return success_response(
            f"Goods on {grn.document_number} returned to the vendor "
            f"({goods_return.document_number}).",
            data={
                "goods_return": GoodsReturnSerializer(goods_return).data,
                "goods_receipt": GoodsReceivedNoteSerializer(
                    _read_grn_for_response(entity, grn.pk)).data,
            },
        )


# --------------------------------------------------------------------------- #
# Opening bills                                                               #
# --------------------------------------------------------------------------- #

#: Rows one import may carry; a larger file is split by the caller.
OPENING_IMPORT_LIMIT = 500


class VendorOpeningBillImportView(_ProcBase):
    """POST - carry in the supplier bills still unpaid when the books began.

    Body ``{"bills": [{vendor, invoice_date, due_date?, vendor_reference?, amount,
    branch?, narration?}]}``: one row per unpaid bill, dated when the supplier raised
    it, so it ages as the original did. ``amount`` is the kobo still owed. Each row's
    branch follows the rule for any bill raised without an order: the caller's own
    branch, or the one named when the caller covers several. The import is all or
    nothing, and a refused row is named by its position.

    Each bill posts ``Dr retained earnings, Cr AP`` with no match and no approval
    route; the dedicated key is the control. A bill dated on or after the day the
    books went live is refused, because it is ordinary business.

    docstring-name: Import opening supplier bills
    """

    rbac_permission = "procurement.vendor_invoice.import_opening"

    def post(self, request):
        from vs_finance.exceptions import FinanceError

        from ..payables import import_opening_vendor_invoices

        entity = resolve_entity(request)
        raw = (request.data or {}).get("bills")
        if not isinstance(raw, list) or not raw:
            raise ValidationError({"bills": "Send at least one opening bill."})
        if len(raw) > OPENING_IMPORT_LIMIT:
            raise ValidationError({
                "bills": f"Send at most {OPENING_IMPORT_LIMIT} bills in one import.",
            })
        rows = []
        seen_references = set()
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValidationError({"bills": {index: "Each bill is an object."}})
            try:
                invoice_date = _date(item.get("invoice_date"), "invoice_date", required=True)
                due_date = _date(item.get("due_date"), "due_date")
                if due_date is not None and due_date < invoice_date:
                    raise ValidationError({"due_date": "A bill cannot fall due before it was raised."})
                amount = _strict_kobo(item.get("amount"), "amount")
                if amount <= 0:
                    raise ValidationError({"amount": "The amount still owed must be positive."})
                vendor = _resolve_vendor(request, entity, item.get("vendor"))
                reference = _text(item.get("vendor_reference"), "vendor_reference", 64)
                if reference:
                    key = (vendor.pk, reference.lower())
                    if key in seen_references or VendorInvoice.objects.filter(
                            entity=entity, vendor=vendor, vendor_reference__iexact=reference,
                    ).exists():
                        raise ValidationError({
                            "vendor_reference": "This vendor invoice number is already recorded.",
                        })
                    seen_references.add(key)
                rows.append({
                    "vendor": vendor,
                    "branch": _raised_branch(request, entity, item),
                    "invoice_date": invoice_date, "due_date": due_date or invoice_date,
                    "vendor_reference": reference,
                    "narration": _text(item.get("narration"), "narration", 255),
                    "amount": amount,
                })
            except ValidationError as exc:
                raise ValidationError({"bills": {index: exc.detail}})
        try:
            bills = import_opening_vendor_invoices(entity, rows, actor_user=request.user)
        except FinanceError as exc:
            raise ValidationError({"bills": exc.message})
        return success_response(
            f"{len(bills)} opening bill(s) carried in.",
            data=[VendorInvoiceSerializer(bill).data for bill in bills], status=201,
        )
