"""Branch period transitions and tenant close coordination.

Each branch closes, reopens and locks its own months. While the school keeps its
periods in order, each branch keeps that order over its own period states.
"""

from django.db import transaction
from django.utils import timezone

from .constants import FinanceAuditAction, PeriodStatus
from .exceptions import PeriodCloseError
from .models import BranchFiscalPeriod, BranchFiscalYear, FiscalPeriod, FiscalYear
from .posting import read_key_shared
from .wording import branch_words, period_label, period_status_word
from vs_tenants.models import Branch


def _at(branch) -> str:
    """" at Ikeja" for a refusal, or nothing at a school with one branch, where it recedes."""
    who = branch_words(branch)
    return f" at {who}" if who else ""


def branch_period_state(entity, period, branch):
    """Resolve one branch's period state after checking tenant ownership."""
    branch_id = getattr(branch, "pk", branch)
    owned = Branch.all_objects.filter(pk=branch_id, tenant_id=entity.tenant_id).first()
    if owned is None or period.entity_id != entity.pk or period.is_closing:
        raise PeriodCloseError("Branch period does not belong to these books.")
    state, _ = BranchFiscalPeriod.objects.get_or_create(
        period=period, branch=owned, defaults={"status": period.status},
    )
    return state


def _all_branches_closed(entity, period):
    branch_ids = set(Branch.all_objects.filter(
        tenant_id=entity.tenant_id,
    ).values_list("pk", flat=True))
    closed_ids = set(BranchFiscalPeriod.objects.filter(
        period=period, status__in=(PeriodStatus.CLOSED, PeriodStatus.LOCKED),
    ).values_list("branch_id", flat=True))
    return branch_ids == closed_ids


@transaction.atomic
def close_branch_period(entity, period, branch, *, actor_user=None, soft=False,
                        force=False, reason=None, run_depreciation=True,
                        release_deferred=True):
    """Close one branch; seal the tenant period after its last branch closes.

    While the school keeps its periods in order, the branch's own earlier periods
    must be closed first (at least soft-closed for a soft close), read from the
    branch's own states: Ikeja closing September waits only for Ikeja's August,
    never for Lekki's (:func:`vs_finance.close.refuse_out_of_order_close`). The
    refusal comes before anything posts, and ``force`` does not override it.

    Locks are taken as :func:`vs_finance.close.close_period` takes them: the year
    shared, then the earlier periods oldest first, then this period FOR UPDATE, so a
    branch reopening an earlier month waits for this close or this close for it.
    """
    from .audit import record
    from .close import (
        _transition, close_checklist, failures_sentence, lock_periods_in_date_order,
        periods_close_in_order, refuse_out_of_order_close, require_reason,
        run_period_depreciation,
    )
    from .deferred_income import release_deferred_income

    said = period_label(period, entity.tenant)
    if force:
        reason = require_reason(reason, act=f"force-close {said}")
    read_key_shared(FiscalYear, period.fiscal_year_id, ("status",))
    in_order = periods_close_in_order(entity)
    if in_order:
        lock_periods_in_date_order(entity, period, before=True)
    FiscalPeriod.objects.select_for_update().only("pk").get(pk=period.pk)
    period.refresh_from_db(fields=["status"])
    state = branch_period_state(entity, period, branch)
    state = BranchFiscalPeriod.objects.select_for_update().select_related("branch").get(pk=state.pk)
    if period.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(
            f"{said} is already {period_status_word(period.status)} at every branch.")
    if state.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(
            f"{said} is already {period_status_word(state.status)}{_at(state.branch)}.")
    if in_order:
        refuse_out_of_order_close(entity, period, soft=soft, branch=state.branch)

    if run_depreciation:
        run_period_depreciation(entity, period, actor_user=actor_user, branch=state.branch)
    if release_deferred:
        release_deferred_income(
            entity, up_to=period.end_date, actor_user=actor_user,
            allow_restricted=True, branch=state.branch,
        )
    checklist = close_checklist(entity, period, branch=state.branch)
    if not checklist.passed and not force:
        raise PeriodCloseError(
            f"{said} is not ready to close{_at(state.branch)}. "
            f"{failures_sentence(checklist.failures)}.",
            failures=[i.name for i in checklist.failures],
        )
    state.status = PeriodStatus.SOFT_CLOSED if soft else PeriodStatus.CLOSED
    state.closed_at = timezone.now()
    state.closed_by = actor_user
    state.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    record(
        entity=entity, action=FinanceAuditAction.PERIOD_CLOSED,
        actor_user=actor_user, target=period, target_type="FiscalPeriod",
        message=f"{state.branch.name} {period_status_word(state.status)} {said}.",
        branch_id=state.branch_id, period_status=state.status,
        **({"forced": True, "reason": reason} if force else {}),
    )
    if not soft and _all_branches_closed(entity, period):
        tenant_checklist = close_checklist(entity, period)
        if not tenant_checklist.passed and not force:
            raise PeriodCloseError(
                f"Every branch has closed {said}, but the school's books are not ready "
                f"to close it. {failures_sentence(tenant_checklist.failures)}.",
                failures=[i.name for i in tenant_checklist.failures],
            )
        _transition(
            period, PeriodStatus.CLOSED, actor_user=actor_user,
            action=FinanceAuditAction.PERIOD_CLOSED,
            message=f"Closed {said} after every branch closed it.",
        )
    return state, checklist


