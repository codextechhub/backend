"""Period-close services — the controlled sealing of an accounting period.

Closing a period is the control that stops the past being silently rewritten. Before a
period is sealed, the close runs a **checklist** of integrity checks and posts the
auto-entries a period needs (depreciation), then transitions the period's status
through the 4-state lock (OPEN → SOFT_CLOSED → CLOSED → LOCKED).

Decoupling note: this lives in ``vs_finance`` and therefore checks only finance-native
invariants (trial balance balanced, AR sub-ledger == control, all due depreciation
posted). The AP / GR-IR reconciliations live in ``vs_procurement`` — which depends on
finance, not the other way round — so a caller passes those in via ``extra_checks``
rather than finance importing procurement.
"""
from __future__ import annotations  # Defer annotation evaluation during finance app import.

from dataclasses import dataclass, field  # Lightweight containers for checklist results.

from django.db import transaction  # Keeps close/reopen/lock mutations atomic.
from django.utils import timezone  # Supplies close timestamps.

from .audit import record  # Writes finance audit events for period state changes.
from .constants import (  # Import project symbols used by this module.
    AssetStatus,  # Fixed asset lifecycle status.
    DocumentStatus,  # Journal draft/posted lifecycle status.
    FinanceAuditAction,  # Audit action enum values.
    PeriodStatus,  # Accounting period lifecycle statuses.
)  # Close the grouped expression.
from .exceptions import PeriodCloseError  # Domain error for period close failures.


@dataclass  # Apply the decorator to this callable.
class ChecklistItem:  # One close checklist result.
    """One pre-close check: did it pass, and a human-readable detail line."""

    name: str  # Machine-readable check name.
    passed: bool  # Whether the check succeeded.
    blocking: bool = True  # Whether failure prevents close unless forced.
    detail: str = ""  # Human-readable diagnostic text.


@dataclass  # Apply the decorator to this callable.
class CloseChecklist:  # Collection of period close checks.
    period_id: int  # Fiscal period primary key.
    items: list = field(default_factory=list)  # Checklist items collected during validation.

    @property  # Apply the decorator to this callable.
    def passed(self) -> bool:  # Overall close readiness.
        """True when no *blocking* check failed (non-blocking warnings are allowed)."""
        return all(i.passed for i in self.items if i.blocking)  # Ignore non-blocking warnings.

    @property  # Apply the decorator to this callable.
    def failures(self) -> list:  # Blocking failed checks only.
        return [i for i in self.items if i.blocking and not i.passed]  # Used in close error details.


def _date_in_period(period, date) -> bool:  # Check whether a date falls inside a fiscal period.
    return period.start_date <= date <= period.end_date  # Inclusive period boundary comparison.


