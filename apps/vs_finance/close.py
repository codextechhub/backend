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


#: Checks the fiscal-year close runs before it posts anything, in registration order.
#: Populated at startup from each app's ``ready()``; see :func:`register_year_close_check`.
_REGISTERED_YEAR_CHECKS: list = []


def register_year_close_check(check):
    """Register a check that :func:`close_fiscal_year` runs before sealing a year.

    ``check`` is called as ``check(entity, fiscal_year)`` and answers the way a
    period check does (:func:`register_close_check`): a :class:`ChecklistItem`, an
    iterable of them, or ``None``. Registration is idempotent, and a check that
    raises fails the close as a blocking item.

    A year close has checks of its own because some things can only go wrong at the
    year boundary: a charge dated in a closed year can never post afterwards. Finance
    registers its own year checks here from ``AppConfig.ready`` through the same
    seam a dependent app uses, so the year close never lists them by hand.
    """
    if check not in _REGISTERED_YEAR_CHECKS:
        _REGISTERED_YEAR_CHECKS.append(check)
    return check


def registered_year_close_checks() -> list:
    """The registered year-close checks, for tests and diagnostics."""
    return list(_REGISTERED_YEAR_CHECKS)


def _run_registered_check(check, *args) -> list:
    """Run one registered check and return its items; a raising check fails, blocking."""
    try:
        result = check(*args)
    except Exception as exc:  # noqa: BLE001 - a broken check must not pass silently.
        return [ChecklistItem(
            name=getattr(check, "check_name", getattr(check, "__name__", "registered_check")),
            passed=False,
            detail=f"check raised {type(exc).__name__}: {exc}",
        )]
    if result is None:
        return []
    return [result] if isinstance(result, ChecklistItem) else list(result)


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
        items.extend(_run_registered_check(check, entity, period))

    return CloseChecklist(period_id=period.id, items=items)  # Return checklist summary.


@dataclass
class YearCloseChecklist:
    """The registered year-close checks' results for one fiscal year."""

    fiscal_year_id: int
    items: list = field(default_factory=list)

    passed = CloseChecklist.passed
    failures = CloseChecklist.failures


def year_close_checklist(entity, fiscal_year) -> YearCloseChecklist:
    """Run every registered year-close check for ``fiscal_year`` (no side effects)."""
    items: list[ChecklistItem] = []
    for check in _REGISTERED_YEAR_CHECKS:
        items.extend(_run_registered_check(check, entity, fiscal_year))
    return YearCloseChecklist(fiscal_year_id=fiscal_year.pk, items=items)


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


#: Longest reason a close override may carry; it is stored on the audit row.
REASON_MAX_LENGTH = 500


def require_reason(reason, *, act):
    """Return ``reason`` stripped, or refuse the act for want of one.

    Forcing a close over its checklist, reopening a period and reopening a year each
    undo a control. The reason is what the audit trail shows the next reader, so a
    blank one is refused as a 400 on ``reason`` rather than recorded as nothing.
    ``act`` completes the sentence ("A reason is required to <act>.").
    """
    from rest_framework.exceptions import ValidationError

    text = str(reason or "").strip()
    if not text:
        raise ValidationError({"reason": f"A reason is required to {act}."})
    if len(text) > REASON_MAX_LENGTH:
        raise ValidationError({
            "reason": f"Keep the reason to {REASON_MAX_LENGTH} characters or fewer.",
        })
    return text


# Apply and audit a period status transition.
def _transition(period, new_status, *, actor_user, action, message, **metadata):
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
        **metadata,
    )
    return period  # Return transitioned period.


