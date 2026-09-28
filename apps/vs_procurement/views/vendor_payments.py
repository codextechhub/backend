"""Vendor-payment drafts, approval hand-off, posting, cancellation, and reversal.

Allocations on a draft are an editable settlement *plan*.  They do not reduce
invoice balances until the approved payment is posted by the payables service.
This boundary is important: workflow approval authorizes the plan; posting is
the separate accounting mutation that creates ledger history.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.constants import DocumentStatus, PaymentMethod
from vs_finance.money import format_naira
from vs_finance.views import resolve_entity
from vs_finance.views_ops.base import require_own_branch_bank
from vs_config.clock import tenant_today

from .. import approvals, payables
from ..constants import ProcApprovalState, VendorKycStatus, WhtSource
from ..models import VendorInvoice, VendorPayment, VendorPaymentAllocation
from ..serializers import VendorPaymentListSerializer, VendorPaymentSerializer
from .base import (
    _ProcBase,
    _branch_q,
    _branch_scoped,
    _branch_visible,
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
        "attachments__uploaded_by",
    )


def _payment_list_queryset(entity):
    """Lighter list source - the list row never serializes the journal lines, so the
    journal select_related/prefetch the detail drawer needs are dropped here."""
    return VendorPayment.objects.filter(entity=entity).select_related(
        "vendor", "payment_account", "payment_account__bank_account", "wht_tax_code",
        "created_by", "branch",
    ).prefetch_related("allocations__vendor_invoice")


def _resolve_bank_account(request, entity, ref, *, document_branch):
    """Resolve an active bank account, backed by a postable GL account, the caller can see.

    Which accounts a caller may name, and which a branch's payment may be paid
    from, are finance's rules, read through finance's resolver: the caller's
    branches' accounts and the school-wide ones, and for a payment that belongs
    to a branch, that branch's or a school-wide one. What is added here is the
    payment's own condition, that the account and its ledger account are open
    for posting. A payment's branch is the one its bills give
    (:func:`_settled_branch_id`): an edit and a post derive it first and pass it
    here, while a new payment names its account before its bills are resolved,
    so it passes ``document_branch=None`` and checks the branch afterwards with
    :func:`require_own_branch_bank`.
    """
    if ref in (None, ""):
        raise ValidationError({"bank_account": "An active bank or cash account is required."})
    from vs_finance.views_ops.base import _resolve_bank_account as _reachable_bank_account

    account = _reachable_bank_account(
        request, entity, ref, document_branch=document_branch, noun="vendor payment")
    gl = account.gl_account
    if not (account.is_active and gl.is_active and gl.is_postable):
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


#: The answer for a bill that is not this vendor's, not posted, or outside reach.
UNKNOWN_BILL = "Every invoice must be posted and belong to the selected vendor."


def _allocation_plan(request, entity, vendor, payload):
    """Validate a unique posted-invoice allocation plan for one entity/vendor.

    Client totals are ignored.  The server resolves posted invoices, checks each
    live balance, and returns the exact rows used to derive gross payment value.

    Bills resolve within the caller's branches, read the way every procurement
    document is (:func:`_branch_visible`), which is also what the eligible-bill
    picker offers. Ikeja's officer naming a Lekki bill by id gets the same 400
    as for a bill that does not exist, on create, on edit and when applying an
    advance alike.
    """
    if not isinstance(payload, list) or not payload:
        raise ValidationError({"allocations": "Select at least one posted vendor invoice."})
    invoice_ids = [item.get("vendor_invoice") for item in payload]
    if any(value in (None, "") for value in invoice_ids) or len(set(invoice_ids)) != len(invoice_ids):
        raise ValidationError({"allocations": "Each vendor invoice may be selected once."})
    # Resolve the entity/vendor/status join server-side so changing an invoice id
    # cannot allocate another tenant's liability or another vendor's balance.
    invoices = {
        invoice.pk: invoice for invoice in _branch_visible(request, VendorInvoice.objects.filter(
            entity=entity, vendor=vendor, pk__in=invoice_ids, status=DocumentStatus.POSTED,
        ))
    }
    plan = []
    for item in payload:
        invoice = invoices.get(int(item["vendor_invoice"]))
        if invoice is None:
            raise ValidationError({"allocations": UNKNOWN_BILL})
        amount = _money(item.get("amount", 0), "amount")
        if amount <= 0 or amount > invoice.balance_due:
            raise ValidationError({"allocations": f"Allocation for {invoice.document_number} must be positive and within its balance."})
        plan.append((invoice, amount))
    return plan


def _settled_branch_id(request, plan):
    """The branch a payment takes from the bills it settles, checked against the caller.

    Bills of one branch give that branch; bills of several (only a caller who is not
    branch-bound can select those) settle at entity level. Create, edit and post all
    derive it here, so an edit that swaps the bills moves the payment with them.
    """
    return _inherited_branch_id(request, *(invoice for invoice, _ in plan))


def _resolve_payment_wht(body, *, gross, tax_code, plan, existing=None):
    """The WHT a draft carries: typed by the caller, or computed from its tax code.

    A ``wht_amount`` in the body is kept as entered. Without one, a new draft
    computes it through :func:`vs_procurement.payables.resolve_wht`. An edit that
    omits it recomputes when the draft's figure was computed (the bills or the
    code may have changed) and keeps any other figure: one somebody typed is a
    deliberate choice that an unrelated edit must not erase, and a draft saved
    before the source was recorded is treated the same way.
    """
    supplied = body.get("wht_amount")
    if supplied in (None, "") and existing is not None and existing.wht_source != WhtSource.COMPUTED:
        supplied = existing.wht_amount
    if supplied not in (None, ""):
        supplied = _money(supplied, "wht_amount")
    else:
        supplied = None
    wht = payables.resolve_wht(gross=gross, supplied=supplied, tax_code=tax_code, bills=plan)
    if wht.amount > gross:
        raise ValidationError({"wht_amount": "WHT cannot exceed the invoice amount being settled."})
    return wht


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
        vendor = _resolve_vendor(request, entity, body.get("vendor"))
        _validate_vendor_for_payment(vendor)
        bank = _resolve_bank_account(request, entity, body.get("bank_account"), document_branch=None)
        plan = _allocation_plan(request, entity, vendor, body.get("allocations"))
        gross = sum(amount for _, amount in plan)  # Gross is the exact approved liability split.
        wht_code = _resolve_tax(entity, body.get("wht_tax_code")) or vendor.default_wht_tax_code
        wht = _resolve_payment_wht(body, gross=gross, tax_code=wht_code, plan=plan)
        branch_id = _settled_branch_id(request, plan)
        require_own_branch_bank(bank, branch_id, noun="vendor payment")
        payment = VendorPayment.objects.create(
            entity=entity, vendor=vendor,
            branch_id=branch_id,
            payment_date=_date(body.get("payment_date"), "payment_date", required=True),
            method=_validate_method(body.get("method")), gross_amount=gross,
            wht_amount=wht.amount, net_amount=gross - wht.amount, allocated_amount=0,
            wht_source=wht.source,
            payment_account=bank.gl_account,
            wht_tax_code=wht_code,
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
            resolved = _resolve_vendor(request, entity, vendor)
            qs = qs.filter(vendor=resolved)
        rows = [{
            "id": invoice.id, "document_number": invoice.document_number,
            "vendor_id": invoice.vendor_id, "vendor_code": invoice.vendor.code,
            # The payment inherits the bills' branch, which decides the bank it may use.
            "branch_id": invoice.branch_id,
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
        vendor = _resolve_vendor(request, entity, body.get("vendor", payment.vendor_id))
        _validate_vendor_for_payment(vendor)
        plan = _allocation_plan(request, entity, vendor, body.get("allocations"))
        branch_id = _settled_branch_id(request, plan)
        bank = _resolve_bank_account(
            request, entity,
            body.get("bank_account", getattr(getattr(payment.payment_account, "bank_account", None), "id", None)),
            document_branch=branch_id,
        )
        gross = sum(amount for _, amount in plan)  # Editing recomputes, never trusts a client total.
        wht_code = (
            _resolve_tax(entity, body.get("wht_tax_code")) if "wht_tax_code" in body
            else payment.wht_tax_code
        )
        wht = _resolve_payment_wht(
            body, gross=gross, tax_code=wht_code, plan=plan, existing=payment,
        )
        payment.vendor = vendor
        payment.branch_id = branch_id
        payment.payment_date = _date(body.get("payment_date", payment.payment_date), "payment_date", required=True)
        payment.method = _validate_method(body.get("method", payment.method))
        payment.gross_amount = gross
        payment.wht_amount = wht.amount
        payment.wht_source = wht.source
        payment.net_amount = gross - wht.amount
        payment.allocated_amount = 0
        payment.payment_account = bank.gl_account
        payment.wht_tax_code = wht_code
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
        instance = approvals.submit_for_approval(
            payment, actor_user=request.user,
            confirm_without_approval=bool((request.data or {}).get("confirm_without_approval")),
            confirmation_reason=str((request.data or {}).get("reason") or "").strip(),
        )
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


def _recheck_branch_before_posting(request, entity, payment):
    """Refuse to post a draft whose branch no longer holds, before any money moves.

    A draft is checked when it is written, but it is posted later, possibly by
    someone else, after a bill or a bank account has changed, or after it was
    written by code that did not apply these rules. So posting asks the same three
    questions again:

    * every bill in the plan is one this caller can reach (the 400 of an unknown
      bill otherwise);
    * the bills still give the branch the payment carries, which is the branch
      its journal is booked to (a 400 asking for the draft to be edited, which
      re-derives it);
    * the bank account is one this caller can reach, and is that branch's or a
      school-wide one (finance's 404 and 400, see
      :func:`vs_finance.views_ops.base._resolve_bank_account`).

    A payment whose ledger account backs no bank account was not written by this
    screen, which always names one, and has no bank account to re-check.
    """
    invoice_ids = [row.vendor_invoice_id for row in payment.allocations.all()]
    bills = list(_branch_visible(request, VendorInvoice.objects.filter(
        entity=entity, pk__in=invoice_ids)))
    if len(bills) != len(set(invoice_ids)):
        raise ValidationError({"allocations": UNKNOWN_BILL})
    branch_id = _inherited_branch_id(request, *bills)
    if branch_id != payment.branch_id:
        raise ValidationError({"allocations": (
            "The bills this payment settles now belong to another branch. "
            "Edit the draft to bring it up to date before posting."
        )})
    bank = getattr(payment.payment_account, "bank_account", None)
    if bank is not None:
        _resolve_bank_account(request, entity, bank.pk, document_branch=branch_id)


def post_payment_for_caller(request, entity, payment):
    """Post an approved vendor payment on behalf of the caller behind ``request``.

    The one route by which a person posts a vendor payment, whether from the
    procurement screen or through the school's FAL, so both ask the same
    questions before any money is booked:

    * the payment is one this caller can reach (404 otherwise, as for a payment
      that does not exist);
    * it carries a saved allocation plan, because the plan is what was approved
      and posting must not settle different bills from the ones approved;
    * the plan's bills, its branch and its bank account still hold for this
      caller (:func:`_recheck_branch_before_posting`).

    ``request`` needs only a ``user``: a caller with no HTTP request passes the
    acting user as ``SimpleNamespace(user=user)``. A gateway payout's booking is
    not a person posting and does not come through here.
    """
    if not _branch_visible(request, VendorPayment.objects.filter(pk=payment.pk)).exists():
        raise NotFound("No such vendor payment in this entity.")
    if not payment.allocations.exists():
        raise ValidationError({"allocations": "An approved invoice-allocation plan is required before posting."})
    _recheck_branch_before_posting(request, entity, payment)
    return payables.post_vendor_payment(payment, actor_user=request.user, auto_allocate=False)


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
        post_payment_for_caller(request, entity, payment)
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
    ``{auto_allocate:true}`` to settle the vendor's open bills oldest-first: only bills
    of the payment's own branch (school-wide bills for a school-wide payment) that the
    caller can reach. Each amount is capped at the bill's balance and the advance still
    remaining.

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
            # The draft-plan validator: each bill within this entity, vendor and
            # the caller's branches.
            plan = _allocation_plan(request, entity, payment.vendor, raw)
            payables.allocate_vendor_payment(
                payment, allocations=plan, actor_user=request.user, strict=True)
        elif body.get("auto_allocate"):
            # Oldest-first among the payment's own branch's bills this caller reaches.
            payables.allocate_vendor_payment(
                payment, actor_user=request.user, bill_scope=_branch_q(request))
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
        reversal_date = _date(request.data.get("date"), "date") or tenant_today(entity.tenant)
        payables.reverse_vendor_payment(payment, actor_user=request.user, date=reversal_date)
        return success_response(
            f"Vendor payment {payment.document_number} reversed.",
            data=_serialize_detail(_payment_queryset(entity).get(pk=pk)),
        )