def close_checklist(entity, period, *, extra_checks=None) -> CloseChecklist:  # Run pre-close integrity checks.
    """Run the pre-close integrity checks for ``period`` and return the results.

    ``extra_checks`` is an optional iterable of zero-arg callables returning a
    :class:`ChecklistItem` (or ``(name, passed, detail)`` tuples) — used to inject
    checks from dependent apps (e.g. procurement's AP / GR-IR reconciliations) without
    finance importing them.
    """
    from .models import JournalEntry, FixedAsset  # Local import avoids model import cycles.
    from .reports import reconcile_ar, trial_balance  # Finance report checks used by close.

    items: list[ChecklistItem] = []  # Accumulate checklist results in display order.

    # 1. Trial balance balances (it always should — a tripwire for corruption).  # Detect GL imbalance.
    tb = trial_balance(entity, period=period)  # Compute trial balance for this period.
    items.append(ChecklistItem(  # Add trial balance check result.
        name="trial_balance_balanced", passed=tb.is_balanced,  # Pass only when debits equal credits.
        detail=f"difference {tb.difference} kobo",  # Include imbalance amount for diagnostics.
    ))  # Execute the module statement.

    # 2. No draft journals dated within the period (un-posted work left behind).  # Warning-level close signal.
    draft_count = JournalEntry.objects.filter(  # Count unposted journals in the period.
        entity=entity, status=DocumentStatus.DRAFT,  # Scope to draft journals for this entity.
        date__gte=period.start_date, date__lte=period.end_date,  # Restrict to period dates.
    ).count()  # Execute the module statement.
    items.append(ChecklistItem(  # Add draft journal warning result.
        name="no_draft_journals", passed=draft_count == 0, blocking=False,  # Drafts warn but do not block.
        detail=f"{draft_count} draft journal(s) dated in period",  # Include count for the user.
    ))  # Execute the module statement.

    # 3. AR sub-ledger reconciles to the AR control account.  # Ensure receivables tie to GL.
    ar = reconcile_ar(entity)  # Compute AR subledger/control reconciliation.
    items.append(ChecklistItem(  # Add AR reconciliation result.
        name="ar_reconciled", passed=ar.is_reconciled,  # Pass only when subledger equals control.
        detail=f"sub-ledger {ar.subledger_total} vs control {ar.control_total} kobo",  # Include both balances.
    ))  # Execute the module statement.

    # 4. All due depreciation has been posted up to the period end.  # Avoid closing with missing asset expense.
    unposted = 0  # Count due depreciation charges that are still unposted.
    for asset in FixedAsset.objects.filter(entity=entity, asset_status=AssetStatus.ACTIVE):  # Check active assets only.
        unposted += asset.schedule.filter(  # Count this asset's due unposted schedule rows.
            is_posted=False, depreciation_date__lte=period.end_date,  # Due by period end and not posted.
        ).count()  # Execute the module statement.
    items.append(ChecklistItem(  # Add depreciation readiness result.
        name="depreciation_posted", passed=unposted == 0,  # Pass when no due charges remain.
        detail=f"{unposted} due depreciation charge(s) not yet posted",  # Include unposted count.
    ))  # Execute the module statement.

    for check in (extra_checks or []):  # Run dependent-app checks injected by caller.
        result = check() if callable(check) else check  # Support callables and precomputed results.
        if isinstance(result, ChecklistItem):  # Native checklist items pass through unchanged.
            items.append(result)  # Add the supplied checklist item.
        else:  # (name, passed, detail) tuple
            name, passed, *rest = result  # Unpack tuple-style check result.
            items.append(ChecklistItem(name=name, passed=passed,  # Continue the structured value.
                                       detail=rest[0] if rest else ""))  # Normalize tuple to ChecklistItem.

    return CloseChecklist(period_id=period.id, items=items)  # Return checklist summary.


@transaction.atomic  # Apply the decorator to this callable.
def run_period_depreciation(entity, period, *, actor_user=None):  # Post due depreciation during close.
    """Post all depreciation due on/before this period's end (a close auto-posting).

    Posts into the period even when it is SOFT_CLOSED (``allow_restricted``), which is
    exactly the privileged auto-posting the soft-close state exists for. Returns the
    number of charges posted.
    """
    from .assets import post_depreciation  # Local import avoids circular service imports.
    from .models import FixedAsset  # Fixed assets with depreciation schedules.

    count = 0  # Count depreciation schedule rows posted by this close run.
    assets = FixedAsset.objects.filter(entity=entity, asset_status=AssetStatus.ACTIVE)  # Active assets in this entity.
    for asset in assets:  # Check each active asset for due depreciation.
        if asset.schedule.filter(is_posted=False, depreciation_date__lte=period.end_date).exists():  # Post only when due rows exist.
            posted = post_depreciation(  # Post all due depreciation up to period end.
                asset, up_to_date=period.end_date,  # Period end is the depreciation cutoff.
                actor_user=actor_user, allow_restricted=True,  # Allow close auto-posting in restricted periods.
            )  # Close the grouped expression.
            count += len(posted)  # Add posted schedule count.
    return count  # Return total charges posted.


def _transition(period, new_status, *, actor_user, action, message):  # Apply and audit a period status transition.
    period.status = new_status  # Set the new lifecycle status.
    fields = ["status", "updated_at"]  # Base fields changed by every transition.
    if new_status in (PeriodStatus.SOFT_CLOSED, PeriodStatus.CLOSED, PeriodStatus.LOCKED):  # Closing statuses capture actor/time.
        period.closed_at = timezone.now()  # Stamp close/lock time.
        period.closed_by = actor_user  # Store the user who closed/locked the period.
        fields += ["closed_at", "closed_by"]  # Persist close metadata too.
    period.save(update_fields=fields)  # Save only changed transition fields.
    record(  # Audit the transition.
        entity=period.entity, action=action, actor_user=actor_user, target=period,  # Entity, action, actor, target.
        message=message, target_type="FiscalPeriod",  # Human message and explicit target type.
        period=str(period), period_status=new_status,  # Structured period metadata.
    )  # Close the grouped expression.
    return period  # Return transitioned period.