@transaction.atomic
# Handle the close period workflow.
def close_period(entity, period, *, actor_user=None, soft=False, force=False,
                 run_depreciation=True, extra_checks=None, reason=None,
                 release_deferred=True):  # Close or soft-close a fiscal period.
    """Close ``period`` after running (and optionally enforcing) the checklist.

    ``soft`` transitions OPEN → SOFT_CLOSED (auto-postings still allowed); otherwise it
    transitions OPEN/SOFT_CLOSED → CLOSED. ``run_depreciation`` posts due depreciation
    first, and ``release_deferred`` releases the deferred income due by the period's
    end (:func:`vs_finance.deferred_income.release_deferred_income`), both before the
    checklist runs. Blocking checklist failures raise :class:`PeriodCloseError` unless
    ``force``. Returns the period.

    ``force`` needs a ``reason`` (:func:`require_reason`), asked for up front so a
    forced request without one is refused before depreciation posts anything. The
    reason lands on the PERIOD_CLOSED audit row beside the checklist it overrode.

    The period's row is locked FOR UPDATE for the whole close, and its status re-read
    under the lock. A posting takes KEY SHARE on the same row in its guard
    (:func:`vs_finance.posting.ensure_period_open`), so a posting in flight finishes
    before the month closes and a later one sees it closed. The year's row is
    share-locked first, the order the posting guard and the year close both use, so
    a month close and a year close cannot deadlock on each other.
    """
    from .models import FiscalPeriod, FiscalYear
    from .posting import read_key_shared

    if period.is_closing:
        raise PeriodCloseError(
            f"'{period}' is the year's closing period; it opens and closes with its "
            f"fiscal year, not on its own.")
    if force:
        reason = require_reason(reason, act=f"force-close period '{period}'")
    read_key_shared(FiscalYear, period.fiscal_year_id, ("status",))  # Year before month.
    FiscalPeriod.objects.select_for_update().only("pk").get(pk=period.pk)
    period.refresh_from_db(fields=["status"])
    if period.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):  # Already sealed periods cannot be closed again.
        raise PeriodCloseError(
            f"Period '{period}' is already '{period.status}'.",
        )

    if run_depreciation:  # Close can auto-post due depreciation.
        run_period_depreciation(entity, period, actor_user=actor_user)  # Post depreciation before checklist.
    if release_deferred:  # Income earned in the month leaves deferred income.
        from .deferred_income import release_deferred_income

        release_deferred_income(
            entity, up_to=period.end_date, actor_user=actor_user, allow_restricted=True,
        )

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
        **({"forced": True, "reason": reason} if force else {}),
    )
    return period, checklist  # Return updated period and checklist details.


@transaction.atomic
# Re-open a closed or soft-closed period.
def reopen_period(entity, period, *, actor_user=None, reason=None):
    """Re-open a CLOSED or SOFT_CLOSED period back to OPEN (audited). LOCKED can't reopen.

    A ``reason`` is required (:func:`require_reason`) and stored on the audit row.

    A month of a CLOSED or LOCKED fiscal year stays shut. The year's result is
    already in Retained Earnings, so anything posted into a reopened month of it
    would sit outside that result for good; the year has to be reopened first
    (:func:`reopen_fiscal_year`), which a LOCKED year never is.

    The year's row is share-locked and the period's locked FOR UPDATE, year first as
    every close and posting takes them, so a year close in flight finishes before
    this reads the year's status.
    """
    from .models import FiscalPeriod, FiscalYear
    from .posting import read_key_shared

    if period.is_closing:
        raise PeriodCloseError(
            f"'{period}' is the year's closing period; reopen the fiscal year instead.")
    reason = require_reason(reason, act=f"reopen period '{period}'")
    year = read_key_shared(FiscalYear, period.fiscal_year_id, ("year", "status"))
    FiscalPeriod.objects.select_for_update().only("pk").get(pk=period.pk)
    period.refresh_from_db(fields=["status"])
    if year is not None and year[1] == PeriodStatus.LOCKED:
        raise PeriodCloseError(
            f"Period '{period}' belongs to FY{year[0]}, which is LOCKED; neither the "
            f"year nor its periods can be re-opened.")
    if year is not None and year[1] == PeriodStatus.CLOSED:
        raise PeriodCloseError(
            f"Period '{period}' belongs to FY{year[0]}, which is CLOSED. Reopen the "
            f"fiscal year first, then re-open the period.")
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
        reason=reason,
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


