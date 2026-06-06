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
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from .audit import record
from .constants import (
    AssetStatus,
    DocumentStatus,
    FinanceAuditAction,
    PeriodStatus,
)
from .exceptions import PeriodCloseError


@dataclass
class ChecklistItem:
    """One pre-close check: did it pass, and a human-readable detail line."""

    name: str
    passed: bool
    blocking: bool = True
    detail: str = ""


@dataclass
class CloseChecklist:
    period_id: int
    items: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True when no *blocking* check failed (non-blocking warnings are allowed)."""
        return all(i.passed for i in self.items if i.blocking)

    @property
    def failures(self) -> list:
        return [i for i in self.items if i.blocking and not i.passed]


def _date_in_period(period, date) -> bool:
    return period.start_date <= date <= period.end_date


def close_checklist(entity, period, *, extra_checks=None) -> CloseChecklist:
    """Run the pre-close integrity checks for ``period`` and return the results.

    ``extra_checks`` is an optional iterable of zero-arg callables returning a
    :class:`ChecklistItem` (or ``(name, passed, detail)`` tuples) — used to inject
    checks from dependent apps (e.g. procurement's AP / GR-IR reconciliations) without
    finance importing them.
    """
    from .models import JournalEntry, FixedAsset
    from .reports import reconcile_ar, trial_balance

    items: list[ChecklistItem] = []

    # 1. Trial balance balances (it always should — a tripwire for corruption).
    tb = trial_balance(entity, period=period)
    items.append(ChecklistItem(
        name="trial_balance_balanced", passed=tb.is_balanced,
        detail=f"difference {tb.difference} kobo",
    ))

    # 2. No draft journals dated within the period (un-posted work left behind).
    draft_count = JournalEntry.objects.filter(
        entity=entity, status=DocumentStatus.DRAFT,
        date__gte=period.start_date, date__lte=period.end_date,
    ).count()
    items.append(ChecklistItem(
        name="no_draft_journals", passed=draft_count == 0, blocking=False,
        detail=f"{draft_count} draft journal(s) dated in period",
    ))

    # 3. AR sub-ledger reconciles to the AR control account.
    ar = reconcile_ar(entity)
    items.append(ChecklistItem(
        name="ar_reconciled", passed=ar.is_reconciled,
        detail=f"sub-ledger {ar.subledger_total} vs control {ar.control_total} kobo",
    ))

    # 4. All due depreciation has been posted up to the period end.
    unposted = 0
    for asset in FixedAsset.objects.filter(entity=entity, asset_status=AssetStatus.ACTIVE):
        unposted += asset.schedule.filter(
            is_posted=False, depreciation_date__lte=period.end_date,
        ).count()
    items.append(ChecklistItem(
        name="depreciation_posted", passed=unposted == 0,
        detail=f"{unposted} due depreciation charge(s) not yet posted",
    ))

    for check in (extra_checks or []):
        result = check() if callable(check) else check
        if isinstance(result, ChecklistItem):
            items.append(result)
        else:  # (name, passed, detail) tuple
            name, passed, *rest = result
            items.append(ChecklistItem(name=name, passed=passed,
                                       detail=rest[0] if rest else ""))

    return CloseChecklist(period_id=period.id, items=items)


@transaction.atomic
def run_period_depreciation(entity, period, *, actor_user=None):
    """Post all depreciation due on/before this period's end (a close auto-posting).

    Posts into the period even when it is SOFT_CLOSED (``allow_restricted``), which is
    exactly the privileged auto-posting the soft-close state exists for. Returns the
    number of charges posted.
    """
    from .assets import post_depreciation
    from .models import FixedAsset

    count = 0
    assets = FixedAsset.objects.filter(entity=entity, asset_status=AssetStatus.ACTIVE)
    for asset in assets:
        if asset.schedule.filter(is_posted=False, depreciation_date__lte=period.end_date).exists():
            posted = post_depreciation(
                asset, up_to_date=period.end_date,
                actor_user=actor_user, allow_restricted=True,
            )
            count += len(posted)
    return count


def _transition(period, new_status, *, actor_user, action, message):
    period.status = new_status
    fields = ["status", "updated_at"]
    if new_status in (PeriodStatus.SOFT_CLOSED, PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        period.closed_at = timezone.now()
        period.closed_by = actor_user
        fields += ["closed_at", "closed_by"]
    period.save(update_fields=fields)
    record(
        entity=period.entity, action=action, actor_user=actor_user, target=period,
        message=message, target_type="FiscalPeriod",
        period=str(period), period_status=new_status,
    )
    return period


@transaction.atomic
def close_period(entity, period, *, actor_user=None, soft=False, force=False,
                 run_depreciation=True, extra_checks=None):
    """Close ``period`` after running (and optionally enforcing) the checklist.

    ``soft`` transitions OPEN → SOFT_CLOSED (auto-postings still allowed); otherwise it
    transitions OPEN/SOFT_CLOSED → CLOSED. ``run_depreciation`` posts due depreciation
    first. Blocking checklist failures raise :class:`PeriodCloseError` unless ``force``.
    Returns the period.
    """
    if period.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(
            f"Period '{period}' is already '{period.status}'.",
        )

    if run_depreciation:
        run_period_depreciation(entity, period, actor_user=actor_user)

    checklist = close_checklist(entity, period, extra_checks=extra_checks)
    if not checklist.passed and not force:
        failed = ", ".join(f"{i.name} ({i.detail})" for i in checklist.failures)
        raise PeriodCloseError(
            f"Period '{period}' is not ready to close: {failed}.",
            failures=[i.name for i in checklist.failures],
        )

    new_status = PeriodStatus.SOFT_CLOSED if soft else PeriodStatus.CLOSED
    _transition(
        period, new_status, actor_user=actor_user,
        action=FinanceAuditAction.PERIOD_CLOSED,
        message=f"Closed period to {new_status}"
                + ("" if checklist.passed else " (forced over checklist failures)"),
    )
    return period, checklist


@transaction.atomic
def reopen_period(entity, period, *, actor_user=None):
    """Re-open a CLOSED or SOFT_CLOSED period back to OPEN (audited). LOCKED can't reopen."""
    if period.status == PeriodStatus.LOCKED:
        raise PeriodCloseError(f"Period '{period}' is LOCKED and cannot be re-opened.")
    if period.status == PeriodStatus.OPEN:
        raise PeriodCloseError(f"Period '{period}' is already open.")
    period.status = PeriodStatus.OPEN
    period.closed_at = None
    period.closed_by = None
    period.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    record(
        entity=entity, action=FinanceAuditAction.PERIOD_REOPENED,
        actor_user=actor_user, target=period, target_type="FiscalPeriod",
        message=f"Re-opened period '{period}'.", period=str(period),
    )
    return period


@transaction.atomic
def lock_period(entity, period, *, actor_user=None):
    """Permanently seal a CLOSED period (e.g. after statutory filing). Irreversible."""
    if period.status != PeriodStatus.CLOSED:
        raise PeriodCloseError(
            f"Only a CLOSED period can be locked; '{period}' is '{period.status}'.",
        )
    _transition(
        period, PeriodStatus.LOCKED, actor_user=actor_user,
        action=FinanceAuditAction.PERIOD_LOCKED,
        message=f"Locked period '{period}' — permanently sealed.",
    )
    return period