@transaction.atomic  # Apply the decorator to this callable.
def close_period(entity, period, *, actor_user=None, soft=False, force=False,  # Define the callable used by this module.
                 run_depreciation=True, extra_checks=None):  # Close or soft-close a fiscal period.
    """Close ``period`` after running (and optionally enforcing) the checklist.

    ``soft`` transitions OPEN → SOFT_CLOSED (auto-postings still allowed); otherwise it
    transitions OPEN/SOFT_CLOSED → CLOSED. ``run_depreciation`` posts due depreciation
    first. Blocking checklist failures raise :class:`PeriodCloseError` unless ``force``.
    Returns the period.
    """
    if period.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):  # Already sealed periods cannot be closed again.
        raise PeriodCloseError(  # Raise the domain error for this path.
            f"Period '{period}' is already '{period.status}'.",
        )  # Close the grouped expression.

    if run_depreciation:  # Close can auto-post due depreciation.
        run_period_depreciation(entity, period, actor_user=actor_user)  # Post depreciation before checklist.

    checklist = close_checklist(entity, period, extra_checks=extra_checks)  # Run close integrity checks.
    if not checklist.passed and not force:  # Blocking failures stop the close unless forced.
        failed = ", ".join(f"{i.name} ({i.detail})" for i in checklist.failures)  # Build readable failure summary.
        raise PeriodCloseError(  # Raise the domain error for this path.
            f"Period '{period}' is not ready to close: {failed}.",
            failures=[i.name for i in checklist.failures],  # Provide machine-readable failed check names.
        )  # Close the grouped expression.

    new_status = PeriodStatus.SOFT_CLOSED if soft else PeriodStatus.CLOSED  # Choose requested close strength.
    _transition(  # Apply the period status transition and audit it.
        period, new_status, actor_user=actor_user,  # Target status and actor.
        action=FinanceAuditAction.PERIOD_CLOSED,  # Audit action for close.
        message=f"Closed period to {new_status}"  # Base audit message.
                + ("" if checklist.passed else " (forced over checklist failures)"),  # Flag forced closes.
    )  # Close the grouped expression.
    return period, checklist  # Return updated period and checklist details.


@transaction.atomic  # Apply the decorator to this callable.
def reopen_period(entity, period, *, actor_user=None):  # Re-open a closed or soft-closed period.
    """Re-open a CLOSED or SOFT_CLOSED period back to OPEN (audited). LOCKED can't reopen."""
    if period.status == PeriodStatus.LOCKED:  # Locked periods are irreversible.
        raise PeriodCloseError(f"Period '{period}' is LOCKED and cannot be re-opened.")
    if period.status == PeriodStatus.OPEN:  # Open periods do not need reopening.
        raise PeriodCloseError(f"Period '{period}' is already open.")
    period.status = PeriodStatus.OPEN  # Restore open lifecycle status.
    period.closed_at = None  # Clear close timestamp.
    period.closed_by = None  # Clear close actor.
    period.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])  # Persist reopen fields.
    record(  # Audit the reopen.
        entity=entity, action=FinanceAuditAction.PERIOD_REOPENED,  # Audit action for reopening.
        actor_user=actor_user, target=period, target_type="FiscalPeriod",  # Actor and target context.
        message=f"Re-opened period '{period}'.", period=str(period),  # Human and structured period text.
    )  # Close the grouped expression.
    return period  # Return reopened period.


@transaction.atomic  # Apply the decorator to this callable.
def lock_period(entity, period, *, actor_user=None):  # Permanently lock a closed fiscal period.
    """Permanently seal a CLOSED period (e.g. after statutory filing). Irreversible."""
    if period.status != PeriodStatus.CLOSED:  # Only fully closed periods may be locked.
        raise PeriodCloseError(  # Raise the domain error for this path.
            f"Only a CLOSED period can be locked; '{period}' is '{period.status}'.",
        )  # Close the grouped expression.
    _transition(  # Apply irreversible lock transition and audit it.
        period, PeriodStatus.LOCKED, actor_user=actor_user,  # Target locked status and actor.
        action=FinanceAuditAction.PERIOD_LOCKED,  # Audit action for lock.
        message=f"Locked period '{period}' — permanently sealed.",  # Human-readable audit message.
    )  # Close the grouped expression.
    return period  # Return locked period.
