"""Expense claims.
"""
from __future__ import annotations


from django.db import transaction
from rest_framework.exceptions import NotFound
from vs_rbac.scoping import branch_q  # include_shared spelled out per call site

from core.response import success_response

from ..views import resolve_entity
from ..models import (
    ExpenseClaim,
    ExpenseClaimLine,
)
from ..serializers import (
    ExpenseClaimSerializer,
)


from .base import (
    _FinanceBase,
    _date,
    _dec,
    _raised_branch,
    _money,
    _require_lines,
    _resolve_account,
    _resolve_bank_account,
    _resolve_cost_center,
    _resolve_currency,
    _resolve_tax,
)

# --------------------------------------------------------------------------- #
# Expense claims                                                              #
# --------------------------------------------------------------------------- #

# Group endpoint behavior for Expense Claim List Create View.
class ExpenseClaimListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) expense claims for an entity.

    docstring-name: Expense claims
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.expenseclaim.create" if self.request.method == "POST" \
            else "finance.expenseclaim.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from ..approvals import ApprovalGate
        from ..constants import DocumentStatus, InvoicePaymentStatus

        entity = resolve_entity(request)
        qs = ExpenseClaim.objects.filter(
            branch_q(request, include_shared=True), entity=entity,
        ).prefetch_related("lines")
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        if (pay := request.query_params.get("payment_status")):
            qs = qs.filter(payment_status=pay)
        # The UI collapses (status × payment_status) into display states; translate
        # them to the underlying filters so the list stays filtered server-side.
        disp = request.query_params.get("display_status")
        if disp == "DRAFT":
            qs = qs.filter(status=DocumentStatus.DRAFT)
        elif disp == "REJECTED":
            qs = qs.filter(status=DocumentStatus.CANCELLED)
        elif disp == "PAID":
            qs = qs.filter(status=DocumentStatus.POSTED,
                           payment_status=InvoicePaymentStatus.PAID)
        elif disp == "APPROVED":  # posted but not yet fully reimbursed
            qs = qs.filter(status=DocumentStatus.POSTED).exclude(
                payment_status=InvoicePaymentStatus.PAID)
        elif disp == "PENDING":
            qs = qs.filter(status=DocumentStatus.PENDING_APPROVAL)
        if (search := request.query_params.get("q")):
            from django.db.models import Q
            qs = qs.filter(
                Q(claimant_name__icontains=search)
                | Q(title__icontains=search)
                | Q(document_number__icontains=search)
            )
        return self.paginate(
            request, qs.order_by("-claim_date", "-id"), ExpenseClaimSerializer,
            context={"request": request, "approval_gate": ApprovalGate()},
        )

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..expenses import price_expense_claim

        entity = resolve_entity(request)
        body = request.data or {}
        lines = _require_lines(body)
        claim = ExpenseClaim.objects.create(
            entity=entity,
            # A claim is money one person spent doing their job at one place, so
            # the strict reading applies: a claims officer covering two branches
            # is asked which this one is rather than having it filed school-wide.
            branch=_raised_branch(request, entity, body),
            claimant_name=body.get("claimant_name", ""),
            claim_date=_date(body.get("claim_date"), "claim_date", required=True),
            title=body.get("title", ""),
            narration=body.get("narration", ""),
            currency=_resolve_currency(body.get("currency")),
            created_by=request.user,
        )
        for i, ln in enumerate(lines, start=1):
            ExpenseClaimLine.objects.create(
                claim=claim, line_no=i,
                description=ln.get("description", ""),
                expense_account=_resolve_account(
                    entity, ln.get("expense_account"),
                    f"lines[{i}].expense_account", required=True),
                quantity=_dec(ln.get("quantity", 1), f"lines[{i}].quantity"),
                unit_price=_money(ln.get("unit_price", 0), f"lines[{i}].unit_price"),
                tax_code=_resolve_tax(
                    entity, ln.get("tax_code"), f"lines[{i}].tax_code",
                    usage="purchase",
                ),
                cost_center=_resolve_cost_center(
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            )
        price_expense_claim(claim)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} created.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data, status=201,
        )


# Define Expense Claim Action Base values.
class _ExpenseClaimActionBase(_FinanceBase):
    # Support the claim workflow.
    def _claim(self, request, pk):
        entity = resolve_entity(request)
        claim = ExpenseClaim.objects.filter(
            branch_q(request, include_shared=True), entity=entity, pk=pk,
        ).first()
        if claim is None:
            raise NotFound("Expense claim not found for this entity.")
        return entity, claim


# Group endpoint behavior for Expense Claim Detail View.
class ExpenseClaimDetailView(_ExpenseClaimActionBase):
    """docstring-name: Expense claims"""
    rbac_permission = "finance.expenseclaim.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, claim = self._claim(request, pk)
        return success_response(
            "Expense claim retrieved.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


# Group endpoint behavior for Expense Claim Post View.
class ExpenseClaimPostView(_ExpenseClaimActionBase):
    """docstring-name: Post an expense claim"""
    rbac_permission = "finance.expenseclaim.post"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from rest_framework.exceptions import ValidationError

        from ..approvals import approval_required
        from ..expenses import post_expense_claim

        _, claim = self._claim(request, pk)
        if approval_required(claim):
            raise ValidationError({
                "detail": "This expense claim is approval-gated; submit it for "
                          "approval instead of posting it directly."
            })
        post_expense_claim(claim, actor_user=request.user)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} posted.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


