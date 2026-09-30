"""Budgets and variance.

Every budget belongs to one branch, and the school's plan is the roll-up of its
branches' plans rather than a document of its own.

* A budget is read with the transaction scope
  (:func:`vs_rbac.scoping.transaction_branch_scope`): a branch-bound reader sees
  and changes only her own branches' budgets, and one not yet given a branch
  (raised before every budget named one) only a whole-school reader sees.
* A budget is measured against its own branch's journals, so whoever can read it
  sees its actuals and variance in full.
* A new budget names its branch (:func:`vs_rbac.scoping.raised_transaction_branch`):
  a branch-bound reader's is filed to her branch, a whole-school reader at a
  school with several branches names one, and a school with one branch files it
  to that branch without asking.
* ``budgets/rollup/`` is the school total: the reader's branches' plans for a year
  summed and set against the same reader's journals
  (:func:`vs_finance.reports.budget_rollup`).
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_rbac.scoping import transaction_branch_scope

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
    _transaction_branch,
)

# --------------------------------------------------------------------------- #
# Budgets                                                                     #
# --------------------------------------------------------------------------- #

def _reader_scope(request):
    """The reader's reach over budgets: her own branches' only."""
    return transaction_branch_scope(request)


def _filing_choices(request, entity) -> dict:
    """The branches this reader may file a budget for.

    Answered by the server because the branch list a client can read is the
    school's, not the reader's; offering Lekki to the Ikeja bursar would only
    lead to a refusal on save. ``POST`` applies the same rule through
    :func:`vs_rbac.scoping.raised_transaction_branch`.
    """
    from vs_rbac.scoping import WHOLE_TENANT, caller_branch_ids
    from vs_tenants.models import Branch

    ids = caller_branch_ids(request)
    branches = Branch.objects.filter(tenant=entity.tenant)
    if ids is not WHOLE_TENANT:
        branches = branches.filter(pk__in=ids)
    return {"branches": [{"id": b.id, "name": b.name} for b in branches.order_by("name")]}


def _money_pair(amount):
    return {"kobo": amount, "naira": format_naira(amount)}


def _variance_payload(report) -> dict:
    """A variance report, one budget's or a roll-up's, as the variance screen reads it."""
    return {
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
    }


# Group endpoint behavior for Budget List Create View.
class BudgetListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) budgets for an entity.

    docstring-name: Budgets
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.budget.create" if self.request.method == "POST" \
            else "finance.budget.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from core.pagination import XVSPagination

        from ..reports import budget_vs_actual

        entity = resolve_entity(request)
        qs = _reader_scope(request).filter(
            Budget.objects.filter(entity=entity)
        ).select_related("fiscal_year", "branch", "entity__tenant").prefetch_related("lines")
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)

        # Paginate first, then run the (per-row) variance enrichment over just the page
        # so the actual-vs-budget figures cost one report per visible row, not per entity.
        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(qs.order_by("-id"), request, view=self)
        data = BudgetSerializer(page, many=True, context={"request": request}).data
        by_id = {b.id: b for b in page}
        for row in data:
            report = budget_vs_actual(by_id[row["id"]])
            budgeted, actual = report.total_budget, report.total_actual
            row["budgeted_total"] = budgeted
            row["actual_ytd"] = actual
            row["consumed_pct"] = round(actual * 100 / budgeted, 1) if budgeted else None
        response = paginator.get_paginated_response(data)
        response.data["narrowed"] = _reader_scope(request).is_narrowed
        response.data["filing"] = _filing_choices(request, entity)
        return response

    # Handle POST requests for this endpoint.
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
            lines=_resolve_lines(request, entity, body.get("lines")),
            actor_user=request.user,
            branch=_transaction_branch(request, entity, body),
        )
        return success_response(
            f"Budget {budget.code} created.",
            data=BudgetSerializer(budget, context={"request": request}).data, status=201,
        )


# Support the resolve lines workflow.
def _resolve_lines(request, entity, raw):
    """Resolve a body ``lines`` list into service dicts (account/cost_center resolved)."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValidationError({"lines": "Expected a list of budget lines."})
    out = []
    for i, ln in enumerate(raw):
        out.append({
            "account": _resolve_account(request, entity, ln.get("account"), f"lines[{i}].account", required=True),
            "cost_center": _resolve_cost_center(entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            "period_no": _int(ln.get("period_no"), f"lines[{i}].period_no", required=True, minimum=1),
            "amount": _money(ln.get("amount", 0), f"lines[{i}].amount"),
        })
    return out


# Define Budget Action Base values.
class _BudgetActionBase(_FinanceBase):
    def _budget(self, request, pk):
        """The budget, if it is in the reader's reach, for a read or a write alike.

        Another branch's budget, and one not yet given a branch, answers 404 to a
        branch-bound reader, so its existence is not confirmed. What she can reach
        is her own branches', and hers to change.
        """
        entity = resolve_entity(request)
        budget = _reader_scope(request).filter(
            Budget.objects.filter(entity=entity, pk=pk)
        ).select_related("fiscal_year", "branch", "entity__tenant").first()
        if budget is None:
            raise NotFound("Budget not found for this entity.")
        return entity, budget

    def _payload(self, request, budget):
        return BudgetSerializer(budget, context={"request": request}).data


# Group endpoint behavior for Budget Detail View.
class BudgetDetailView(_BudgetActionBase):
    """GET one budget; PATCH to rename a draft; DELETE a draft. docstring-name: Budgets"""

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        if self.request.method == "PATCH":
            return "finance.budget.edit"
        if self.request.method == "DELETE":
            return "finance.budget.delete"
        return "finance.budget.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, budget = self._budget(request, pk)
        return success_response("Budget retrieved.", data=self._payload(request, budget))

    # Handle PATCH requests for this endpoint.
    def patch(self, request, pk):
        from ..budgets import update_budget

        _, budget = self._budget(request, pk)
        body = request.data or {}
        name = body.get("name")
        if name is not None and not str(name).strip():
            raise ValidationError({"name": "A budget name is required."})
        update_budget(budget, name=str(name).strip() if name is not None else None, actor_user=request.user)
        budget.refresh_from_db()
        return success_response("Budget updated.", data=self._payload(request, budget))

    # Handle DELETE requests for this endpoint.
    def delete(self, request, pk):
        from ..budgets import delete_budget

        _, budget = self._budget(request, pk)
        delete_budget(budget, actor_user=request.user)
        return success_response("Budget deleted.", data={})


# Group endpoint behavior for Budget Line Create View.
class BudgetLineCreateView(_BudgetActionBase):
    """POST one cell (upsert); PUT to replace all of a draft budget's lines.

    docstring-name: Budget lines
    """

    rbac_permission = "finance.budget.edit"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..budgets import add_budget_line

        entity, budget = self._budget(request, pk)
        body = request.data or {}
        add_budget_line(
            budget,
            account=_resolve_account(request, entity, body.get("account"), "account", required=True),
            period_no=_int(body.get("period_no"), "period_no", required=True, minimum=1),
            amount=_money(body.get("amount", 0), "amount"),
            cost_center=_resolve_cost_center(entity, body.get("cost_center"), "cost_center"),
        )
        budget.refresh_from_db()
        return success_response(
            "Budget line saved.", data=self._payload(request, budget), status=201,
        )

    # Handle PUT requests for this endpoint.
    def put(self, request, pk):
        from ..budgets import set_budget_lines

        entity, budget = self._budget(request, pk)
        body = request.data or {}
        set_budget_lines(budget, _resolve_lines(request, entity, body.get("lines")))
        budget.refresh_from_db()
        return success_response("Budget lines saved.", data=self._payload(request, budget))


# Group endpoint behavior for Budget Line Detail View.
class BudgetLineDetailView(_BudgetActionBase):
    """DELETE one line from a draft budget. docstring-name: Budget lines"""

    rbac_permission = "finance.budget.edit"

    # Handle DELETE requests for this endpoint.
    def delete(self, request, pk, line_id):
        from ..budgets import delete_budget_line

        _, budget = self._budget(request, pk)
        delete_budget_line(budget, line_id)
        budget.refresh_from_db()
        return success_response("Budget line removed.", data=self._payload(request, budget))


# Group endpoint behavior for Budget Approve View.
class BudgetApproveView(_BudgetActionBase):
    """docstring-name: Approve a budget"""
    rbac_permission = "finance.budget.approve"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..budgets import approve_budget

        _, budget = self._budget(request, pk)
        approve_budget(budget, actor_user=request.user)
        budget.refresh_from_db()
        return success_response(
            f"Budget '{budget.name}' approved and locked.",
            data=self._payload(request, budget),
        )


# Group endpoint behavior for Budget Variance View.
class BudgetVarianceView(_BudgetActionBase):
    """GET ?period_no - budget-vs-actual variance for the budget.

    docstring-name: Budget vs actual variance
    """

    rbac_permission = "finance.budget.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        from ..reports import budget_vs_actual

        _, budget = self._budget(request, pk)
        period_no = _int(request.query_params.get("period_no"), "period_no", minimum=1)
        report = budget_vs_actual(budget, period_no=period_no)
        return success_response("Budget variance retrieved.", data=_variance_payload(report))


# Group endpoint behavior for Budget Heatmap View.
class BudgetHeatmapView(_BudgetActionBase):
    """GET - per-account, per-period budget-vs-actual matrix (the variance heatmap).

    Cells are bare kobo (budget/actual) to keep the 12×N grid small; the FE colours
    each by its actual/budget ratio and formats locally.

    docstring-name: Budget variance heatmap
    """

    rbac_permission = "finance.budget.view"

    # Handle GET requests for this endpoint.
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
                        "account_type": r.account_type,
                        "cells": r.cells,
                        "budget_total": r.budget_total,
                        "actual_total": r.actual_total,
                    }
                    for r in matrix.rows
                ],
                "total_budget": matrix.total_budget,
                "total_actual": matrix.total_actual,
            },
        )