def _closing_buckets(entity, fiscal_year):
    """The year's income and expense movement, grouped by the branch it closes under.

    Returns ``{branch_id: {account_id: (debit, credit)}}`` built from the ledger's
    journal lines, with accounts that are already flat left out and branches with
    nothing to close left out altogether. Every key is a real branch.

    An entry with no branch is filed under the tenant's only branch when it has
    exactly one. At a tenant with several, nothing says which branch's result it
    belongs to, so the close is refused and the count of such entries named. A
    tenant that owns no branch raises
    :class:`~vs_finance.exceptions.BranchlessTenantError`
    (:func:`vs_finance.branch_ledger.only_branch_id_or_several`): no closing
    journal is ever written without a branch.
    """
    from django.db.models import Sum

    from .branch_ledger import branches_in_year, ledger_lines, only_branch_id_or_several
    from .constants import AccountType

    pl_types = (AccountType.INCOME, AccountType.EXPENSE)
    only = only_branch_id_or_several(entity.tenant_id)
    shape = branches_in_year(entity, fiscal_year, account_types=pl_types)
    if shape.has_unbranched and only is None:
        count = shape.unbranched_entries
        raise PeriodCloseError(
            f"FY{fiscal_year.year} cannot close yet: {count} journal "
            f"{'entry carries' if count == 1 else 'entries carry'} income or expense "
            f"with no branch. The year is closed branch by branch, so give "
            f"{'it' if count == 1 else 'each of them'} a branch first.",
            failures=["unbranched_entries"], unbranched_entries=count,
        )

    rows = (
        ledger_lines(entity)
        .filter(entry__period__fiscal_year=fiscal_year,
                account__is_postable=True,  # Header accounts never take a line.
                account__account_type__in=pl_types)
        .values("entry__branch_id", "account_id")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    )
    buckets: dict = {}
    for row in rows:
        branch_id = row["entry__branch_id"]
        if branch_id is None:
            branch_id = only
        accounts = buckets.setdefault(branch_id, {})
        debit, credit = accounts.get(row["account_id"], (0, 0))
        accounts[row["account_id"]] = (debit + int(row["d"] or 0), credit + int(row["c"] or 0))

    closing = {}
    for branch_id, accounts in buckets.items():
        live = {acc: (d, c) for acc, (d, c) in accounts.items() if d != c}
        if live:
            closing[branch_id] = live
    return closing


def _lock_fiscal_year(fiscal_year):
    """Take the year's row lock and bring ``fiscal_year`` up to date under it.

    Closing and reopening a year each read its status and then change it; the lock
    makes two requests queue rather than both acting on the same stale status. It
    also serialises the year change with postings: the posting guard holds KEY SHARE
    on this row until its transaction commits
    (:func:`vs_finance.posting.read_key_shared`), which FOR UPDATE waits for, so no
    posting can land in the year between the close's reading of the ledger and its
    sealing of the year.
    """
    from .models import FiscalYear

    FiscalYear.objects.select_for_update().only("pk").get(pk=fiscal_year.pk)
    fiscal_year.refresh_from_db()
    return fiscal_year


