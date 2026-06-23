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
    """GET (list) / POST (create draft) budgets for an entity.

    docstring-name: Budgets
    """

    @property
    def rbac_permission(self):
        return "finance.budget.create" if self.request.method == "POST" \
            else "finance.budget.view"

    def get(self, request):
        from ..reports import budget_vs_actual

        entity = resolve_entity(request)
        qs = Budget.objects.filter(entity=entity).select_related("fiscal_year").prefetch_related("lines")
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        budgets = list(qs[:200])

        # Enrich each budget with its actual-vs-budget headline figures so the list can
        # show ACTUAL YTD / CONSUMED without the FE fanning out a variance call per row.
        data = BudgetSerializer(budgets, many=True).data
        by_id = {b.id: b for b in budgets}
        for row in data:
            budget = by_id[row["id"]]
            report = budget_vs_actual(budget)
            budgeted = report.total_budget
            actual = report.total_actual
            row["budgeted_total"] = budgeted
            row["actual_ytd"] = actual
            row["consumed_pct"] = round(actual * 100 / budgeted, 1) if budgeted else None
        return success_response("Budgets retrieved.", data=data)

    def post(self, request):
        from ..budgets import create_budget

        entity = resolve_entity(request)
        body = request.data or {}
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "A budget name is required."})
        budget = create_budget(
            entity,
            name=name,
            fiscal_year=_resolve_fiscal_year(entity, body.get("fiscal_year")),
            lines=_resolve_lines(entity, body.get("lines")),
            actor_user=request.user,
        )
        return success_response(
            f"Budget {budget.code} created.",
            data=BudgetSerializer(budget).data, status=201,
        )


def _resolve_lines(entity, raw):
    """Resolve a body ``lines`` list into service dicts (account/cost_center resolved)."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValidationError({"lines": "Expected a list of budget lines."})
    out = []
    for i, ln in enumerate(raw):
        out.append({
            "account": _resolve_account(entity, ln.get("account"), f"lines[{i}].account", required=True),
            "cost_center": _resolve_cost_center(entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            "period_no": _int(ln.get("period_no"), f"lines[{i}].period_no", required=True, minimum=1),
            "amount": _money(ln.get("amount", 0), f"lines[{i}].amount"),
        })
    return out


class _BudgetActionBase(_FinanceBase):
    def _budget(self, request, pk):
        entity = resolve_entity(request)
        budget = Budget.objects.filter(entity=entity, pk=pk).select_related("fiscal_year").first()
        if budget is None:
            raise NotFound("Budget not found for this entity.")
        return entity, budget


class BudgetDetailView(_BudgetActionBase):
    """GET one budget; PATCH to rename a draft. docstring-name: Budgets"""

    @property
    def rbac_permission(self):
        return "finance.budget.edit" if self.request.method == "PATCH" else "finance.budget.view"

    def get(self, request, pk):
        _, budget = self._budget(request, pk)
        return success_response("Budget retrieved.", data=BudgetSerializer(budget).data)

    def patch(self, request, pk):
        from ..budgets import update_budget

        _, budget = self._budget(request, pk)
        body = request.data or {}
        name = body.get("name")
        if name is not None and not str(name).strip():
            raise ValidationError({"name": "A budget name is required."})
        update_budget(budget, name=str(name).strip() if name is not None else None, actor_user=request.user)
        budget.refresh_from_db()
        return success_response("Budget updated.", data=BudgetSerializer(budget).data)


class BudgetLineCreateView(_BudgetActionBase):
    """POST one cell (upsert); PUT to replace all of a draft budget's lines.

    docstring-name: Budget lines
    """

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

    def put(self, request, pk):
        from ..budgets import set_budget_lines

        entity, budget = self._budget(request, pk)
        body = request.data or {}
        set_budget_lines(budget, _resolve_lines(entity, body.get("lines")))
        budget.refresh_from_db()
        return success_response("Budget lines saved.", data=BudgetSerializer(budget).data)


class BudgetLineDetailView(_BudgetActionBase):
    """DELETE one line from a draft budget. docstring-name: Budget lines"""

    rbac_permission = "finance.budget.edit"

    def delete(self, request, pk, line_id):
        from ..budgets import delete_budget_line

        _, budget = self._budget(request, pk)
        delete_budget_line(budget, line_id)
        budget.refresh_from_db()
        return success_response("Budget line removed.", data=BudgetSerializer(budget).data)


class BudgetApproveView(_BudgetActionBase):
    """docstring-name: Approve a budget"""
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
    """GET ?period_no — budget-vs-actual variance for the budget.

    docstring-name: Budget vs actual variance
    """

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


class BudgetHeatmapView(_BudgetActionBase):
    """GET — per-account, per-period budget-vs-actual matrix (the variance heatmap).

    Cells are bare kobo (budget/actual) to keep the 12×N grid small; the FE colours
    each by its actual/budget ratio and formats locally.

    docstring-name: Budget variance heatmap
    """

    rbac_permission = "finance.budget.view"

    def get(self, request, pk):
        from ..reports import budget_monthly_matrix

        _, budget = self._budget(request, pk)
        matrix = budget_monthly_matrix(budget)
        return success_response(
            "Budget heatmap retrieved.",
            data={
                "budget_id": matrix.budget_id,
                "fiscal_year_id": matrix.fiscal_year_id,
                "periods": matrix.periods,
                "rows": [
                    {
                        "account_id": r.account_id, "code": r.code, "name": r.name,
                        "account_type": r.account_type, "cells": r.cells,
                        "budget_total": r.budget_total, "actual_total": r.actual_total,
                    }
                    for r in matrix.rows
                ],
                "total_budget": matrix.total_budget,
                "total_actual": matrix.total_actual,
            },
        )