# Group endpoint behavior for Budget Rollup View.
class BudgetRollupView(_FinanceBase):
    """GET ?fiscal_year&period_no - the school total: every plan in reach, summed.

    Each branch contributes its approved budget for the year, else its latest
    draft (:func:`vs_finance.reports.rolled_up_budgets`), and the sum is set
    against the reader's own journals. A whole-school reader gets the school's
    total; a branch-bound reader gets her own branches'. ``budgets`` lists the
    plans summed, so the screen can say what the total is made of.

    docstring-name: Budget school total
    """

    rbac_permission = "finance.budget.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from vs_config.clock import tenant_today

        from ..models import FiscalYear
        from ..reports import budget_rollup, fiscal_year_as_of

        entity = resolve_entity(request)
        if request.query_params.get("fiscal_year"):
            fiscal_year = _resolve_fiscal_year(entity, request.query_params["fiscal_year"])
        else:
            fiscal_year = (
                fiscal_year_as_of(entity, tenant_today(entity.tenant))
                or FiscalYear.objects.filter(entity=entity).order_by("-year").first()
            )
        if fiscal_year is None:
            raise ValidationError({"fiscal_year": "This entity has no fiscal year to total."})
        period_no = _int(request.query_params.get("period_no"), "period_no", minimum=1)
        scope = _reader_scope(request)
        report = budget_rollup(entity, fiscal_year, scope=scope, period_no=period_no)
        data = _variance_payload(report)
        data["fiscal_year"] = fiscal_year.year
        data["narrowed"] = scope.is_narrowed
        data["budgets"] = [
            {
                "id": b.id, "code": b.code, "name": b.name, "status": b.status,
                "branch_id": b.branch_id, "branch_name": b.branch.name if b.branch_id else None,
            }
            for b in report.budgets
        ]
        return success_response("Budget school total retrieved.", data=data)


