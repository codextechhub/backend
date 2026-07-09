"""Budgets and variance.
"""
from __future__ import annotations  # Import dependency used by this finance module.


from rest_framework.exceptions import NotFound, ValidationError  # Import dependency used by this finance module.

from core.response import success_response  # Import dependency used by this finance module.

from ..money import format_naira  # Import dependency used by this finance module.
from ..views import resolve_entity  # Import dependency used by this finance module.
from ..models import (  # Import dependency used by this finance module.
    Budget,  # Finance processing step.
)  # Continue structured finance payload.
from ..serializers import (  # Import dependency used by this finance module.
    BudgetSerializer,  # Finance processing step.
)  # Continue structured finance payload.


from .base import (  # Import dependency used by this finance module.
    _FinanceBase,  # Finance processing step.
    _int,  # Finance processing step.
    _money,  # Finance processing step.
    _resolve_account,  # Finance processing step.
    _resolve_cost_center,  # Finance processing step.
    _resolve_fiscal_year,  # Finance processing step.
)  # Continue structured finance payload.

# --------------------------------------------------------------------------- #
# Budgets                                                                     #
# --------------------------------------------------------------------------- #

class BudgetListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create draft) budgets for an entity.

    docstring-name: Budgets
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.budget.create" if self.request.method == "POST" \
            else "finance.budget.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        from core.pagination import XVSPagination  # Import dependency used by this finance module.

        from ..reports import budget_vs_actual  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = Budget.objects.filter(entity=entity).select_related("fiscal_year").prefetch_related("lines")  # Query finance data from the database.
        if (status_val := request.query_params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(status=status_val)  # Store intermediate finance value.

        # Paginate first, then run the (per-row) variance enrichment over just the page
        # so the actual-vs-budget figures cost one report per visible row, not per entity.
        paginator = XVSPagination()  # Store intermediate finance value.
        paginator.page_size = 25  # Store intermediate finance value.
        page = paginator.paginate_queryset(qs.order_by("-id"), request, view=self)  # Store intermediate finance value.
        data = BudgetSerializer(page, many=True).data  # Store intermediate finance value.
        by_id = {b.id: b for b in page}  # Store intermediate finance value.
        for row in data:  # Iterate through finance records.
            budget = by_id[row["id"]]  # Store intermediate finance value.
            report = budget_vs_actual(budget)  # Store intermediate finance value.
            budgeted = report.total_budget  # Store intermediate finance value.
            actual = report.total_actual  # Store intermediate finance value.
            row["budgeted_total"] = budgeted  # Store intermediate finance value.
            row["actual_ytd"] = actual  # Store intermediate finance value.
            row["consumed_pct"] = round(actual * 100 / budgeted, 1) if budgeted else None  # Store intermediate finance value.
        return paginator.get_paginated_response(data)  # Return the computed finance response.

    def post(self, request):  # Function handles this finance operation.
        from ..budgets import create_budget  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        name = str(body.get("name", "")).strip()  # Store intermediate finance value.
        if not name:  # Branch when this finance condition is true.
            raise ValidationError({"name": "A budget name is required."})  # Surface validation or finance error.
        budget = create_budget(  # Store intermediate finance value.
            entity,  # Finance processing step.
            name=name,  # Store intermediate finance value.
            fiscal_year=_resolve_fiscal_year(entity, body.get("fiscal_year")),  # Store intermediate finance value.
            lines=_resolve_lines(entity, body.get("lines")),  # Store intermediate finance value.
            actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Budget {budget.code} created.",  # Finance processing step.
            data=BudgetSerializer(budget).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


def _resolve_lines(entity, raw):  # Function handles this finance operation.
    """Resolve a body ``lines`` list into service dicts (account/cost_center resolved)."""
    if not raw:  # Branch when this finance condition is true.
        return []  # Return the computed finance response.
    if not isinstance(raw, list):  # Branch when this finance condition is true.
        raise ValidationError({"lines": "Expected a list of budget lines."})  # Surface validation or finance error.
    out = []  # Store intermediate finance value.
    for i, ln in enumerate(raw):  # Iterate through finance records.
        out.append({  # Finance processing step.
            "account": _resolve_account(entity, ln.get("account"), f"lines[{i}].account", required=True),  # Store intermediate finance value.
            "cost_center": _resolve_cost_center(entity, ln.get("cost_center"), f"lines[{i}].cost_center"),  # Finance processing step.
            "period_no": _int(ln.get("period_no"), f"lines[{i}].period_no", required=True, minimum=1),  # Store intermediate finance value.
            "amount": _money(ln.get("amount", 0), f"lines[{i}].amount"),  # Finance processing step.
        })  # Continue structured finance payload.
    return out  # Return the computed finance response.


class _BudgetActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _budget(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        budget = Budget.objects.filter(entity=entity, pk=pk).select_related("fiscal_year").first()  # Query finance data from the database.
        if budget is None:  # Branch when this finance condition is true.
            raise NotFound("Budget not found for this entity.")  # Surface validation or finance error.
        return entity, budget  # Return the computed finance response.


class BudgetDetailView(_BudgetActionBase):  # Class groups related finance API or service behavior.
    """GET one budget; PATCH to rename a draft; DELETE a draft. docstring-name: Budgets"""

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        if self.request.method == "PATCH":  # Branch when this finance condition is true.
            return "finance.budget.edit"  # Return the computed finance response.
        if self.request.method == "DELETE":  # Branch when this finance condition is true.
            return "finance.budget.delete"  # Return the computed finance response.
        return "finance.budget.view"  # Return the computed finance response.

    def get(self, request, pk):  # Function handles this finance operation.
        _, budget = self._budget(request, pk)  # Store intermediate finance value.
        return success_response("Budget retrieved.", data=BudgetSerializer(budget).data)  # Return the computed finance response.

    def patch(self, request, pk):  # Function handles this finance operation.
        from ..budgets import update_budget  # Import dependency used by this finance module.

        _, budget = self._budget(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        name = body.get("name")  # Store intermediate finance value.
        if name is not None and not str(name).strip():  # Branch when this finance condition is true.
            raise ValidationError({"name": "A budget name is required."})  # Surface validation or finance error.
        update_budget(budget, name=str(name).strip() if name is not None else None, actor_user=request.user)  # Store intermediate finance value.
        budget.refresh_from_db()  # Finance processing step.
        return success_response("Budget updated.", data=BudgetSerializer(budget).data)  # Return the computed finance response.

    def delete(self, request, pk):  # Function handles this finance operation.
        from ..budgets import delete_budget  # Import dependency used by this finance module.

        _, budget = self._budget(request, pk)  # Store intermediate finance value.
        delete_budget(budget, actor_user=request.user)  # Store intermediate finance value.
        return success_response("Budget deleted.", data={})  # Return the computed finance response.


class BudgetLineCreateView(_BudgetActionBase):  # Class groups related finance API or service behavior.
    """POST one cell (upsert); PUT to replace all of a draft budget's lines.

    docstring-name: Budget lines
    """

    rbac_permission = "finance.budget.edit"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..budgets import add_budget_line  # Import dependency used by this finance module.

        entity, budget = self._budget(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        add_budget_line(  # Finance processing step.
            budget,  # Finance processing step.
            account=_resolve_account(entity, body.get("account"), "account", required=True),  # Store intermediate finance value.
            period_no=_int(body.get("period_no"), "period_no", required=True, minimum=1),  # Store intermediate finance value.
            amount=_money(body.get("amount", 0), "amount"),  # Store intermediate finance value.
            cost_center=_resolve_cost_center(entity, body.get("cost_center"), "cost_center"),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        budget.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Budget line saved.", data=BudgetSerializer(budget).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def put(self, request, pk):  # Function handles this finance operation.
        from ..budgets import set_budget_lines  # Import dependency used by this finance module.

        entity, budget = self._budget(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        set_budget_lines(budget, _resolve_lines(entity, body.get("lines")))  # Finance processing step.
        budget.refresh_from_db()  # Finance processing step.
        return success_response("Budget lines saved.", data=BudgetSerializer(budget).data)  # Return the computed finance response.


class BudgetLineDetailView(_BudgetActionBase):  # Class groups related finance API or service behavior.
    """DELETE one line from a draft budget. docstring-name: Budget lines"""

    rbac_permission = "finance.budget.edit"  # Store intermediate finance value.

    def delete(self, request, pk, line_id):  # Function handles this finance operation.
        from ..budgets import delete_budget_line  # Import dependency used by this finance module.

        _, budget = self._budget(request, pk)  # Store intermediate finance value.
        delete_budget_line(budget, line_id)  # Finance processing step.
        budget.refresh_from_db()  # Finance processing step.
        return success_response("Budget line removed.", data=BudgetSerializer(budget).data)  # Return the computed finance response.


class BudgetApproveView(_BudgetActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Approve a budget"""
    rbac_permission = "finance.budget.approve"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..budgets import approve_budget  # Import dependency used by this finance module.

        _, budget = self._budget(request, pk)  # Store intermediate finance value.
        approve_budget(budget, actor_user=request.user)  # Store intermediate finance value.
        budget.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Budget '{budget.name}' approved and locked.",  # Finance processing step.
            data=BudgetSerializer(budget).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class BudgetVarianceView(_BudgetActionBase):  # Class groups related finance API or service behavior.
    """GET ?period_no — budget-vs-actual variance for the budget.

    docstring-name: Budget vs actual variance
    """

    rbac_permission = "finance.budget.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        from ..reports import budget_vs_actual  # Import dependency used by this finance module.

        _, budget = self._budget(request, pk)  # Store intermediate finance value.
        period_no = _int(request.query_params.get("period_no"), "period_no", minimum=1)  # Store intermediate finance value.
        report = budget_vs_actual(budget, period_no=period_no)  # Store intermediate finance value.

        def _money_pair(amount):  # Function handles this finance operation.
            return {"kobo": amount, "naira": format_naira(amount)}  # Return the computed finance response.

        return success_response(  # Return the computed finance response.
            "Budget variance retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "budget_id": report.budget_id,  # Finance processing step.
                "fiscal_year_id": report.fiscal_year_id,  # Finance processing step.
                "period_no": report.period_no,  # Finance processing step.
                "rows": [  # Finance processing step.
                    {  # Continue structured finance payload.
                        "account_id": r.account_id, "code": r.code, "name": r.name,  # Finance processing step.
                        "account_type": r.account_type,  # Finance processing step.
                        "budget": _money_pair(r.budget),  # Finance processing step.
                        "actual": _money_pair(r.actual),  # Finance processing step.
                        "variance": _money_pair(r.variance),  # Finance processing step.
                    }  # Continue structured finance payload.
                    for r in report.rows  # Iterate through finance records.
                ],  # Continue structured finance payload.
                "total_budget": _money_pair(report.total_budget),  # Finance processing step.
                "total_actual": _money_pair(report.total_actual),  # Finance processing step.
                "total_variance": _money_pair(report.total_variance),  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class BudgetHeatmapView(_BudgetActionBase):  # Class groups related finance API or service behavior.
    """GET — per-account, per-period budget-vs-actual matrix (the variance heatmap).

    Cells are bare kobo (budget/actual) to keep the 12×N grid small; the FE colours
    each by its actual/budget ratio and formats locally.

    docstring-name: Budget variance heatmap
    """

    rbac_permission = "finance.budget.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        from ..reports import budget_monthly_matrix  # Import dependency used by this finance module.

        _, budget = self._budget(request, pk)  # Store intermediate finance value.
        matrix = budget_monthly_matrix(budget)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Budget heatmap retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "budget_id": matrix.budget_id,  # Finance processing step.
                "fiscal_year_id": matrix.fiscal_year_id,  # Finance processing step.
                "periods": matrix.periods,  # Finance processing step.
                "rows": [  # Finance processing step.
                    {  # Continue structured finance payload.
                        "account_id": r.account_id, "code": r.code, "name": r.name,  # Finance processing step.
                        "account_type": r.account_type, "cells": r.cells,  # Finance processing step.
                        "budget_total": r.budget_total, "actual_total": r.actual_total,  # Finance processing step.
                    }  # Continue structured finance payload.
                    for r in matrix.rows  # Iterate through finance records.
                ],  # Continue structured finance payload.
                "total_budget": matrix.total_budget,  # Finance processing step.
                "total_actual": matrix.total_actual,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


