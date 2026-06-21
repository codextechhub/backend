"""Expense claims.
"""
from __future__ import annotations


from django.db import transaction
from rest_framework.exceptions import NotFound

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

class ExpenseClaimListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) expense claims for an entity.

    docstring-name: Expense claims
    """

    @property
    def rbac_permission(self):
        return "finance.expenseclaim.create" if self.request.method == "POST" \
            else "finance.expenseclaim.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = ExpenseClaim.objects.filter(entity=entity).prefetch_related("lines")
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        if (pay := request.query_params.get("payment_status")):
            qs = qs.filter(payment_status=pay)
        return success_response(
            "Expense claims retrieved.",
            data=ExpenseClaimSerializer(qs.order_by("-claim_date", "-id")[:200], many=True).data,
        )

    @transaction.atomic
    def post(self, request):
        from ..expenses import price_expense_claim

        entity = resolve_entity(request)
        body = request.data or {}
        lines = _require_lines(body)
        claim = ExpenseClaim.objects.create(
            entity=entity,
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
                tax_code=_resolve_tax(entity, ln.get("tax_code"), f"lines[{i}].tax_code"),
                cost_center=_resolve_cost_center(
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            )
        price_expense_claim(claim)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} created.",
            data=ExpenseClaimSerializer(claim).data, status=201,
        )


class _ExpenseClaimActionBase(_FinanceBase):
    def _claim(self, request, pk):
        entity = resolve_entity(request)
        claim = ExpenseClaim.objects.filter(entity=entity, pk=pk).first()
        if claim is None:
            raise NotFound("Expense claim not found for this entity.")
        return entity, claim


class ExpenseClaimDetailView(_ExpenseClaimActionBase):
    """docstring-name: Expense claims"""
    rbac_permission = "finance.expenseclaim.view"

    def get(self, request, pk):
        _, claim = self._claim(request, pk)
        return success_response(
            "Expense claim retrieved.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


class ExpenseClaimPostView(_ExpenseClaimActionBase):
    """docstring-name: Post an expense claim"""
    rbac_permission = "finance.expenseclaim.post"

    def post(self, request, pk):
        from ..expenses import post_expense_claim

        _, claim = self._claim(request, pk)
        post_expense_claim(claim, actor_user=request.user)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} posted.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


class ExpenseClaimRejectView(_ExpenseClaimActionBase):
    """POST — reject (cancel) a draft expense claim. docstring-name: Reject an expense claim"""
    rbac_permission = "finance.expenseclaim.post"  # the approver decides approve OR reject

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


class ExpenseClaimReceiptView(_ExpenseClaimActionBase):
    """POST (multipart ``file``) attach / DELETE a receipt on a claim line.

    docstring-name: Expense line receipt
    """

    rbac_permission = "finance.expenseclaim.create"

    def _line(self, request, pk, line_id):
        _, claim = self._claim(request, pk)
        line = claim.lines.filter(pk=line_id).first()
        if line is None:
            raise NotFound("Line not found on this claim.")
        return claim, line

    def post(self, request, pk, line_id):
        from rest_framework.exceptions import ValidationError

        claim, line = self._line(request, pk, line_id)
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "A receipt file is required."})
        line.receipt.save(upload.name, upload, save=True)
        claim.refresh_from_db()
        return success_response(
            "Receipt attached.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data, status=201,
        )

    def delete(self, request, pk, line_id):
        claim, line = self._line(request, pk, line_id)
        if line.receipt:
            line.receipt.delete(save=True)
        claim.refresh_from_db()
        return success_response(
            "Receipt removed.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


class ExpenseClaimSettleView(_ExpenseClaimActionBase):
    """docstring-name: Settle an expense claim"""
    rbac_permission = "finance.expenseclaim.settle"

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


