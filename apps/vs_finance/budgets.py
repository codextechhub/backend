"""Budget services — planning figures and approval locking.

A budget never posts to the ledger; it is the *plan* that actuals are measured against
(see :func:`vs_finance.reports.budget_vs_actual`). The one rule with teeth here is the
**approval lock**: once a budget is approved the plan is frozen, so a disappointing
variance can't be quietly fixed by re-writing the budget after the fact.
"""
from __future__ import annotations  # Defer annotation evaluation during app import.

from django.db import transaction  # Keeps budget mutations atomic.
from django.utils import timezone  # Supplies approval timestamp.

from .audit import record  # Writes finance audit events.
from .constants import AccountType, BudgetStatus, DocType, FinanceAuditAction  # Budget enums and document type.
from .exceptions import BudgetError  # Domain error for invalid budget operations.


def _ensure_editable(budget):  # Guard writes to approved/locked budgets.
    if budget.is_locked:  # Approved budgets are frozen.
        raise BudgetError(
            f"Budget '{budget.name}' is '{budget.status}' and can no longer be edited.",
        )


def _ensure_pl_account(account):  # Validate that a budget line targets P&L.
    """A budget line is only meaningful for an income/expense account — variance is
    measured against P&L movement, so balance-sheet accounts are rejected."""
    if account.account_type not in (AccountType.INCOME, AccountType.EXPENSE):  # Balance-sheet accounts are not budgeted.
        raise BudgetError(
            f"Budgets cover income and expense accounts only — "
            f"'{account.code} {account.name}' is {account.get_account_type_display()}.",
        )


@transaction.atomic
def create_budget(entity, *, name, fiscal_year, lines=None, actor_user=None):  # Create a draft budget document.
    """Create a draft budget with an auto-allocated code, optionally with its lines.

    The code is drawn from the same per-entity document sequence as invoices/receipts
    (``CFX-<entity>-BDG-<year>-NNNNN``), so every budget has a stable unique reference.
    """
    from .models import Budget  # Local import avoids model import cycles.
    from .numbering import next_document_number  # Allocates the budget document code.

    code = next_document_number(  # Allocate a unique budget code for the fiscal year.
        entity=entity, branch=None, doc_type=DocType.BUDGET, fiscal_year=fiscal_year.year,  # Budget numbers are entity-scoped.
    )
    budget = Budget.objects.create(  # Create the draft budget header.
        entity=entity, code=code, fiscal_year=fiscal_year, name=name,  # Persist entity, document code, year, and name.
    )
    if lines:  # Optional initial line payload.
        set_budget_lines(budget, lines)  # Replace draft budget lines with the supplied lines.
    return budget  # Return the created draft budget.


@transaction.atomic
def update_budget(budget, *, name=None, actor_user=None):  # Rename a draft budget.
    """Rename a draft budget. Refuses once approved/locked."""
    _ensure_editable(budget)  # Refuse edits to locked budgets.
    if name is not None:  # Only update name when caller supplied one.
        budget.name = name  # Set the new display name.
        budget.save(update_fields=["name", "updated_at"])  # Persist only changed fields.
    return budget  # Return the updated budget.


@transaction.atomic
def add_budget_line(budget, *, account, period_no, amount, cost_center=None):  # Upsert one budget cell.
    """Add or update one (account, cost-centre, period) cell of a draft budget.

    Refuses to mutate an approved/locked budget — that's the whole point of the lock.
    Returns the :class:`BudgetLine`.
    """
    from .models import BudgetLine  # Local import avoids model import cycles.

    _ensure_editable(budget)  # Refuse edits to locked budgets.
    _ensure_pl_account(account)  # Ensure account is income or expense.
    if not (1 <= int(period_no) <= 12):  # Periods map to the twelve fiscal months.
        raise BudgetError("period_no must be between 1 and 12.")

    line, _ = BudgetLine.objects.update_or_create(  # Create or replace this account/cost-center/period amount.
        budget=budget, account=account, cost_center=cost_center, period_no=int(period_no),  # Unique budget cell coordinates.
        defaults={"amount": int(amount)},  # Store amount in integer kobo.
    )
    return line  # Return the created or updated budget line.


