"""Vendor-payment drafts, approval hand-off, posting, cancellation, and reversal.

Allocations on a draft are an editable settlement *plan*.  They do not reduce
invoice balances until the approved payment is posted by the payables service.
This boundary is important: workflow approval authorizes the plan; posting is
the separate accounting mutation that creates ledger history.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.response import success_response
from vs_finance.constants import DocumentStatus, PaymentMethod
from vs_finance.money import format_naira
from vs_finance.views import resolve_entity

from .. import approvals, payables
from ..constants import ProcApprovalState, VendorKycStatus
from ..models import VendorInvoice, VendorPayment, VendorPaymentAllocation
from ..serializers import VendorPaymentListSerializer, VendorPaymentSerializer
from .base import (
    _ProcBase,
    _branch_scoped,
    _date,
    _document_or_404,
    _inherited_branch_id,
    _money,
    _resolve_tax,
    _resolve_vendor,
)


def _payment_queryset(entity):
    """Eager-load every relation the detail drawer serializes (incl. the posted journal)."""
    return VendorPayment.objects.filter(entity=entity).select_related(
        "vendor", "payment_account", "payment_account__bank_account", "wht_tax_code",
        "journal", "created_by", "branch",
    ).prefetch_related(
        "allocations__vendor_invoice", "journal__lines__account",
    )


def _payment_list_queryset(entity):
    """Lighter list source - the list row never serializes the journal lines, so the
    journal select_related/prefetch the detail drawer needs are dropped here."""
    return VendorPayment.objects.filter(entity=entity).select_related(
        "vendor", "payment_account", "payment_account__bank_account", "wht_tax_code",
        "created_by", "branch",
    ).prefetch_related("allocations__vendor_invoice")


def _resolve_bank_account(entity, ref):
    """Resolve an active entity bank account backed by a postable GL account."""
    if ref in (None, ""):
        raise ValidationError({"bank_account": "An active bank or cash account is required."})
    from vs_finance.models import BankAccount

    account = BankAccount.objects.select_related("gl_account").filter(
        entity=entity, pk=ref, is_active=True,
        gl_account__is_active=True, gl_account__is_postable=True,
    ).first()
    if account is None:
        raise ValidationError({"bank_account": "No active bank account with a postable GL account exists in this entity."})
    return account


def _validate_vendor_for_payment(vendor):
    """Enforce vendor operational gates before money can enter a payment draft.

    Active status alone is insufficient: KYC must be verified and an explicit
    payment hold always wins, including while editing an older draft.
    """
    if not vendor.is_active:
        raise ValidationError({"vendor": "Inactive vendors cannot be paid."})
    if vendor.kyc_status != VendorKycStatus.VERIFIED:
        raise ValidationError({"vendor": "The vendor must be KYC verified before payment."})
    if vendor.on_hold:
        raise ValidationError({"vendor": "This vendor is on hold; payments are blocked."})


def _validate_method(value):
    """Return a supported finance payment method, defaulting to bank transfer."""
    method = value or PaymentMethod.BANK_TRANSFER
    if method not in PaymentMethod.values:
        raise ValidationError({"method": "Select a valid payment method."})
    return method


def _allocation_plan(entity, vendor, payload):
    """Validate a unique posted-invoice allocation plan for one entity/vendor.

    Client totals are ignored.  The server resolves posted invoices, checks each
    live balance, and returns the exact rows used to derive gross payment value.
    """
    if not isinstance(payload, list) or not payload:
        raise ValidationError({"allocations": "Select at least one posted vendor invoice."})
    invoice_ids = [item.get("vendor_invoice") for item in payload]
    if any(value in (None, "") for value in invoice_ids) or len(set(invoice_ids)) != len(invoice_ids):
        raise ValidationError({"allocations": "Each vendor invoice may be selected once."})
    # Resolve the entity/vendor/status join server-side so changing an invoice id
    # cannot allocate another tenant's liability or another vendor's balance.
    invoices = {
        invoice.pk: invoice for invoice in VendorInvoice.objects.filter(
            entity=entity, vendor=vendor, pk__in=invoice_ids, status=DocumentStatus.POSTED,
        )
    }
    plan = []
    for item in payload:
        invoice = invoices.get(int(item["vendor_invoice"]))
        if invoice is None:
            raise ValidationError({"allocations": "Every invoice must be posted and belong to the selected vendor."})
        amount = _money(item.get("amount", 0), "amount")
        if amount <= 0 or amount > invoice.balance_due:
            raise ValidationError({"allocations": f"Allocation for {invoice.document_number} must be positive and within its balance."})
        plan.append((invoice, amount))
    return plan


def _replace_plan(payment, plan):
    """Replace draft instructions without touching invoice settlement balances."""
    # Draft allocation rows are instructions only; invoice balances remain unchanged.
    payment.allocations.all().delete()
    VendorPaymentAllocation.objects.bulk_create([
        VendorPaymentAllocation(payment=payment, vendor_invoice=invoice, amount=amount)
        for invoice, amount in plan
    ])


def _activity_message(log):
    """Render immutable legacy audit rows without exposing internal kobo units."""
    metadata = log.metadata or {}
    if log.action == "VENDOR_PAYMENT_POSTED" and "net" in metadata:
        return f"Payment posted: {format_naira(metadata['net'])} net, {format_naira(metadata.get('wht', 0))} WHT."
    if log.action == "VENDOR_PAYMENT_ALLOCATED" and "allocated" in metadata:
        return f"Allocated {format_naira(metadata['allocated'])} to vendor invoices."
    return log.message


def _serialize_detail(payment):
    """Overlay workflow, posting, and audit context onto the canonical serializer.

    The overlay is read-only presentation data: it does not duplicate workflow or
    ledger state on the payment model.  Audit metadata is rendered into safe,
    human-readable activity rather than exposing the raw JSON field.
    """
    from vs_finance.models import FinanceAuditLog
    from vs_workflow.models import WorkflowInstance

    data = VendorPaymentSerializer(payment).data
    workflow = WorkflowInstance.all_objects.filter(
        document_type="procurement.vendor_payment", document_object_id=str(payment.pk),
    ).order_by("-created_at").first()
    data["workflow_instance_id"] = workflow.id if workflow else None
    data["posting_lines"] = [{
        "account_code": line.account.code, "account_name": line.account.name,
        "debit": line.debit, "credit": line.credit,
    } for line in payment.journal.lines.all()] if payment.journal_id else []
    data["activity"] = [{
        "id": log.id, "action": log.action, "message": _activity_message(log),
        "status": log.status,
        "actor_name": (
            f"{getattr(log.actor, 'first_name', '')} {getattr(log.actor, 'last_name', '')}".strip()
            or getattr(log.actor, "email", "System")
        ) if log.actor_id else "System",
        "created_at": log.created_at,
    } for log in FinanceAuditLog.objects.filter(
        entity=payment.entity, target_type="VendorPayment", target_id=str(payment.pk),
    ).select_related("actor").order_by("-created_at")[:20]]
    return data


class VendorPaymentListCreateView(_ProcBase):
    """List entity payments or create a gated, allocated payment draft."""

    @property
    def rbac_permission(self):
        """Require create permission for POST and view permission for GET."""
        return "procurement.vendor_payment.create" if self.request.method == "POST" \
            else "procurement.vendor_payment.view"

    def get(self, request):
        """Return a paginated, filterable payment console for the current entity."""
        entity = resolve_entity(request)
        qs = _branch_scoped(request, entity, _payment_list_queryset(entity), request.query_params)
        if status := request.query_params.get("status"):
            qs = qs.filter(status=status)
        if approval := request.query_params.get("approval_state"):
            qs = qs.filter(approval_state=approval)
        if search := request.query_params.get("search", "").strip():
            qs = qs.filter(Q(document_number__icontains=search) | Q(reference__icontains=search)
                           | Q(vendor__code__icontains=search) | Q(vendor__name__icontains=search))
        return self.paginate(request, qs.order_by("-id"), VendorPaymentListSerializer)

    @transaction.atomic
    def post(self, request):
        """Create a draft from server-resolved invoices and derived kobo totals."""
        entity = resolve_entity(request)
        body = request.data
        vendor = _resolve_vendor(entity, body.get("vendor"))
        _validate_vendor_for_payment(vendor)
        bank = _resolve_bank_account(entity, body.get("bank_account"))
        plan = _allocation_plan(entity, vendor, body.get("allocations"))
        gross = sum(amount for _, amount in plan)  # Gross is the exact approved liability split.
        wht = _money(body.get("wht_amount", 0), "wht_amount")
        if wht > gross:
            raise ValidationError({"wht_amount": "WHT cannot exceed the invoice amount being settled."})
        payment = VendorPayment.objects.create(
            entity=entity, vendor=vendor,
            # A settlement belongs to the branch of the bills it settles. Invoices
            # from different branches (only a caller who is not branch-bound can
            # select those) settle at entity level.
            branch_id=_inherited_branch_id(request, *(invoice for invoice, _ in plan)),
            payment_date=_date(body.get("payment_date"), "payment_date", required=True),
            method=_validate_method(body.get("method")), gross_amount=gross,
            wht_amount=wht, net_amount=gross - wht, allocated_amount=0,
            payment_account=bank.gl_account,
            wht_tax_code=_resolve_tax(entity, body.get("wht_tax_code")) or vendor.default_wht_tax_code,
            reference=str(body.get("reference") or "").strip(),
            narration=str(body.get("narration") or "").strip(),
            created_by=request.user if request.user.is_authenticated else None,
        )
        _replace_plan(payment, plan)
        return success_response(
            "Vendor payment draft created.", data=_serialize_detail(_payment_queryset(entity).get(pk=payment.pk)), status=201,
        )


class VendorPaymentEligibleInvoiceView(_ProcBase):
    """List up to 100 posted, unpaid invoices eligible for allocation.

    The optional vendor reference is resolved inside the current entity before
    filtering, so it cannot become a cross-tenant invoice-discovery channel.
    """
    rbac_permission = "procurement.vendor_payment.view"

    def get(self, request):
        """Return oldest-due eligible invoices, optionally for one vendor."""
        entity = resolve_entity(request)
        qs = VendorInvoice.objects.filter(entity=entity, status=DocumentStatus.POSTED).exclude(payment_status="PAID")
        # A branch-bound caller cannot settle another branch's bill, so this
        # picker must not offer one either.
        qs = _branch_scoped(request, entity, qs, request.query_params)
        if vendor := request.query_params.get("vendor"):
            resolved = _resolve_vendor(entity, vendor)
            qs = qs.filter(vendor=resolved)
        rows = [{
            "id": invoice.id, "document_number": invoice.document_number,
            "vendor_id": invoice.vendor_id, "vendor_code": invoice.vendor.code,
            "invoice_date": invoice.invoice_date, "due_date": invoice.due_date,
            "total": invoice.total, "amount_paid": invoice.amount_paid,
            "balance_due": invoice.balance_due, "payment_status": invoice.payment_status,
        } for invoice in qs.select_related("vendor").order_by("due_date", "invoice_date", "id")[:100]]
        return success_response("Eligible vendor invoices retrieved.", data=rows)


class VendorPaymentDetailView(_ProcBase):
    """Read a payment detail overlay or edit a mutable draft under row lock."""

    @property
    def rbac_permission(self):
        """Separate read access from permission to rewrite settlement intent."""
        return "procurement.vendor_payment.update" if self.request.method == "PATCH" \
            else "procurement.vendor_payment.view"

    def get(self, request, pk):
        """Return one entity-scoped payment without leaking foreign ids."""
        entity = resolve_entity(request)
        payment = _document_or_404(
            request, _payment_queryset(entity), pk,
            "No such vendor payment in this entity.",
        )
        return success_response("Vendor payment retrieved.", data=_serialize_detail(payment))

    @transaction.atomic
    def patch(self, request, pk):
        """Replace an unsubmitted/rejected draft and its allocation plan atomically."""
        entity = resolve_entity(request)
        # Serialize competing edits so an allocation plan and its derived totals
        # cannot be saved from different request snapshots.
        payment = _document_or_404(
            request, VendorPayment.objects.select_for_update().filter(entity=entity),
            pk, "No such vendor payment in this entity.",
        )
        if payment.status != DocumentStatus.DRAFT or payment.approval_state not in (
            ProcApprovalState.NOT_SUBMITTED, ProcApprovalState.REJECTED,
        ):
            raise ValidationError({"status": "Only an unsubmitted or rejected draft payment can be edited."})
        body = request.data
        vendor = _resolve_vendor(entity, body.get("vendor", payment.vendor_id))
        _validate_vendor_for_payment(vendor)
        bank = _resolve_bank_account(entity, body.get("bank_account", getattr(getattr(payment.payment_account, "bank_account", None), "id", None)))
        plan = _allocation_plan(entity, vendor, body.get("allocations"))
        gross = sum(amount for _, amount in plan)  # Editing recomputes, never trusts a client total.
        wht = _money(body.get("wht_amount", payment.wht_amount), "wht_amount")
        if wht > gross:
            raise ValidationError({"wht_amount": "WHT cannot exceed the invoice amount being settled."})
        payment.vendor = vendor
        payment.payment_date = _date(body.get("payment_date", payment.payment_date), "payment_date", required=True)
        payment.method = _validate_method(body.get("method", payment.method))
        payment.gross_amount = gross
        payment.wht_amount = wht
        payment.net_amount = gross - wht
        payment.allocated_amount = 0
        payment.payment_account = bank.gl_account
        payment.wht_tax_code = _resolve_tax(entity, body.get("wht_tax_code")) if "wht_tax_code" in body else payment.wht_tax_code
        payment.reference = str(body.get("reference", payment.reference) or "").strip()
        payment.narration = str(body.get("narration", payment.narration) or "").strip()
        payment.approval_state = ProcApprovalState.NOT_SUBMITTED
        payment.save()
        _replace_plan(payment, plan)
        return success_response("Vendor payment draft updated.", data=_serialize_detail(_payment_queryset(entity).get(pk=pk)))


class VendorPaymentSubmitView(_ProcBase):
    """Hand a complete draft to workflow; submission does not post accounting."""
    rbac_permission = "procurement.vendor_payment.submit"

    def post(self, request, pk):
        """Submit a draft allocation plan for approval eligibility checks."""
        entity = resolve_entity(request)
        payment = _document_or_404(
            request, _payment_queryset(entity), pk,
            "No such vendor payment in this entity.",
        )
        if payment.status != DocumentStatus.DRAFT or not payment.allocations.exists():
            raise ValidationError({"status": "Only a draft with invoice allocations can be submitted."})
        instance = approvals.submit_for_approval(payment, actor_user=request.user)
        from vs_workflow.services import release as release_svc

        payment.refresh_from_db()
        return success_response("Vendor payment submitted for approval.", data={
            "document": VendorPaymentSerializer(payment).data,
            "workflow_instance_id": instance.pk,
            "approval_state": payment.approval_state,
            # See _approval_response in views/requisitions.py: same contract, so the
            # four procurement submit screens answer "who approves this" identically.
            "approval": release_svc.approval_block(instance),
        })


class VendorPaymentPostView(_ProcBase):
    """Post an approved payment through the payables accounting boundary."""
    rbac_permission = "procurement.vendor_payment.post"

    def post(self, request, pk):
        """Create settlement effects from the approved allocation plan."""
        entity = resolve_entity(request)
        payment = _document_or_404(
            request, _payment_queryset(entity), pk,
            "No such vendor payment in this entity.",
        )
        if not payment.allocations.exists():
            raise ValidationError({"allocations": "An approved invoice-allocation plan is required before posting."})
        # Explicit allocations are approval evidence; never let posting silently
        # invent a different oldest-first plan.
        payables.post_vendor_payment(payment, actor_user=request.user, auto_allocate=False)
        return success_response(
            f"Vendor payment {payment.document_number} posted.",
            data=_serialize_detail(_payment_queryset(entity).get(pk=pk)),
        )


class VendorPaymentAllocateAdvanceView(_ProcBase):
    """POST /procurement/vendor-payments/<id>/allocate/ - draw a vendor advance down.

    Money paid to a supplier before their bill existed sits in the vendor-advance
    asset (1240). This applies it to bills that have since been raised, reclassifying
    it into AP (``Dr AP, Cr vendor advances``) and settling them. No cash moves; the
    disbursement already happened.

    Body ``{allocations:[{vendor_invoice, amount}]}`` for an explicit split, or
    ``{auto_allocate:true}`` to settle the vendor's open bills oldest-first. Each
    amount is capped at the bill's balance and the advance still remaining.

    The AP mirror of ``/finance/payments/<id>/allocate/``. Note the deliberate
    difference from *posting*: posting refuses to settle a bill dated after the
    payment, because on that date the liability did not exist. Here a newer bill is
    the whole point - the advance was paid ahead of it - so the reclassification is
    dated at the later of the two documents instead of being refused.

    docstring-name: Apply a vendor advance
    """

    rbac_permission = "procurement.vendor_payment.allocate"

    @transaction.atomic
    def post(self, request, pk):
        """Apply the advance, then return the payment with its refreshed figures."""
        entity = resolve_entity(request)
        payment = _document_or_404(
            request, _payment_queryset(entity), pk,
            "No such vendor payment in this entity.",
        )
        if payment.status != DocumentStatus.POSTED:
            raise ValidationError(
                {"status": "Only a posted vendor payment holds an advance to apply."})
        if payment.advance_remaining <= 0:
            raise ValidationError(
                {"allocations": "This payment has no advance left to apply."})

        body = request.data or {}
        raw = body.get("allocations")
        before = payment.advance_remaining  # Report what this call did, not the total.
        if raw:
            # Reuse the draft-plan validator: it resolves each bill against this
            # entity AND this vendor server-side, so a swapped id cannot reach
            # another tenant's liability.
            plan = _allocation_plan(entity, payment.vendor, raw)
            payables.allocate_vendor_payment(
                payment, allocations=plan, actor_user=request.user, strict=True)
        elif body.get("auto_allocate"):
            payables.allocate_vendor_payment(payment, actor_user=request.user)
        else:
            raise ValidationError(
                {"allocations": "Provide allocations or auto_allocate=true."})

        payment.refresh_from_db()
        applied = before - payment.advance_remaining
        message = (
            f"Applied {format_naira(applied)} of {payment.document_number} to open bills."
            if applied > 0
            # Auto-allocation finding nothing eligible is a real outcome, not an error:
            # the advance is intact and the vendor simply has no open bill yet.
            else f"No open bill could take {payment.document_number}'s advance."
        )
        return success_response(
            message, data=_serialize_detail(_payment_queryset(entity).get(pk=pk)),
        )


class VendorPaymentCancelView(_ProcBase):
    """Cancel only a non-pending draft; posted history is never deleted here."""
    rbac_permission = "procurement.vendor_payment.cancel"

    @transaction.atomic
    def post(self, request, pk):
        """Lock and cancel an eligible draft without racing submission/posting."""
        entity = resolve_entity(request)
        payment = _document_or_404(
            request, VendorPayment.objects.select_for_update().filter(entity=entity),
            pk, "No such vendor payment in this entity.",
        )
        if payment.status != DocumentStatus.DRAFT or payment.approval_state == ProcApprovalState.PENDING:
            raise ValidationError({"status": "Only a non-pending, unposted payment can be cancelled."})
        payment.status = DocumentStatus.CANCELLED
        payment.save(update_fields=["status", "updated_at"])
        return success_response("Vendor payment cancelled.", data=VendorPaymentSerializer(payment).data)


class VendorPaymentReverseView(_ProcBase):
    """Reverse a posted payment through the accounting service."""
    rbac_permission = "procurement.vendor_payment.reverse"

    def post(self, request, pk):
        """Create dated reversing effects while preserving the original payment."""
        entity = resolve_entity(request)
        payment = _document_or_404(
            request, _payment_queryset(entity), pk,
            "No such vendor payment in this entity.",
        )
        reversal_date = _date(request.data.get("date"), "date") or timezone.localdate()
        payables.reverse_vendor_payment(payment, actor_user=request.user, date=reversal_date)
        return success_response(
            f"Vendor payment {payment.document_number} reversed.",
            data=_serialize_detail(_payment_queryset(entity).get(pk=pk)),
        )