@transaction.atomic
def reopen_branch_period(entity, period, branch, *, actor_user=None, reason=None):
    """Reopen one branch and the tenant period while leaving other branches shut.

    While the school keeps its periods in order, every later period must be OPEN
    for this branch first (:func:`vs_finance.close.refuse_out_of_order_reopen`).
    The later periods are locked after this one, oldest first, as
    :func:`vs_finance.close.reopen_period` locks them.
    """
    from .audit import record
    from .close import (
        lock_periods_in_date_order, periods_close_in_order, refuse_out_of_order_reopen,
        require_reason,
    )

    said = period_label(period, entity.tenant)
    reason = require_reason(reason, act=f"reopen {said}")
    year = read_key_shared(FiscalYear, period.fiscal_year_id, ("year", "status"))
    FiscalPeriod.objects.select_for_update().only("pk").get(pk=period.pk)
    in_order = periods_close_in_order(entity)
    if in_order:
        lock_periods_in_date_order(entity, period, before=False)
    state = branch_period_state(entity, period, branch)
    state = BranchFiscalPeriod.objects.select_for_update().select_related("branch").get(pk=state.pk)
    branch_year_status = BranchFiscalYear.objects.filter(
        fiscal_year_id=period.fiscal_year_id, branch_id=state.branch_id,
    ).values_list("status", flat=True).first()
    if branch_year_status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(
            f"Reopen FY{year[0]}{_at(state.branch)} before reopening {said}.",
        )
    if year and year[1] in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(f"Reopen FY{year[0]} before reopening {said}.")
    if period.status == PeriodStatus.LOCKED:
        raise PeriodCloseError(f"{said} is locked and can never be reopened.")
    if state.status == PeriodStatus.OPEN:
        raise PeriodCloseError(f"{said} is already open{_at(state.branch)}.")
    if state.status == PeriodStatus.LOCKED:
        raise PeriodCloseError(
            f"{said} is locked{_at(state.branch)} and can never be reopened.")
    if in_order:
        refuse_out_of_order_reopen(entity, period, branch=state.branch)
    state.status = PeriodStatus.OPEN
    state.closed_at = None
    state.closed_by = None
    state.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    if period.status == PeriodStatus.CLOSED:
        period.status = PeriodStatus.OPEN
        period.closed_at = None
        period.closed_by = None
        period.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    record(
        entity=entity, action=FinanceAuditAction.PERIOD_REOPENED,
        actor_user=actor_user, target=period, target_type="FiscalPeriod",
        message=f"Reopened {said} at {state.branch.name}.",
        branch_id=state.branch_id, reason=reason,
    )
    return state


@transaction.atomic
def lock_branch_period(entity, period, branch, *, actor_user=None):
    """Permanently lock one branch's closed period.

    Every earlier period must be CLOSED or LOCKED for this branch while the school
    keeps its periods in order, for the reasons :func:`vs_finance.close.lock_period`
    gives.
    """
    from .audit import record
    from .close import refuse_out_of_order_close

    read_key_shared(FiscalYear, period.fiscal_year_id, ("status",))
    FiscalPeriod.objects.select_for_update().only("pk").get(pk=period.pk)
    state = branch_period_state(entity, period, branch)
    state = BranchFiscalPeriod.objects.select_for_update().select_related("branch").get(pk=state.pk)
    if state.status != PeriodStatus.CLOSED:
        raise PeriodCloseError(
            f"Only a closed month can be locked; {period_label(period, entity.tenant)} is "
            f"{period_status_word(state.status)}{_at(state.branch)}.")
    refuse_out_of_order_close(entity, period, branch=state.branch, act="lock")
    if period.end_date == period.fiscal_year.end_date:
        year_state = BranchFiscalYear.objects.filter(
            fiscal_year=period.fiscal_year, branch_id=state.branch_id,
        ).values_list("status", flat=True).first()
        if year_state not in (PeriodStatus.CLOSED, PeriodStatus.LOCKED) \
                and period.fiscal_year.status == PeriodStatus.OPEN:
            raise PeriodCloseError("Close this branch's fiscal year before locking its final period.")
    state.status = PeriodStatus.LOCKED
    state.save(update_fields=["status", "updated_at"])
    record(
        entity=entity, action=FinanceAuditAction.PERIOD_LOCKED,
        actor_user=actor_user, target=period, target_type="FiscalPeriod",
        message=f"{state.branch.name} locked {period_label(period, entity.tenant)} for good.",
        branch_id=state.branch_id,
    )
    branch_ids = set(Branch.all_objects.filter(
        tenant_id=entity.tenant_id,
    ).values_list("pk", flat=True))
    locked_ids = set(BranchFiscalPeriod.objects.filter(
        period=period, status=PeriodStatus.LOCKED,
    ).values_list("branch_id", flat=True))
    if branch_ids == locked_ids and period.status != PeriodStatus.LOCKED:
        from .close import _transition
        _transition(
            period, PeriodStatus.LOCKED, actor_user=actor_user,
            action=FinanceAuditAction.PERIOD_LOCKED,
            message="Locked period after every branch locked.",
        )
    return state
