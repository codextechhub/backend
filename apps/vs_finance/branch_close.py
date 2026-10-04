"""Branch period transitions and tenant close coordination."""

from django.db import transaction
from django.utils import timezone

from .constants import FinanceAuditAction, PeriodStatus
from .exceptions import PeriodCloseError
from .models import BranchFiscalPeriod, BranchFiscalYear, FiscalPeriod, FiscalYear
from .posting import read_key_shared
from vs_tenants.models import Branch


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
    """Close one branch; seal the tenant period after its last branch closes."""
    from .audit import record
    from .close import _transition, close_checklist, require_reason, run_period_depreciation
    from .deferred_income import release_deferred_income

    if force:
        reason = require_reason(reason, act=f"force-close period '{period}'")
    read_key_shared(FiscalYear, period.fiscal_year_id, ("status",))
    FiscalPeriod.objects.select_for_update().only("pk").get(pk=period.pk)
    period.refresh_from_db(fields=["status"])
    state = branch_period_state(entity, period, branch)
    state = BranchFiscalPeriod.objects.select_for_update().select_related("branch").get(pk=state.pk)
    if period.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(f"Period '{period}' is already '{period.status}'.")
    if state.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(f"Branch '{state.branch.name}' is already '{state.status}'.")

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
            f"Branch '{state.branch.name}' is not ready to close: "
            + ", ".join(i.name for i in checklist.failures),
            failures=[i.name for i in checklist.failures],
        )
    state.status = PeriodStatus.SOFT_CLOSED if soft else PeriodStatus.CLOSED
    state.closed_at = timezone.now()
    state.closed_by = actor_user
    state.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    record(
        entity=entity, action=FinanceAuditAction.PERIOD_CLOSED,
        actor_user=actor_user, target=period, target_type="FiscalPeriod",
        message=f"Closed {state.branch.name} period to {state.status}.",
        branch_id=state.branch_id, period_status=state.status,
        **({"forced": True, "reason": reason} if force else {}),
    )
    if not soft and _all_branches_closed(entity, period):
        tenant_checklist = close_checklist(entity, period)
        if not tenant_checklist.passed and not force:
            raise PeriodCloseError(
                "Tenant period is not ready to close: "
                + ", ".join(i.name for i in tenant_checklist.failures),
                failures=[i.name for i in tenant_checklist.failures],
            )
        _transition(
            period, PeriodStatus.CLOSED, actor_user=actor_user,
            action=FinanceAuditAction.PERIOD_CLOSED,
            message="Closed period after every branch closed.",
        )
    return state, checklist


@transaction.atomic
def reopen_branch_period(entity, period, branch, *, actor_user=None, reason=None):
    """Reopen one branch and the tenant period while leaving other branches shut."""
    from .audit import record
    from .close import require_reason

    reason = require_reason(reason, act=f"reopen period '{period}'")
    year = read_key_shared(FiscalYear, period.fiscal_year_id, ("year", "status"))
    FiscalPeriod.objects.select_for_update().only("pk").get(pk=period.pk)
    state = branch_period_state(entity, period, branch)
    state = BranchFiscalPeriod.objects.select_for_update().select_related("branch").get(pk=state.pk)
    branch_year_status = BranchFiscalYear.objects.filter(
        fiscal_year_id=period.fiscal_year_id, branch_id=state.branch_id,
    ).values_list("status", flat=True).first()
    if branch_year_status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(
            f"Reopen FY{year[0]} for {state.branch.name} before reopening its period.",
        )
    if year and year[1] in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(f"Reopen FY{year[0]} before reopening its branch period.")
    if period.status == PeriodStatus.LOCKED:
        raise PeriodCloseError(f"Period '{period}' is LOCKED and cannot be re-opened.")
    if state.status in (PeriodStatus.OPEN, PeriodStatus.LOCKED):
        raise PeriodCloseError(f"Branch period is '{state.status}' and cannot reopen.")
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
        message=f"Reopened {state.branch.name} period.",
        branch_id=state.branch_id, reason=reason,
    )
    return state


@transaction.atomic
def lock_branch_period(entity, period, branch, *, actor_user=None):
    """Permanently lock one branch's closed period."""
    from .audit import record

    read_key_shared(FiscalYear, period.fiscal_year_id, ("status",))
    FiscalPeriod.objects.select_for_update().only("pk").get(pk=period.pk)
    state = branch_period_state(entity, period, branch)
    state = BranchFiscalPeriod.objects.select_for_update().select_related("branch").get(pk=state.pk)
    if state.status != PeriodStatus.CLOSED:
        raise PeriodCloseError("Only a CLOSED branch period can be locked.")
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
        message=f"Locked {state.branch.name} period.", branch_id=state.branch_id,
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