@transaction.atomic
def set_budget_lines(budget, lines):  # Replace all draft budget lines in one operation.
    """Replace a draft budget's lines wholesale with ``lines``.

    ``lines`` is a list of dicts with resolved ``account`` (+ optional ``cost_center``),
    ``period_no`` (1–12) and ``amount`` (kobo). Each (account, cost-centre, period) cell
    must be unique. Draft-only.
    """
    from .models import BudgetLine  # Local import avoids model import cycles.

    _ensure_editable(budget)  # Refuse edits to locked budgets.
    rows, seen = [], set()  # Collect replacement rows and duplicate keys.
    for i, ln in enumerate(lines):  # Validate each requested line before deleting existing rows.
        account = ln["account"]  # Caller supplies an already resolved account.
        period_no = int(ln["period_no"])  # Normalize period number to integer.
        cost_center = ln.get("cost_center")  # Optional analytics dimension.
        _ensure_pl_account(account)  # Ensure account is budgetable.
        if not (1 <= period_no <= 12):  # Periods map to the twelve fiscal months.
            raise BudgetError(f"lines[{i}].period_no must be between 1 and 12.")
        key = (account.id, cost_center.id if cost_center else None, period_no)  # Unique cell identity.
        if key in seen:  # Duplicate cells would overwrite each other.
            raise BudgetError(
                f"Duplicate line for {account.code} / period {period_no} — "
                f"each account × cost centre × period appears once.",
            )
        seen.add(key)  # Remember this cell to catch later duplicates.
        rows.append(BudgetLine(  # Stage a new budget line for bulk insertion.
            budget=budget, account=account, cost_center=cost_center,  # Store budget, account, and optional cost center.
            period_no=period_no, amount=int(ln.get("amount", 0)),  # Store period and integer kobo amount.
        ))
    budget.lines.all().delete()  # Clear old lines only after the replacement payload validates.
    if rows:  # Avoid unnecessary bulk_create call for an empty budget.
        BudgetLine.objects.bulk_create(rows)  # Insert replacement lines efficiently.
    return budget  # Return the budget with replaced lines.


@transaction.atomic
def delete_budget_line(budget, line_id):  # Remove one line from an editable budget.
    """Remove one line from a draft budget."""
    _ensure_editable(budget)  # Refuse edits to locked budgets.
    budget.lines.filter(pk=line_id).delete()  # Delete only a line belonging to this budget.
    return budget  # Return the parent budget.


@transaction.atomic
def delete_budget(budget, *, actor_user=None):  # Delete a draft budget and audit it.
    """Delete a DRAFT budget (its lines cascade). Refuses an approved/locked budget.

    The audit row is written **before** the delete — capturing the document fields the
    record needs while the row still exists — then the budget is removed.
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
    budget.delete()  # Delete the budget; related lines cascade.
    return budget  # Return the now-deleted budget instance for caller context.


@transaction.atomic
def approve_budget(budget, *, actor_user=None):  # Approve and lock a draft budget.
    """Approve a draft budget, locking its lines against further edits."""
    if budget.status != BudgetStatus.DRAFT:  # Only draft budgets can enter approval.
        raise BudgetError(
            f"Budget '{budget.name}' is '{budget.status}'; only a draft can be approved.",
        )
    budget.status = BudgetStatus.APPROVED  # Move budget to approved lifecycle state.
    budget.approved_at = timezone.now()  # Stamp approval time.
    budget.approved_by = actor_user  # Store approving user.
    budget.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])  # Persist approval fields.

    record(  # Audit successful approval.
        entity=budget.entity, action=FinanceAuditAction.BUDGET_APPROVED,  # Audit action for approval.
        actor_user=actor_user, target=budget,  # Actor and target context.
        message=f"Approved budget '{budget.name}' for FY{budget.fiscal_year.year}.",  # Human-readable audit message.
        budget_id=budget.id,  # Structured budget id.
    )
    return budget  # Return the approved budget.
