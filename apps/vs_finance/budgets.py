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
from .constants import AccountType, BudgetStatus, DocType, FinanceAuditAction
from .exceptions import BudgetError


def _ensure_editable(budget):
    if budget.is_locked:
        raise BudgetError(
            f"Budget '{budget.name}' is '{budget.status}' and can no longer be edited.",
        )


def _ensure_pl_account(account):
    """A budget line is only meaningful for an income/expense account — variance is
    measured against P&L movement, so balance-sheet accounts are rejected."""
    if account.account_type not in (AccountType.INCOME, AccountType.EXPENSE):
        raise BudgetError(
            f"Budgets cover income and expense accounts only — "
            f"'{account.code} {account.name}' is {account.get_account_type_display()}.",
        )


@transaction.atomic
def create_budget(entity, *, name, fiscal_year, lines=None, actor_user=None):
    """Create a draft budget with an auto-allocated code, optionally with its lines.

    The code is drawn from the same per-entity document sequence as invoices/receipts
    (``CFX-<entity>-BDG-<year>-NNNNN``), so every budget has a stable unique reference.
    """
    from .models import Budget
    from .numbering import next_document_number

    code = next_document_number(
        entity=entity, branch=None, doc_type=DocType.BUDGET, fiscal_year=fiscal_year.year,
    )
    budget = Budget.objects.create(
        entity=entity, code=code, fiscal_year=fiscal_year, name=name,
    )
    if lines:
        set_budget_lines(budget, lines)
    return budget


@transaction.atomic
def update_budget(budget, *, name=None, actor_user=None):
    """Rename a draft budget. Refuses once approved/locked."""
    _ensure_editable(budget)
    if name is not None:
        budget.name = name
        budget.save(update_fields=["name", "updated_at"])
    return budget


@transaction.atomic
def add_budget_line(budget, *, account, period_no, amount, cost_center=None):
    """Add or update one (account, cost-centre, period) cell of a draft budget.

    Refuses to mutate an approved/locked budget — that's the whole point of the lock.
    Returns the :class:`BudgetLine`.
    """
    from .models import BudgetLine

    _ensure_editable(budget)
    _ensure_pl_account(account)
    if not (1 <= int(period_no) <= 12):
        raise BudgetError("period_no must be between 1 and 12.")

    line, _ = BudgetLine.objects.update_or_create(
        budget=budget, account=account, cost_center=cost_center, period_no=int(period_no),
        defaults={"amount": int(amount)},
    )
    return line


@transaction.atomic
def set_budget_lines(budget, lines):
    """Replace a draft budget's lines wholesale with ``lines``.

    ``lines`` is a list of dicts with resolved ``account`` (+ optional ``cost_center``),
    ``period_no`` (1–12) and ``amount`` (kobo). Each (account, cost-centre, period) cell
    must be unique. Draft-only.
    """
    from .models import BudgetLine

    _ensure_editable(budget)
    rows, seen = [], set()
    for i, ln in enumerate(lines):
        account = ln["account"]
        period_no = int(ln["period_no"])
        cost_center = ln.get("cost_center")
        _ensure_pl_account(account)
        if not (1 <= period_no <= 12):
            raise BudgetError(f"lines[{i}].period_no must be between 1 and 12.")
        key = (account.id, cost_center.id if cost_center else None, period_no)
        if key in seen:
            raise BudgetError(
                f"Duplicate line for {account.code} / period {period_no} — "
                f"each account × cost centre × period appears once.",
            )
        seen.add(key)
        rows.append(BudgetLine(
            budget=budget, account=account, cost_center=cost_center,
            period_no=period_no, amount=int(ln.get("amount", 0)),
        ))
    budget.lines.all().delete()
    if rows:
        BudgetLine.objects.bulk_create(rows)
    return budget


@transaction.atomic
def delete_budget_line(budget, line_id):
    """Remove one line from a draft budget."""
    _ensure_editable(budget)
    budget.lines.filter(pk=line_id).delete()
    return budget


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
