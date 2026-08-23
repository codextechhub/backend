"""Period-close services - the controlled sealing of an accounting period.

Closing a period is the control that stops the past being silently rewritten. Before a
period is sealed, the close runs a **checklist** of integrity checks and posts the
auto-entries a period needs (depreciation), then transitions the period's status
through the 4-state lock (OPEN → SOFT_CLOSED → CLOSED → LOCKED).

Decoupling note: this lives in ``vs_finance`` and therefore knows only finance-native
invariants (trial balance balanced, AR sub-ledger == control, all due depreciation
posted). The AP / GR-IR reconciliations live in ``vs_procurement`` - which depends on
finance, not the other way round - so finance must never import them.

They reach the close through :func:`register_close_check`, which a dependent app calls
from its ``AppConfig.ready``. The registry inverts the dependency the same way the
workflow handlers and export datasets already do. It replaced an ``extra_checks``
argument that only a test ever passed: the seam existed, no caller used it, so in
practice *every* close ran without the payables checks and could seal a period over an
unreconciled AP position while reporting success. ``extra_checks`` is still accepted
for a caller that needs a one-off check, and the registry is applied on top of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from .audit import record
from .account_mappings import resolve_mapped_account
from .constants import (
    AssetStatus,
    AccountMappingKey,
    DocumentStatus,
    FinanceAuditAction,
    PeriodStatus,
)
from .exceptions import PeriodCloseError

#: Checks contributed by dependent apps, in registration order. Populated at startup
#: from each app's ``ready()``; see :func:`register_close_check`.
_REGISTERED_CHECKS: list = []


def register_close_check(check):
    """Register a close check contributed by an app that depends on finance.

    ``check`` is called as ``check(entity, period)`` and returns a
    :class:`ChecklistItem`, an iterable of them, or ``None`` when it has nothing to say
    for that entity. Registration is idempotent, so a module imported twice does not
    double the check.

    A check that raises is reported as a *failed* blocking item rather than being
    allowed to escape. A close is a control: a check that cannot answer is not the same
    as a check that passed, and silently dropping it would recreate the defect this
    registry exists to fix.
    """
    if check not in _REGISTERED_CHECKS:
        _REGISTERED_CHECKS.append(check)
    return check


def registered_close_checks() -> list:
    """The registered checks, for tests and diagnostics."""
    return list(_REGISTERED_CHECKS)


@dataclass
# One close checklist result.
class ChecklistItem:
    """One pre-close check: did it pass, and a human-readable detail line."""

    name: str  # Machine-readable check name.
    passed: bool  # Whether the check succeeded.
    blocking: bool = True  # Whether failure prevents close unless forced.
    detail: str = ""  # Human-readable diagnostic text.


@dataclass
# Collection of period close checks.
class CloseChecklist:
    period_id: int  # Fiscal period primary key.
    items: list = field(default_factory=list)  # Checklist items collected during validation.

    @property
    # Overall close readiness.
    def passed(self) -> bool:
        """True when no *blocking* check failed (non-blocking warnings are allowed)."""
        return all(i.passed for i in self.items if i.blocking)  # Ignore non-blocking warnings.

    @property
    # Blocking failed checks only.
    def failures(self) -> list:
        return [i for i in self.items if i.blocking and not i.passed]  # Used in close error details.


# Check whether a date falls inside a fiscal period.
def _date_in_period(period, date) -> bool:
    return period.start_date <= date <= period.end_date  # Inclusive period boundary comparison.


# Run pre-close integrity checks.
def close_checklist(entity, period, *, extra_checks=None) -> CloseChecklist:
    """Run the pre-close integrity checks for ``period`` and return the results.

    Every check registered through :func:`register_close_check` runs, plus anything
    passed in ``extra_checks`` for a one-off. ``extra_checks`` entries are zero-arg
    callables returning a :class:`ChecklistItem` (or ``(name, passed, detail)`` tuple);
    registered checks are called with ``(entity, period)``.
    """
    from .models import JournalEntry, FixedAsset
    from .reports import reconcile_ar, trial_balance

    items: list[ChecklistItem] = []  # Accumulate checklist results in display order.

    # 1. Trial balance balances (it always should - a tripwire for corruption).  # Detect GL imbalance.
    tb = trial_balance(entity, period=period)  # Compute trial balance for this period.
    items.append(ChecklistItem(  # Add trial balance check result.
        name="trial_balance_balanced", passed=tb.is_balanced,  # Pass only when debits equal credits.
        detail=f"difference {tb.difference} kobo",  # Include imbalance amount for diagnostics.
    ))

    # 2. No draft journals dated within the period (un-posted work left behind).  # Warning-level close signal.
    draft_count = JournalEntry.objects.filter(
        entity=entity, status=DocumentStatus.DRAFT,  # Scope to draft journals for this entity.
        date__gte=period.start_date, date__lte=period.end_date,  # Restrict to period dates.
    ).count()
    items.append(ChecklistItem(  # Add draft journal warning result.
        name="no_draft_journals", passed=draft_count == 0, blocking=False,  # Drafts warn but do not block.
        detail=f"{draft_count} draft journal(s) dated in period",  # Include count for the user.
    ))

    # 3. AR sub-ledger reconciles to the AR control account.  # Ensure receivables tie to GL.
    ar = reconcile_ar(entity)  # Compute AR subledger/control reconciliation.
    items.append(ChecklistItem(  # Add AR reconciliation result.
        name="ar_reconciled", passed=ar.is_reconciled,  # Pass only when subledger equals control.
        detail=f"sub-ledger {ar.subledger_total} vs control {ar.control_total} kobo",  # Include both balances.
    ))

    # 4. All due depreciation has been posted up to the period end.  # Avoid closing with missing asset expense.
    unposted = 0  # Count due depreciation charges that are still unposted.
    for asset in FixedAsset.objects.filter(entity=entity, asset_status=AssetStatus.ACTIVE):
        unposted += asset.schedule.filter(
            is_posted=False, depreciation_date__lte=period.end_date,  # Due by period end and not posted.
        ).count()
    items.append(ChecklistItem(  # Add depreciation readiness result.
        name="depreciation_posted", passed=unposted == 0,  # Pass when no due charges remain.
        detail=f"{unposted} due depreciation charge(s) not yet posted",  # Include unposted count.
    ))

    for check in (extra_checks or []):  # Run dependent-app checks injected by caller.
        result = check() if callable(check) else check  # Support callables and precomputed results.
        if isinstance(result, ChecklistItem):  # Native checklist items pass through unchanged.
            items.append(result)  # Add the supplied checklist item.
        else:  # (name, passed, detail) tuple
            name, passed, *rest = result  # Unpack tuple-style check result.
            items.append(ChecklistItem(name=name, passed=passed,
                                       detail=rest[0] if rest else ""))  # Normalize tuple to ChecklistItem.

    # Checks contributed by dependent apps (procurement's AP and GR/IR reconciliations
    # today). A check that raises fails the close rather than vanishing from it.
    for check in _REGISTERED_CHECKS:
        try:
            result = check(entity, period)
        except Exception as exc:  # noqa: BLE001 - a broken check must not pass silently.
            items.append(ChecklistItem(
                name=getattr(check, "check_name", getattr(check, "__name__", "registered_check")),
                passed=False,
                detail=f"check raised {type(exc).__name__}: {exc}",
            ))
            continue
        if result is None:
            continue
        items.extend([result] if isinstance(result, ChecklistItem) else list(result))

    return CloseChecklist(period_id=period.id, items=items)  # Return checklist summary.


@transaction.atomic
# Post due depreciation during close.
def run_period_depreciation(entity, period, *, actor_user=None):
    """Post all depreciation due on/before this period's end (a close auto-posting).

    Posts into the period even when it is SOFT_CLOSED (``allow_restricted``), which is
    exactly the privileged auto-posting the soft-close state exists for. Returns the
    number of charges posted.
    """
    from .assets import post_depreciation
    from .models import FixedAsset

    count = 0  # Count depreciation schedule rows posted by this close run.
    assets = FixedAsset.objects.filter(entity=entity, asset_status=AssetStatus.ACTIVE)
    for asset in assets:  # Check each active asset for due depreciation.
        if asset.schedule.filter(is_posted=False, depreciation_date__lte=period.end_date).exists():
            posted = post_depreciation(  # Post all due depreciation up to period end.
                asset, up_to_date=period.end_date,  # Period end is the depreciation cutoff.
                actor_user=actor_user, allow_restricted=True,  # Allow close auto-posting in restricted periods.
            )
            count += len(posted)  # Add posted schedule count.
    return count  # Return total charges posted.


# Apply and audit a period status transition.
def _transition(period, new_status, *, actor_user, action, message):
    period.status = new_status  # Set the new lifecycle status.
    fields = ["status", "updated_at"]  # Base fields changed by every transition.
    if new_status in (PeriodStatus.SOFT_CLOSED, PeriodStatus.CLOSED, PeriodStatus.LOCKED):  # Closing statuses capture actor/time.
        period.closed_at = timezone.now()
        period.closed_by = actor_user  # Store the user who closed/locked the period.
        fields += ["closed_at", "closed_by"]  # Persist close metadata too.
    period.save(update_fields=fields)
    record(  # Audit the transition.
        entity=period.entity, action=action, actor_user=actor_user, target=period,  # Entity, action, actor, target.
        message=message, target_type="FiscalPeriod",  # Human message and explicit target type.
        period=str(period), period_status=new_status,  # Structured period metadata.
    )
    return period  # Return transitioned period.


@transaction.atomic
# Handle the close period workflow.
def close_period(entity, period, *, actor_user=None, soft=False, force=False,
                 run_depreciation=True, extra_checks=None):  # Close or soft-close a fiscal period.
    """Close ``period`` after running (and optionally enforcing) the checklist.

    ``soft`` transitions OPEN → SOFT_CLOSED (auto-postings still allowed); otherwise it
    transitions OPEN/SOFT_CLOSED → CLOSED. ``run_depreciation`` posts due depreciation
    first. Blocking checklist failures raise :class:`PeriodCloseError` unless ``force``.
    Returns the period.
    """
    if period.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):  # Already sealed periods cannot be closed again.
        raise PeriodCloseError(
            f"Period '{period}' is already '{period.status}'.",
        )

    if run_depreciation:  # Close can auto-post due depreciation.
        run_period_depreciation(entity, period, actor_user=actor_user)  # Post depreciation before checklist.

    checklist = close_checklist(entity, period, extra_checks=extra_checks)  # Run close integrity checks.
    if not checklist.passed and not force:  # Blocking failures stop the close unless forced.
        failed = ", ".join(f"{i.name} ({i.detail})" for i in checklist.failures)  # Build readable failure summary.
        raise PeriodCloseError(
            f"Period '{period}' is not ready to close: {failed}.",
            failures=[i.name for i in checklist.failures],  # Provide machine-readable failed check names.
        )

    new_status = PeriodStatus.SOFT_CLOSED if soft else PeriodStatus.CLOSED  # Choose requested close strength.
    _transition(  # Apply the period status transition and audit it.
        period, new_status, actor_user=actor_user,  # Target status and actor.
        action=FinanceAuditAction.PERIOD_CLOSED,  # Audit action for close.
        message=f"Closed period to {new_status}"  # Base audit message.
                + ("" if checklist.passed else " (forced over checklist failures)"),  # Flag forced closes.
    )
    return period, checklist  # Return updated period and checklist details.


@transaction.atomic
# Re-open a closed or soft-closed period.
def reopen_period(entity, period, *, actor_user=None):
    """Re-open a CLOSED or SOFT_CLOSED period back to OPEN (audited). LOCKED can't reopen."""
    if period.status == PeriodStatus.LOCKED:  # Locked periods are irreversible.
        raise PeriodCloseError(f"Period '{period}' is LOCKED and cannot be re-opened.")
    if period.status == PeriodStatus.OPEN:  # Open periods do not need reopening.
        raise PeriodCloseError(f"Period '{period}' is already open.")
    period.status = PeriodStatus.OPEN  # Restore open lifecycle status.
    period.closed_at = None  # Clear close timestamp.
    period.closed_by = None  # Clear close actor.
    period.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    record(  # Audit the reopen.
        entity=entity, action=FinanceAuditAction.PERIOD_REOPENED,  # Audit action for reopening.
        actor_user=actor_user, target=period, target_type="FiscalPeriod",  # Actor and target context.
        message=f"Re-opened period '{period}'.", period=str(period),  # Human and structured period text.
    )
    return period  # Return reopened period.


