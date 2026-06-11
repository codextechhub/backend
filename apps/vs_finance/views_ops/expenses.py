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
    """GET (list) / POST (create draft) expense claims for an entity."""

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
    rbac_permission = "finance.expenseclaim.view"

    def get(self, request, pk):
        _, claim = self._claim(request, pk)
        return success_response(
            "Expense claim retrieved.", data=ExpenseClaimSerializer(claim).data,
        )


class ExpenseClaimPostView(_ExpenseClaimActionBase):
    rbac_permission = "finance.expenseclaim.post"

    def post(self, request, pk):
        from ..expenses import post_expense_claim

        _, claim = self._claim(request, pk)
        post_expense_claim(claim, actor_user=request.user)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} posted.",
            data=ExpenseClaimSerializer(claim).data,
        )


class ExpenseClaimSettleView(_ExpenseClaimActionBase):
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
            data=ExpenseClaimSerializer(claim).data,
        )