@transaction.atomic
# Post the year-end closing journals and seal the fiscal year.
def close_fiscal_year(entity, fiscal_year, *, actor_user=None, closing_date=None,
                      require_periods_closed=True, reason=None):
    """Post the year-end closing journals and mark ``fiscal_year`` CLOSED.

    The year's result is worked out per branch and closed together as one act. Each
    branch with income or expense in the year gets its own closing journal, carrying
    that branch, which zeroes the branch's own income and expense accounts and rolls
    its net profit or loss into Retained Earnings (3200). All of them post in one
    transaction, so the year is either closed for every branch or for none. See
    :func:`_closing_buckets` for how an entry with no branch is treated.

    Each closing journal names the year in ``closes_fiscal_year``, which makes the
    year its owner: the journal screen offers no raw reverse, and only
    :func:`reopen_fiscal_year` undoes it.

    * The closing journals post into the year's closing period
      (:func:`vs_finance.seed.ensure_closing_period`), dated the year's last day,
      never into the last month. Every month and the full year keep their real
      income and expense; the statements of profit leave the closing period out,
      and the balance sheet and trial balance include it, so Retained Earnings
      shows the profit moved. The closing period is CLOSED, which the close's
      escape hatch covers; a LOCKED closing period refuses.
    * ``closing_date`` may be left out, or given as the year's ``end_date``. Any
      other date is refused: outside the year it would carry the close into
      another year, and inside it there is nowhere to put it but the closing period,
      which has one date.
    * ``require_periods_closed`` (default) refuses while any period in the year is still
      OPEN - draft/late entries should be posted and the months soft-/closed first.
      Passing ``False`` forces the close over OPEN months and needs a ``reason``
      (:func:`require_reason`), stored on the FISCAL_YEAR_CLOSED audit row. A forced
      close leaves those months reading OPEN; the posting guard refuses them anyway,
      because their year is closed.
    * Every SOFT_CLOSED month of the year is hard-closed as part of the close. A
      soft-closed month still takes privileged postings such as depreciation, and
      nothing may post into a closed year.
    * Every registered year-close check runs first (:func:`register_year_close_check`),
      such as depreciation dated in the year that has not posted. A blocking failure
      refuses the close unless it is forced with a reason; a forced close records the
      checks it overrode on the audit row.
    * The year's row is locked FOR UPDATE before anything is read
      (:func:`_lock_fiscal_year`), so postings into the year already in flight
      finish first and later ones see the year closed.

    Refuses a year already CLOSED/LOCKED. Returns ``(journals, net_income)``: the
    closing journals, one per branch with something to close (an empty list when
    the year had no P&L activity), and the net result across all of them in kobo
    (positive = profit).
    """
    from .constants import JournalSource
    from .models import Account, FiscalPeriod, JournalEntry, JournalLine
    from .posting import _period_accepts_posting, post_journal
    from .seed import ensure_closing_period
    from vs_tenants.models import Branch

    _lock_fiscal_year(fiscal_year)
    if fiscal_year.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):  # Never close a year twice.
        raise PeriodCloseError(
            f"Fiscal year {fiscal_year.year} is already '{fiscal_year.status}'.")

    closing_date = closing_date or fiscal_year.end_date  # Default to the last day of the year.
    if closing_date != fiscal_year.end_date:
        from rest_framework.exceptions import ValidationError

        from vs_config.display import format_date

        start = format_date(fiscal_year.start_date, entity.tenant)
        end = format_date(fiscal_year.end_date, entity.tenant)
        raise ValidationError({
            "closing_date": (
                f"The closing date must fall inside FY{fiscal_year.year} "
                f"({start} to {end}) on its last day: "
                f"the closing entry is dated {end}, in the year's closing "
                f"period. {format_date(closing_date, entity.tenant)} is not that day. "
                f"Leave it out to close on {end}."
            ),
        })

    forced = not require_periods_closed
    if forced:
        reason = require_reason(reason, act=f"force-close FY{fiscal_year.year}")
    else:  # Months must be settled before the year is sealed.
        open_count = FiscalPeriod.objects.filter(  # Count months still fully open.
            fiscal_year=fiscal_year, status=PeriodStatus.OPEN, is_closing=False,
        ).count()
        if open_count:  # Refuse while any month is still OPEN.
            raise PeriodCloseError(
                f"{open_count} period(s) in FY{fiscal_year.year} are still OPEN; "
                f"close or soft-close them before closing the year (or pass force).")

    checklist = year_close_checklist(entity, fiscal_year)
    if not checklist.passed and not forced:  # Blocking failures stop the close unless forced.
        raise PeriodCloseError(
            f"FY{fiscal_year.year} is not ready to close: "
            + " ".join(i.detail for i in checklist.failures),
            failures=[i.name for i in checklist.failures],
        )

    buckets = _closing_buckets(entity, fiscal_year)

    period = None
    if buckets:
        period = ensure_closing_period(fiscal_year)  # The closing entries' own period.
        if not _period_accepts_posting(  # Formal close may use CLOSED, but never LOCKED.
            period, allow_restricted=True, allow_closed=True,
        ):
            raise PeriodCloseError(
                f"The closing period of FY{fiscal_year.year} is LOCKED, so no closing "
                f"entry can post into it.")

    soft_closed = (
        FiscalPeriod.objects.select_for_update()
        .filter(fiscal_year=fiscal_year, status=PeriodStatus.SOFT_CLOSED, is_closing=False)
        .order_by("period_no")
    )
    for month in soft_closed:  # Nothing may post into the year after it closes.
        _transition(
            month, PeriodStatus.CLOSED, actor_user=actor_user,
            action=FinanceAuditAction.PERIOD_CLOSED,
            message=f"Closed period to {PeriodStatus.CLOSED} with the FY{fiscal_year.year} close.",
            fiscal_year=fiscal_year.year,
        )

    journals = []
    net_income = 0  # Σ(credit − debit) over P&L = revenue minus expense = profit.
    net_by_branch = {}
    if buckets:
        retained = resolve_mapped_account(
            entity, AccountMappingKey.RETAINED_EARNINGS, label="retained earnings",
        )
        account_ids = {acc for accounts in buckets.values() for acc in accounts}
        accounts = Account.objects.in_bulk(account_ids)
        branches = Branch.all_objects.in_bulk(list(buckets))
        for branch_id in sorted(buckets):
            closing_lines = []  # (account, debit, credit) - each line zeroes one P&L account.
            branch_net = 0
            for acc_id, (d, c) in sorted(buckets[branch_id].items()):
                if c > d:  # Net credit balance (typical revenue) → debit it flat.
                    closing_lines.append((accounts[acc_id], c - d, 0))
                else:  # Net debit balance (typical expense / contra-revenue) → credit it flat.
                    closing_lines.append((accounts[acc_id], 0, d - c))
                branch_net += c - d
            if branch_net > 0:  # Profit → credit Retained Earnings.
                closing_lines.append((retained, 0, branch_net))
            elif branch_net < 0:  # Loss → debit Retained Earnings.
                closing_lines.append((retained, -branch_net, 0))

            branch = branches[branch_id]
            narration = f"Year-end close FY{fiscal_year.year}"
            entry = JournalEntry.objects.create(  # One closing journal per branch.
                entity=entity, branch=branch, date=closing_date, period=period,
                source=JournalSource.CLOSING, created_by=actor_user,
                narration=f"{narration} - {branch.name}",
                closes_fiscal_year=fiscal_year,
            )
            for i, (acc, debit, credit) in enumerate(closing_lines, start=1):
                JournalLine.objects.create(
                    entry=entry, account=acc, debit=debit, credit=credit,
                    description=narration, line_no=i,
                )
            post_journal(  # Privileged formal-close posting; the only CLOSED-month bypass.
                entry, actor_user=actor_user, allow_restricted=True, allow_closed=True,
            )
            journals.append(entry)
            net_income += branch_net
            net_by_branch[str(branch_id)] = branch_net

    fiscal_year.status = PeriodStatus.CLOSED  # Seal the year.
    fiscal_year.save(update_fields=["status", "updated_at"])
    record(  # Audit the close with the net result and every closing journal.
        entity=entity, action=FinanceAuditAction.FISCAL_YEAR_CLOSED,
        actor_user=actor_user, target=fiscal_year, target_type="FiscalYear",
        message=(
            f"Closed FY{fiscal_year.year}: net {net_income} kobo rolled to retained earnings."
            if journals else f"Closed FY{fiscal_year.year} (no P&L activity)."
        ),
        journal_ids=[j.pk for j in journals], net_by_branch=net_by_branch,
        fiscal_year=fiscal_year.year, net_income=net_income,
        **({"forced": True, "reason": reason,
            "overridden_checks": [i.name for i in checklist.failures]} if forced else {}),
    )
    return journals, net_income


