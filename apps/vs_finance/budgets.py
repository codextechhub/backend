"""Budget services — planning figures and approval locking.

A budget never posts to the ledger; it is the *plan* that actuals are measured against
(see :func:`vs_finance.reports.budget_vs_actual`). The one rule with teeth here is the
**approval lock**: once a budget is approved the plan is frozen, so a disappointing
variance can't be quietly fixed by re-writing the budget after the fact.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .audit import record
from .constants import BudgetStatus, FinanceAuditAction
from .exceptions import BudgetError


@transaction.atomic
def add_budget_line(budget, *, account, period_no, amount, cost_center=None):
    """Add or update one (account, cost-centre, period) cell of a draft budget.

    Refuses to mutate an approved/locked budget — that's the whole point of the lock.
    Returns the :class:`BudgetLine`.
    """
    from .models import BudgetLine

    if budget.is_locked:
        raise BudgetError(
            f"Budget '{budget.name}' is '{budget.status}' and can no longer be edited.",
        )
    if not (1 <= int(period_no) <= 12):
        raise BudgetError("period_no must be between 1 and 12.")

    line, _ = BudgetLine.objects.update_or_create(
        budget=budget, account=account, cost_center=cost_center, period_no=int(period_no),
        defaults={"amount": int(amount)},
    )
    return line


@transaction.atomic
def approve_budget(budget, *, actor_user=None):
    """Approve a draft budget, locking its lines against further edits."""
    if budget.status != BudgetStatus.DRAFT:
        raise BudgetError(
            f"Budget '{budget.name}' is '{budget.status}'; only a draft can be approved.",
        )
    budget.status = BudgetStatus.APPROVED
    budget.approved_at = timezone.now()
    budget.approved_by = actor_user
    budget.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])

    record(
        entity=budget.entity, action=FinanceAuditAction.BUDGET_APPROVED,
        actor_user=actor_user, target=budget,
        message=f"Approved budget '{budget.name}' for FY{budget.fiscal_year.year}.",
        budget_id=budget.id,
    )
    return budget
