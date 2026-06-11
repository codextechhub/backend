"""Budgets and variance.
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response

from ..money import format_naira
from ..views import resolve_entity
from ..models import (
    Budget,
)
from ..serializers import (
    BudgetSerializer,
)


from .base import (
    _FinanceBase,
    _int,
    _money,
    _resolve_account,
    _resolve_cost_center,
    _resolve_fiscal_year,
)

# --------------------------------------------------------------------------- #
# Budgets                                                                     #
# --------------------------------------------------------------------------- #

class BudgetListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) budgets for an entity."""

    @property
    def rbac_permission(self):
        return "finance.budget.create" if self.request.method == "POST" \
            else "finance.budget.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = Budget.objects.filter(entity=entity).select_related("fiscal_year").prefetch_related("lines")
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        return success_response(
            "Budgets retrieved.", data=BudgetSerializer(qs[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "A budget name is required."})
        budget = Budget.objects.create(
            entity=entity,
            fiscal_year=_resolve_fiscal_year(entity, body.get("fiscal_year")),
            name=name,
        )
        return success_response(
            f"Budget '{name}' created.",
            data=BudgetSerializer(budget).data, status=201,
        )


class _BudgetActionBase(_FinanceBase):
    def _budget(self, request, pk):
        entity = resolve_entity(request)
        budget = Budget.objects.filter(entity=entity, pk=pk).select_related("fiscal_year").first()
        if budget is None:
            raise NotFound("Budget not found for this entity.")
        return entity, budget


class BudgetDetailView(_BudgetActionBase):
    rbac_permission = "finance.budget.view"

    def get(self, request, pk):
        _, budget = self._budget(request, pk)
        return success_response("Budget retrieved.", data=BudgetSerializer(budget).data)


class BudgetLineCreateView(_BudgetActionBase):
    """POST {account, period_no, amount, cost_center?} — add/update one budget cell."""

    rbac_permission = "finance.budget.edit"

    def post(self, request, pk):
        from ..budgets import add_budget_line

        entity, budget = self._budget(request, pk)
        body = request.data or {}
        add_budget_line(
            budget,
            account=_resolve_account(entity, body.get("account"), "account", required=True),
            period_no=_int(body.get("period_no"), "period_no", required=True, minimum=1),
            amount=_money(body.get("amount", 0), "amount"),
            cost_center=_resolve_cost_center(entity, body.get("cost_center"), "cost_center"),
        )
        budget.refresh_from_db()
        return success_response(
            "Budget line saved.", data=BudgetSerializer(budget).data, status=201,
        )


class BudgetApproveView(_BudgetActionBase):
    rbac_permission = "finance.budget.approve"

    def post(self, request, pk):
        from ..budgets import approve_budget

        _, budget = self._budget(request, pk)
        approve_budget(budget, actor_user=request.user)
        budget.refresh_from_db()
        return success_response(
            f"Budget '{budget.name}' approved and locked.",
            data=BudgetSerializer(budget).data,
        )


class BudgetVarianceView(_BudgetActionBase):
    """GET ?period_no — budget-vs-actual variance for the budget."""

    rbac_permission = "finance.budget.view"

    def get(self, request, pk):
        from ..reports import budget_vs_actual

        _, budget = self._budget(request, pk)
        period_no = _int(request.query_params.get("period_no"), "period_no", minimum=1)
        report = budget_vs_actual(budget, period_no=period_no)

        def _money_pair(amount):
            return {"kobo": amount, "naira": format_naira(amount)}

        return success_response(
            "Budget variance retrieved.",
            data={
                "budget_id": report.budget_id,
                "fiscal_year_id": report.fiscal_year_id,
                "period_no": report.period_no,
                "rows": [
                    {
                        "account_id": r.account_id, "code": r.code, "name": r.name,
                        "account_type": r.account_type,
                        "budget": _money_pair(r.budget),
                        "actual": _money_pair(r.actual),
                        "variance": _money_pair(r.variance),
                    }
                    for r in report.rows
                ],
                "total_budget": _money_pair(report.total_budget),
                "total_actual": _money_pair(report.total_actual),
                "total_variance": _money_pair(report.total_variance),
            },
        )


