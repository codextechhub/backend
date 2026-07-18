"""Vendor-payment console endpoints and lifecycle actions."""
from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.constants import DocumentStatus, PaymentMethod
from vs_finance.money import format_naira
from vs_finance.views import resolve_entity

from .. import approvals, payables
from ..constants import ProcApprovalState, VendorKycStatus
from ..models import VendorInvoice, VendorPayment, VendorPaymentAllocation
from ..serializers import VendorPaymentListSerializer, VendorPaymentSerializer
from .base import _ProcBase, _date, _money, _resolve_tax, _resolve_vendor


def _payment_queryset(entity):
    """Eager-load every relation the detail drawer serializes (incl. the posted journal)."""
    return VendorPayment.objects.filter(entity=entity).select_related(
        "vendor", "payment_account", "payment_account__bank_account", "wht_tax_code",
        "journal", "created_by",
    ).prefetch_related(
        "allocations__vendor_invoice", "journal__lines__account",
    )


def _payment_list_queryset(entity):
    """Lighter list source — the list row never serializes the journal lines, so the
    journal select_related/prefetch the detail drawer needs are dropped here."""
    return VendorPayment.objects.filter(entity=entity).select_related(
        "vendor", "payment_account", "payment_account__bank_account", "wht_tax_code", "created_by",
    ).prefetch_related("allocations__vendor_invoice")


def _resolve_bank_account(entity, ref):
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
    if not vendor.is_active:
        raise ValidationError({"vendor": "Inactive vendors cannot be paid."})
    if vendor.kyc_status != VendorKycStatus.VERIFIED:
        raise ValidationError({"vendor": "The vendor must be KYC verified before payment."})
    if vendor.on_hold:
        raise ValidationError({"vendor": "This vendor is on hold; payments are blocked."})


def _validate_method(value):
    method = value or PaymentMethod.BANK_TRANSFER
    if method not in PaymentMethod.values:
        raise ValidationError({"method": "Select a valid payment method."})
    return method


def _allocation_plan(entity, vendor, payload):
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
    @property
    def rbac_permission(self):
        return "procurement.vendor_payment.create" if self.request.method == "POST" \
            else "procurement.vendor_payment.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _payment_list_queryset(entity)
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
    rbac_permission = "procurement.vendor_payment.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = VendorInvoice.objects.filter(entity=entity, status=DocumentStatus.POSTED).exclude(payment_status="PAID")
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
    @property
    def rbac_permission(self):
        return "procurement.vendor_payment.update" if self.request.method == "PATCH" \
            else "procurement.vendor_payment.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        payment = _payment_queryset(entity).filter(pk=pk).first()
        if payment is None:
            raise NotFound("No such vendor payment in this entity.")
        return success_response("Vendor payment retrieved.", data=_serialize_detail(payment))

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        payment = VendorPayment.objects.select_for_update().filter(entity=entity, pk=pk).first()
        if payment is None:
            raise NotFound("No such vendor payment in this entity.")
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
    rbac_permission = "procurement.vendor_payment.submit"

    def post(self, request, pk):
        entity = resolve_entity(request)
        payment = _payment_queryset(entity).filter(pk=pk).first()
        if payment is None:
            raise NotFound("No such vendor payment in this entity.")
        if payment.status != DocumentStatus.DRAFT or not payment.allocations.exists():
            raise ValidationError({"status": "Only a draft with invoice allocations can be submitted."})
        instance = approvals.submit_for_approval(payment, actor_user=request.user)
        return success_response("Vendor payment submitted for approval.", data={
            "document": VendorPaymentSerializer(payment).data,
            "workflow_instance_id": instance.pk,
            "approval_state": payment.approval_state,
        })


class VendorPaymentPostView(_ProcBase):
    rbac_permission = "procurement.vendor_payment.post"

    def post(self, request, pk):
        entity = resolve_entity(request)
        payment = _payment_queryset(entity).filter(pk=pk).first()
        if payment is None:
            raise NotFound("No such vendor payment in this entity.")
        if not payment.allocations.exists():
            raise ValidationError({"allocations": "An approved invoice-allocation plan is required before posting."})
        payables.post_vendor_payment(payment, actor_user=request.user, auto_allocate=False)
        return success_response(
            f"Vendor payment {payment.document_number} posted.",
            data=_serialize_detail(_payment_queryset(entity).get(pk=pk)),
        )


class VendorPaymentCancelView(_ProcBase):
    rbac_permission = "procurement.vendor_payment.cancel"

    @transaction.atomic
    def post(self, request, pk):
        entity = resolve_entity(request)
        payment = VendorPayment.objects.select_for_update().filter(entity=entity, pk=pk).first()
        if payment is None:
            raise NotFound("No such vendor payment in this entity.")
        if payment.status != DocumentStatus.DRAFT or payment.approval_state == ProcApprovalState.PENDING:
            raise ValidationError({"status": "Only a non-pending, unposted payment can be cancelled."})
        payment.status = DocumentStatus.CANCELLED
        payment.save(update_fields=["status", "updated_at"])
        return success_response("Vendor payment cancelled.", data=VendorPaymentSerializer(payment).data)


class VendorPaymentReverseView(_ProcBase):
    rbac_permission = "procurement.vendor_payment.reverse"

    def post(self, request, pk):
        entity = resolve_entity(request)
        payment = _payment_queryset(entity).filter(pk=pk).first()
        if payment is None:
            raise NotFound("No such vendor payment in this entity.")
        reversal_date = _date(request.data.get("date"), "date") or timezone.localdate()
        payables.reverse_vendor_payment(payment, actor_user=request.user, date=reversal_date)
        return success_response(
            f"Vendor payment {payment.document_number} reversed.",
            data=_serialize_detail(_payment_queryset(entity).get(pk=pk)),
        )