@transaction.atomic
# Reverse a year's closing journals and set it back to OPEN.
def reopen_fiscal_year(entity, fiscal_year, *, actor_user=None, reason=None):
    """Reopen a CLOSED fiscal year so it can be corrected and closed again.

    Every closing journal the year still has in force (one per branch) is reversed
    through :func:`vs_finance.posting.reverse_journal`, as the year's own act, on the
    closing journal's own date and in the year's closing period. The reversal
    therefore lands inside the year it undoes and outside its months: the income and
    expense accounts read their full-year totals again, Retained Earnings loses the
    result it was given, and no month's figures move. Dated today it would pour a
    whole year's income and expense into the current month.

    The year is set OPEN before the reversals post, so the posting guard admits
    them; the closing period is CLOSED, which ``allow_closed`` covers. Months keep
    their own status: re-open the one that needs a correction,
    post it, close it, and close the year again, which rolls the corrected result
    into Retained Earnings.

    Needs a ``reason`` (:func:`require_reason`), stored on the FISCAL_YEAR_REOPENED
    audit row with the journals reversed. Refuses a LOCKED year, a year that is not
    closed, and a year whose closing journal sits in a LOCKED period, where no
    reversal can post. Returns ``(fiscal_year, reversals)``.
    """
    from .constants import DocumentStatus
    from .posting import reverse_journal

    reason = require_reason(reason, act=f"reopen FY{fiscal_year.year}")
    _lock_fiscal_year(fiscal_year)
    if fiscal_year.status == PeriodStatus.LOCKED:
        raise PeriodCloseError(
            f"Fiscal year {fiscal_year.year} is LOCKED and cannot be re-opened.")
    if fiscal_year.status != PeriodStatus.CLOSED:
        raise PeriodCloseError(
            f"Fiscal year {fiscal_year.year} is '{fiscal_year.status}'; only a CLOSED "
            f"year can be re-opened.")

    journals = list(
        fiscal_year.closing_journals.filter(status=DocumentStatus.POSTED)
        .select_related("period").order_by("pk")
    )
    for journal in journals:
        if journal.period is None or journal.period.status == PeriodStatus.LOCKED:
            raise PeriodCloseError(
                f"FY{fiscal_year.year} cannot be re-opened: its closing journal "
                f"{journal.document_number or journal.pk} sits in "
                f"'{journal.period or journal.date}', which is LOCKED, so it cannot be reversed.")

    fiscal_year.status = PeriodStatus.OPEN  # Open first, so the reversals may post.
    fiscal_year.save(update_fields=["status", "updated_at"])

    reversals = [
        reverse_journal(
            journal, actor_user=actor_user, date=journal.date,
            allow_restricted=True, allow_closed=True, document_owner=fiscal_year,
        )
        for journal in journals
    ]

    record(
        entity=entity, action=FinanceAuditAction.FISCAL_YEAR_REOPENED,
        actor_user=actor_user, target=fiscal_year, target_type="FiscalYear",
        message=(
            f"Re-opened FY{fiscal_year.year}; reversed {len(reversals)} closing journal(s)."
        ),
        fiscal_year=fiscal_year.year, reason=reason,
        journal_ids=[j.pk for j in journals], reversal_ids=[r.pk for r in reversals],
    )
    return fiscal_year, reversals
