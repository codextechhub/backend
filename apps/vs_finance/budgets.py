"""Budget services - planning figures and approval locking.

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


# Guard writes to approved/locked budgets.
def _ensure_editable(budget):
    if budget.is_locked:  # Approved budgets are frozen.
        raise BudgetError(
            f"Budget '{budget.name}' is '{budget.status}' and can no longer be edited.",
        )


# Validate that a budget line targets P&L.
def _ensure_pl_account(account):
    """A budget line is only meaningful for an income/expense account - variance is
    measured against P&L movement, so balance-sheet accounts are rejected."""
    if account.account_type not in (AccountType.INCOME, AccountType.EXPENSE):  # Balance-sheet accounts are not budgeted.
        raise BudgetError(
            f"Budgets cover income and expense accounts only - "
            f"'{account.code} {account.name}' is {account.get_account_type_display()}.",
        )


def _regular_period_nos(budget):
    """Return the configured, postable period numbers for a budget's fiscal year."""
    return tuple(
        budget.fiscal_year.periods.filter(period_no__lte=12)
        .order_by("period_no")
        .values_list("period_no", flat=True)
    )


def _ensure_fiscal_period(period_no, valid_period_nos, *, field_name="period_no"):
    """Normalize ``period_no`` and require a configured regular fiscal period."""
    period_no = int(period_no)
    if period_no not in valid_period_nos:
        if valid_period_nos:
            valid = ", ".join(str(number) for number in valid_period_nos)
            detail = f"valid periods are {valid}"
        else:
            detail = "no regular fiscal periods are configured"
        raise BudgetError(
            f"{field_name} {period_no} does not exist in this fiscal year; {detail}.",
        )
    return period_no


def ensure_budget_period(budget, period_no):
    """Require and return a configured regular period number for ``budget``."""
    return _ensure_fiscal_period(period_no, _regular_period_nos(budget))


@transaction.atomic
# Create a draft budget document.
def create_budget(entity, *, name, fiscal_year, lines=None, actor_user=None):
    """Create a draft budget with an auto-allocated code, optionally with its lines.

    The code is drawn from the same tenant-level daily document sequence as other
    finance records, so every budget has a stable unique reference.
    """
    from .models import Budget
    from .numbering import next_document_number

    code = next_document_number(entity=entity, doc_type=DocType.BUDGET)
    budget = Budget.objects.create(
        entity=entity, code=code, fiscal_year=fiscal_year, name=name,  # Persist entity, document code, year, and name.
    )
    if lines:  # Optional initial line payload.
        set_budget_lines(budget, lines)  # Replace draft budget lines with the supplied lines.
    return budget  # Return the created draft budget.


@transaction.atomic
# Rename a draft budget.
def update_budget(budget, *, name=None, actor_user=None):
    """Rename a draft budget. Refuses once approved/locked."""
    _ensure_editable(budget)  # Refuse edits to locked budgets.
    if name is not None:  # Only update name when caller supplied one.
        budget.name = name  # Set the new display name.
        budget.save(update_fields=["name", "updated_at"])
    return budget  # Return the updated budget.


@transaction.atomic
# Upsert one budget cell.
def add_budget_line(budget, *, account, period_no, amount, cost_center=None):
    """Add or update one (account, cost-centre, period) cell of a draft budget.

    Refuses to mutate an approved/locked budget - that's the whole point of the lock.
    Returns the :class:`BudgetLine`.
    """
    from .models import BudgetLine

    _ensure_editable(budget)  # Refuse edits to locked budgets.
    _ensure_pl_account(account)  # Ensure account is income or expense.
    period_no = ensure_budget_period(budget, period_no)

    line, _ = BudgetLine.objects.update_or_create(
        budget=budget, account=account, cost_center=cost_center, period_no=period_no,  # Unique budget cell coordinates.
        defaults={"amount": int(amount)},  # Store amount in integer kobo.
    )
    return line  # Return the created or updated budget line.