class ExpenseClaimSubmitView(_ExpenseClaimActionBase):
    """POST a draft expense claim into its configured approval workflow."""

    # Submitting cannot post or pay anything. Claim creators already hold the
    # authority to finish and hand off their own draft, so the hand-off needs
    # no tenant grant of its own.
    rbac_permission = "finance.expenseclaim.create"

    def post(self, request, pk):
        from vs_workflow.services import release as release_svc
        from vs_workflow.services.submission import submit_for_approval

        _, claim = self._claim(request, pk)
        instance = submit_for_approval(claim, requested_by=request.user)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} submitted for approval.",
            data=ExpenseClaimSerializer(
                claim, context={"request": request},
            ).data | {"approval": release_svc.approval_block(instance)},
        )


# Group endpoint behavior for Expense Claim Reject View.
class ExpenseClaimRejectView(_ExpenseClaimActionBase):
    """POST - reject (cancel) a draft expense claim. docstring-name: Reject an expense claim"""
    rbac_permission = "finance.expenseclaim.post"  # the approver decides approve OR reject

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..constants import DocumentStatus
        from rest_framework.exceptions import ValidationError

        _, claim = self._claim(request, pk)
        if claim.status != DocumentStatus.DRAFT:
            raise ValidationError(
                {"status": f"Only a draft claim can be rejected (this is '{claim.status}')."})
        claim.status = DocumentStatus.CANCELLED
        claim.save(update_fields=["status", "updated_at"])
        return success_response(
            f"Expense claim {claim.document_number} rejected.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


# Group endpoint behavior for Expense Claim Receipt View.
class ExpenseClaimReceiptView(_ExpenseClaimActionBase):
    """POST (multipart ``file``) attach / DELETE a receipt on a claim line.

    docstring-name: Expense line receipt
    """

    rbac_permission = "finance.expenseclaim.create"

    # Support the line workflow.
    def _line(self, request, pk, line_id):
        from rest_framework.exceptions import ValidationError

        from ..constants import DocumentStatus

        _, claim = self._claim(request, pk)
        line = claim.lines.filter(pk=line_id).first()
        if line is None:
            raise NotFound("Line not found on this claim.")
        if claim.status != DocumentStatus.DRAFT:
            raise ValidationError({
                "detail": "Receipt evidence can only be changed while the claim is a draft."
            })
        return claim, line

    # Handle POST requests for this endpoint.
    def post(self, request, pk, line_id):
        from rest_framework.exceptions import ValidationError

        from core.uploads import validate_upload

        claim, line = self._line(request, pk, line_id)
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "A receipt file is required."})
        # Validate before saving. DatabaseStorage re-checks type and size, but it raises
        # from inside _save, which surfaces as a 500 rather than a 400, and it never
        # inspects content - so a corrupt or mislabelled receipt was stored and only
        # discovered when somebody tried to read it.
        validate_upload(upload)
        line.receipt.save(upload.name, upload, save=True)
        claim.refresh_from_db()
        return success_response(
            "Receipt attached.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data, status=201,
        )

    # Handle DELETE requests for this endpoint.
    def delete(self, request, pk, line_id):
        claim, line = self._line(request, pk, line_id)
        if line.receipt:
            line.receipt.delete(save=True)
        claim.refresh_from_db()
        return success_response(
            "Receipt removed.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


# Group endpoint behavior for Expense Claim Settle View.
class ExpenseClaimSettleView(_ExpenseClaimActionBase):
    """docstring-name: Settle an expense claim"""
    rbac_permission = "finance.expenseclaim.settle"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..expenses import settle_expense_claim

        entity, claim = self._claim(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(entity, body.get("bank_account"))
        amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") else None
        settle_expense_claim(
            claim, bank_account=bank,
            pay_date=_date(body.get("pay_date"), "pay_date", required=True),
            amount=amount, actor_user=request.user,
        )
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} reimbursed.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


# Group endpoint behavior for Expense Claim Void View.
class ExpenseClaimVoidView(_ExpenseClaimActionBase):
    """POST - void a posted, un-reimbursed claim (reverses its journal, marks CANCELLED).

    docstring-name: Void an expense claim
    """
    rbac_permission = "finance.expenseclaim.post"  # the approver undoes their approval

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..expenses import void_expense_claim

        _, claim = self._claim(request, pk)
        void_expense_claim(claim, actor_user=request.user)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} voided.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


# Group endpoint behavior for Expense Claim Summary View.
class ExpenseClaimSummaryView(_FinanceBase):
    """GET - header KPIs over **all** expense claims (accurate under pagination).

    docstring-name: Expense claims
    """

    rbac_permission = "finance.expenseclaim.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from django.db.models import Count, Q, Sum
        from django.db.models.functions import Coalesce
        from django.utils import timezone

        from ..constants import DocumentStatus, InvoicePaymentStatus

        entity = resolve_entity(request)
        today = timezone.now().date()
        live = ~Q(status=DocumentStatus.CANCELLED)
        awaiting_q = Q(status=DocumentStatus.POSTED) & ~Q(
            payment_status=InvoicePaymentStatus.PAID)

        agg = ExpenseClaim.objects.filter(
            branch_q(request, include_shared=True), entity=entity,
        ).aggregate(
            open=Count("id", filter=Q(status__in=[
                DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL,
            ]) | awaiting_q),
            month_total=Coalesce(Sum("total", filter=live & Q(
                claim_date__year=today.year, claim_date__month=today.month)), 0),
            live_total=Coalesce(Sum("total", filter=live), 0),
            live_count=Count("id", filter=live),
            awaiting_total=Coalesce(Sum("total", filter=awaiting_q), 0),
            awaiting_paid=Coalesce(Sum("amount_paid", filter=awaiting_q), 0),
        )
        avg = agg["live_total"] // agg["live_count"] if agg["live_count"] else 0
        return success_response(
            "Expense claim summary retrieved.",
            data={
                "open": agg["open"],
                "month_total": agg["month_total"],
                "avg": avg,
                "awaiting": agg["awaiting_total"] - agg["awaiting_paid"],
            },
        )