@transaction.atomic
# Permanently lock a closed fiscal period.
def lock_period(entity, period, *, actor_user=None):
    """Permanently seal a CLOSED period (e.g. after statutory filing). Irreversible."""
    if period.status != PeriodStatus.CLOSED:  # Only fully closed periods may be locked.
        raise PeriodCloseError(
            f"Only a CLOSED period can be locked; '{period}' is '{period.status}'.",
        )
    if (
        period.end_date == period.fiscal_year.end_date
        and period.fiscal_year.status == PeriodStatus.OPEN
    ):
        raise PeriodCloseError(
            f"Close fiscal year {period.fiscal_year.year} before locking its final period.",
        )
    _transition(  # Apply irreversible lock transition and audit it.
        period, PeriodStatus.LOCKED, actor_user=actor_user,  # Target locked status and actor.
        action=FinanceAuditAction.PERIOD_LOCKED,  # Audit action for lock.
        message=f"Locked period '{period}' - permanently sealed.",  # Human-readable audit message.
    )
    return period  # Return locked period.


@transaction.atomic
# Post the year-end closing journal and seal the fiscal year.
def close_fiscal_year(entity, fiscal_year, *, actor_user=None, closing_date=None,
                      require_periods_closed=True):
    """Post the year-end closing journal and mark ``fiscal_year`` CLOSED.

    The closing entry zeroes every postable income and expense account for the year
    and rolls the net (profit or loss) into Retained Earnings (3200) - the formal
    year-end close that the live "current-year earnings" figure in the reports only
    anticipates. After it posts, the P&L accounts read zero and the year's result is
    permanently in equity.

    * ``closing_date`` defaults to the year's ``end_date``. The formal close journal
      may post into OPEN, SOFT_CLOSED or CLOSED because closing the final month before
      closing the year is the normal operator sequence. A LOCKED period remains
      immutable, so the year must be closed before the final period is locked.
    * ``require_periods_closed`` (default) refuses while any period in the year is still
      OPEN - draft/late entries should be posted and the months soft-/closed first.

    Idempotent: refuses a year already CLOSED/LOCKED. Returns ``(entry, net_income)`` -
    the closing journal (``None`` when the year had no P&L activity) and the net result
    in kobo (positive = profit).
    """
    from django.db.models import Sum  # Aggregate per-account movement.

    from .constants import (  # Enums used only here.
        AccountType, JournalSource,
    )
    from .models import Account, AccountBalance, FiscalPeriod, JournalEntry, JournalLine
    from .posting import _period_accepts_posting, post_journal, resolve_period

    if fiscal_year.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):  # Never close a year twice.
        raise PeriodCloseError(
            f"Fiscal year {fiscal_year.year} is already '{fiscal_year.status}'.")

    if require_periods_closed:  # Months must be settled before the year is sealed.
        open_count = FiscalPeriod.objects.filter(  # Count periods still fully open.
            fiscal_year=fiscal_year, status=PeriodStatus.OPEN,
        ).count()
        if open_count:  # Refuse while any month is still OPEN.
            raise PeriodCloseError(
                f"{open_count} period(s) in FY{fiscal_year.year} are still OPEN; "
                f"close or soft-close them before closing the year (or pass force).")

    # Net movement per P&L account over the year (summed across its periods' balances).
    rows = (
        AccountBalance.objects
        .filter(period__fiscal_year=fiscal_year,  # Only this year's balances.
                account__is_postable=True,  # Header accounts never take a line.
                account__account_type__in=[AccountType.INCOME, AccountType.EXPENSE])
        .values("account")
        .annotate(d=Sum("debit_total"), c=Sum("credit_total"))  # Movement per account.
    )
    net_by_account = {  # account_id → (Σdebit, Σcredit); drop already-flat accounts.
        r["account"]: (int(r["d"] or 0), int(r["c"] or 0))
        for r in rows if int(r["d"] or 0) != int(r["c"] or 0)
    }
    accounts = {a.id: a for a in Account.objects.filter(id__in=net_by_account)}  # Load once.

    closing_lines = []  # (account, debit, credit) - each line zeroes one P&L account.
    net_income = 0  # Σ(credit − debit) over P&L = revenue minus expense = profit.
    for acc_id, (d, c) in net_by_account.items():  # Build one closing line per account.
        acc = accounts[acc_id]
        if c > d:  # Net credit balance (typical revenue) → debit it flat.
            closing_lines.append((acc, c - d, 0))
        else:  # Net debit balance (typical expense / contra-revenue) → credit it flat.
            closing_lines.append((acc, 0, d - c))
        net_income += c - d  # Revenue adds; expense (d>c) subtracts.

    if not closing_lines:  # No P&L activity - seal the year with no journal.
        fiscal_year.status = PeriodStatus.CLOSED  # Mark the year closed.
        fiscal_year.save(update_fields=["status", "updated_at"])
        record(  # Audit the (empty) close.
            entity=entity, action=FinanceAuditAction.FISCAL_YEAR_CLOSED,
            actor_user=actor_user, target=fiscal_year, target_type="FiscalYear",
            message=f"Closed FY{fiscal_year.year} (no P&L activity).",
            fiscal_year=fiscal_year.year, net_income=0,
        )
        return None, 0

    # Balance the entry to Retained Earnings: a profit credits equity, a loss debits it.
    retained = resolve_mapped_account(
        entity, AccountMappingKey.RETAINED_EARNINGS, label="retained earnings",
    )
    if net_income > 0:  # Profit → credit Retained Earnings.
        closing_lines.append((retained, 0, net_income))
    else:  # Loss (or break-even handled above) → debit Retained Earnings.
        closing_lines.append((retained, -net_income, 0))

    closing_date = closing_date or fiscal_year.end_date  # Default to the last day of the year.
    period = resolve_period(entity, closing_date)  # The period the closing entry posts into.
    if not _period_accepts_posting(  # Formal close may use CLOSED, but never LOCKED.
        period, allow_restricted=True, allow_closed=True,
    ):
        raise PeriodCloseError(
            f"The closing date {closing_date} falls in a LOCKED period; "
            f"close the fiscal year before locking its final period.")

    entry = JournalEntry.objects.create(  # The year-end closing journal.
        entity=entity, date=closing_date, period=period, source=JournalSource.CLOSING,
        narration=f"Year-end close FY{fiscal_year.year}", created_by=actor_user,
    )
    for i, (acc, debit, credit) in enumerate(closing_lines, start=1):  # Write each closing line.
        JournalLine.objects.create(
            entry=entry, account=acc, debit=debit, credit=credit,
            description=f"Year-end close FY{fiscal_year.year}", line_no=i,
        )
    post_journal(  # Privileged formal-close posting; this is the only CLOSED bypass.
        entry, actor_user=actor_user, allow_restricted=True, allow_closed=True,
    )

    fiscal_year.status = PeriodStatus.CLOSED  # Seal the year.
    fiscal_year.save(update_fields=["status", "updated_at"])
    record(  # Audit the close with the net result + journal id.
        entity=entity, action=FinanceAuditAction.FISCAL_YEAR_CLOSED,
        actor_user=actor_user, target=fiscal_year, target_type="FiscalYear",
        message=f"Closed FY{fiscal_year.year}: net {net_income} kobo rolled to retained earnings.",
        journal_id=entry.pk, fiscal_year=fiscal_year.year, net_income=net_income,
    )
    return entry, net_income