@transaction.atomic
# Replace all draft budget lines in one operation.
def set_budget_lines(budget, lines):
    """Replace a draft budget's lines wholesale with ``lines``.

    ``lines`` is a list of dicts with resolved ``account`` (+ optional ``cost_center``),
    ``period_no`` and ``amount`` (kobo). The period must exist in the budget's fiscal
    year. Each (account, cost-centre, period) cell must be unique. Draft-only.
    """
    from .models import BudgetLine

    _ensure_editable(budget)  # Refuse edits to locked budgets.
    rows, seen = [], set()  # Collect replacement rows and duplicate keys.
    valid_period_nos = _regular_period_nos(budget)  # Read the configured calendar once.
    for i, ln in enumerate(lines):  # Validate each requested line before deleting existing rows.
        account = ln["account"]  # Caller supplies an already resolved account.
        period_no = _ensure_fiscal_period(
            ln["period_no"],
            valid_period_nos,
            field_name=f"lines[{i}].period_no",
        )
        cost_center = ln.get("cost_center")
        _ensure_pl_account(account)  # Ensure account is budgetable.
        key = (account.id, cost_center.id if cost_center else None, period_no)  # Unique cell identity.
        if key in seen:  # Duplicate cells would overwrite each other.
            raise BudgetError(
                f"Duplicate line for {account.code} / period {period_no} - "
                f"each account × cost centre × period appears once.",
            )
        seen.add(key)  # Remember this cell to catch later duplicates.
        rows.append(BudgetLine(  # Stage a new budget line for bulk insertion.
            budget=budget, account=account, cost_center=cost_center,  # Store budget, account, and optional cost center.
            period_no=period_no, amount=int(ln.get("amount", 0)),
        ))
    budget.lines.all().delete()
    if rows:  # Avoid unnecessary bulk_create call for an empty budget.
        BudgetLine.objects.bulk_create(rows)
    return budget  # Return the budget with replaced lines.


@transaction.atomic
# Remove one line from an editable budget.
def delete_budget_line(budget, line_id):
    """Remove one line from a draft budget."""
    _ensure_editable(budget)  # Refuse edits to locked budgets.
    budget.lines.filter(pk=line_id).delete()
    return budget  # Return the parent budget.


@transaction.atomic
# Delete a draft budget and audit it.
def delete_budget(budget, *, actor_user=None):
    """Delete a DRAFT budget (its lines cascade). Refuses an approved/locked budget.

    The audit row is written **before** the delete - capturing the document fields the
    record needs while the row still exists - then the budget is removed.
    """
    _ensure_editable(budget)  # Refuse deletion once locked.
    entity = budget.entity  # Capture entity before deleting the row.
    name = budget.name  # Capture name for audit message.
    fiscal_year = budget.fiscal_year.year  # Capture fiscal year for audit message.
    budget_id = budget.id  # Capture id for structured audit metadata.
    record(  # Audit before deletion while target fields still exist.
        entity=entity, action=FinanceAuditAction.BUDGET_DELETED,  # Audit action for budget deletion.
        actor_user=actor_user, target=budget,  # Actor and target context.
        message=f"Deleted budget '{name}' for FY{fiscal_year}.",  # Human-readable audit message.
        budget_id=budget_id,  # Structured id for later traceability.
    )
    budget.delete()
    return budget  # Return the now-deleted budget instance for caller context.


@transaction.atomic
# Approve and lock a draft budget.
def approve_budget(budget, *, actor_user=None):
    """Approve a draft budget, locking its lines against further edits."""
    if budget.status != BudgetStatus.DRAFT:  # Only draft budgets can enter approval.
        raise BudgetError(
            f"Budget '{budget.name}' is '{budget.status}'; only a draft can be approved.",
        )
    budget.status = BudgetStatus.APPROVED  # Move budget to approved lifecycle state.
    budget.approved_at = timezone.now()
    budget.approved_by = actor_user  # Store approving user.
    budget.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])

    record(  # Audit successful approval.
        entity=budget.entity, action=FinanceAuditAction.BUDGET_APPROVED,  # Audit action for approval.
        actor_user=actor_user, target=budget,  # Actor and target context.
        message=f"Approved budget '{budget.name}' for FY{budget.fiscal_year.year}.",  # Human-readable audit message.
        budget_id=budget.id,  # Structured budget id.
    )
    return budget  # Return the approved budget.
