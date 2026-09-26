"""Budgets and variance.

A budget is either the school's plan (no branch) or one branch's plan. Readers
see the plans in their reach: their own branches' and the school's.

* A branch plan is measured against that branch's own journals, and whoever can
  see it sees its actuals and variance.
* The school's plan is measured against the whole ledger. A branch-bound reader
  sees that plan but not its actuals: the school's actuals would show them other
  branches' money, and their own against the whole plan would read as a
  shortfall that is only the other branches' share. For them those figures are
  ``None``, and variance rows for accounts the plan does not cover are left out,
  since their presence alone reports other branches' postings.
* A branch-bound reader may change only their own branches' plans; the school's
  plan is read-only to them, and ``can_manage`` on each budget says which. What
  they create is filed to their branch. A school-wide reader names a branch, or
  none for the school's plan.
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import SAFE_METHODS

from core.response import success_response
from vs_rbac.scoping import assert_caller_may_change, branch_scope, raised_branch

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

def _reader_scope(request):
    """The reader's reach over budgets: their branches' plans and the school's."""
    return branch_scope(request, include_shared=True)


def _withholds_actuals(request, budget) -> bool:
    """True when this reader sees only ``budget``'s plan; see the module docstring."""
    return budget.branch_id is None and _reader_scope(request).is_narrowed


def _filing_choices(request, entity) -> dict:
    """Whose plan this reader may create: the school's, and which branches'.

    Answered by the server because the branch list a client can read is the
    school's, not the reader's; offering Lekki to the Ikeja bursar would only
    lead to a refusal on save. ``POST`` applies the same rule through
    :func:`vs_rbac.scoping.raised_branch`.
    """
    from vs_rbac.scoping import WHOLE_TENANT, caller_branch_ids
    from vs_tenants.models import Branch

    ids = caller_branch_ids(request)
    branches = Branch.objects.filter(tenant=entity.tenant)
    if ids is not WHOLE_TENANT:
        branches = branches.filter(pk__in=ids)
    return {
        "school": ids is WHOLE_TENANT,
        "branches": [{"id": b.id, "name": b.name} for b in branches.order_by("name")],
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
            budget = by_id[row["id"]]
            report = budget_vs_actual(budget)
            budgeted = report.total_budget
            actual = report.total_actual
            hidden = _withholds_actuals(request, budget)
            row["budgeted_total"] = budgeted
            row["actual_ytd"] = None if hidden else actual
            row["consumed_pct"] = (
                None if hidden or not budgeted else round(actual * 100 / budgeted, 1)
            )
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
            lines=_resolve_lines(entity, body.get("lines")),
            actor_user=request.user,
            branch=raised_branch(request, entity.tenant, body),
        )
        return success_response(
            f"Budget {budget.code} created.",
            data=BudgetSerializer(budget, context={"request": request}).data, status=201,
        )


# Support the resolve lines workflow.
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


# Define Budget Action Base values.
class _BudgetActionBase(_FinanceBase):
    def _budget(self, request, pk):
        """The budget, if it is in the reader's reach, and theirs to change on a write.

        Another branch's plan answers 404, so its existence is not confirmed. A
        write to the school's plan by a branch-bound reader is refused here, once,
        for every write endpoint below.
        """
        entity = resolve_entity(request)
        budget = _reader_scope(request).filter(
            Budget.objects.filter(entity=entity, pk=pk)
        ).select_related("fiscal_year", "branch", "entity__tenant").first()
        if budget is None:
            raise NotFound("Budget not found for this entity.")
        if request.method not in SAFE_METHODS:
            assert_caller_may_change(
                request.user, entity.tenant, [budget.branch_id],
                message="This is the school's budget. Your branch can read it but not change it.",
            )
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
            account=_resolve_account(entity, body.get("account"), "account", required=True),
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
        set_budget_lines(budget, _resolve_lines(entity, body.get("lines")))
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
        narrowed = _withholds_actuals(request, budget)

        # Support the money pair workflow.
        def _money_pair(amount):
            if narrowed:
                return None
            return {"kobo": amount, "naira": format_naira(amount)}

        return success_response(
            "Budget variance retrieved.",
            data={
                "budget_id": report.budget_id,
                "fiscal_year_id": report.fiscal_year_id,
                "period_no": report.period_no,
                "narrowed": narrowed,
                "rows": [
                    {
                        "account_id": r.account_id, "code": r.code, "name": r.name,
                        "account_type": r.account_type,
                        "budget": {"kobo": r.budget, "naira": format_naira(r.budget)},
                        "actual": _money_pair(r.actual),
                        "variance": _money_pair(r.variance),
                    }
                    for r in report.rows
                    if not narrowed or r.budget
                ],
                "total_budget": {"kobo": report.total_budget, "naira": format_naira(report.total_budget)},
                "total_actual": _money_pair(report.total_actual),
                "total_variance": _money_pair(report.total_variance),
            },
        )


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
        narrowed = _withholds_actuals(request, budget)
        return success_response(
            "Budget heatmap retrieved.",
            data={
                "budget_id": matrix.budget_id,
                "fiscal_year_id": matrix.fiscal_year_id,
                "periods": matrix.periods,
                "narrowed": narrowed,
                "rows": [
                    {
                        "account_id": r.account_id, "code": r.code, "name": r.name,
                        "account_type": r.account_type,
                        "cells": [
                            {**cell, "actual": None} for cell in r.cells
                        ] if narrowed else r.cells,
                        "budget_total": r.budget_total,
                        "actual_total": None if narrowed else r.actual_total,
                    }
                    for r in matrix.rows
                    if not narrowed or r.budget_total
                ],
                "total_budget": matrix.total_budget,
                "total_actual": None if narrowed else matrix.total_actual,
            },
        )


