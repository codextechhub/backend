"""
Approver resolution — builds the eligible approver list for a stage at activation time.

The list is frozen into WorkflowStageApprover rows the moment a stage activates.
All subsequent eligibility checks read that snapshot rather than re-querying RBAC
live, so mid-workflow permission changes don't retroactively affect who can vote.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

from django.db.models import Q
from django.utils import timezone

from vs_workflow.constants import ApproverScope, ApproverSource, OrganogramTarget
from vs_workflow.models import ApprovalDelegation, WorkflowInstance, WorkflowStage

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser


# Store one eligible actor and any delegation context for the stage snapshot.
@dataclass
class EligibleApprover:
    """Carries one resolved approver and, when delegation is active, who they act for.

    on_behalf_of is set when the approver was added via an ApprovalDelegation row
    rather than holding the permission themselves. It is stored in the
    WorkflowStageApprover snapshot so the audit trail shows both names.
    """
    user: AbstractBaseUser
    on_behalf_of: Optional[AbstractBaseUser] = None


# Resolve RBAC permission holders for a stage.
def _users_with_permission(school, branch, permission_key: str, scope: ApproverScope):
    """Resolve the set of users holding permission_key in the given scope via vs_rbac.

    This is the single integration boundary between the workflow engine and the
    RBAC system. If vs_rbac is unavailable (e.g. a standalone install) it falls
    back to all active users in the school, so the engine degrades gracefully
    rather than breaking. Scope controls which school/branch args are forwarded:
    PLATFORM passes both as None, SCHOOL passes school only, BRANCH passes both.
    """
    try:
        from vs_rbac.evaluator import resolve_users_with_permission
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "vs_rbac not available; returning unscoped user set. Connect vs_rbac.")
        from django.contrib.auth import get_user_model
        UserModel = get_user_model()
        qs = UserModel.objects.filter(is_active=True)
        if school is not None and hasattr(UserModel, "school"):
            qs = qs.filter(school=school)
        return qs

    if scope == ApproverScope.PLATFORM:
        # Platform approvers are global; school and branch scope are intentionally removed.
        school_arg, branch_arg = None, None
    elif scope == ApproverScope.BRANCH:
        school_arg, branch_arg = school, branch
    else:  # SCHOOL
        school_arg, branch_arg = school, None
    return resolve_users_with_permission(
        school=school_arg, branch=branch_arg, permission_key=permission_key,
    )


# Resolve organogram-based approvers relative to the requester.
def _organogram_base_users(stage: WorkflowStage, instance: WorkflowInstance) -> list:
    """Resolve base approvers by climbing the CX organogram relative to the requester.

    Opt-in strategy (ApproverSource.ORGANOGRAM). Degrades gracefully to an empty
    list if vs_user / the organogram service is unavailable, mirroring the RBAC
    path's defensive ImportError handling. The requester is excluded inside the
    service helpers, so they can never approve their own submission.
    """
    try:
        from vs_user.services.organogram import OrganogramService
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "vs_user organogram not available; ORGANOGRAM stage resolved to no approvers.")
        return []

    requester = instance.requested_by
    target = stage.organogram_target

    if target == OrganogramTarget.DIRECT_MANAGER:
        return OrganogramService.resolve_direct_manager(requester)
    if target == OrganogramTarget.N_LEVELS_UP:
        return OrganogramService.resolve_n_levels_up(requester, stage.organogram_levels)
    if target == OrganogramTarget.DEPARTMENT_HEAD:
        return OrganogramService.resolve_department_head(requester)
    if target == OrganogramTarget.SPECIFIC_POSITION:
        return OrganogramService.resolve_specific_position(
            stage.organogram_position, exclude_user=requester,
        )
    return []


# Build the frozen approver snapshot for a stage activation.
def resolve_approvers(stage: WorkflowStage, instance: WorkflowInstance) -> List[EligibleApprover]:
    """Build the full eligible approver list for a stage at the moment it activates.

    The base approver set is produced by the stage's `approver_source`:
      - RBAC_PERMISSION (default): users holding stage.approver_permission_key
        in the configured scope. This is the original, untouched behaviour.
      - ORGANOGRAM (opt-in): the holder(s) of the seat reached by climbing the
        CX organogram relative to the requester (direct manager, N levels up,
        department head, or a specific position).

    The requester is always excluded — they cannot approve their own submission.
    Active delegations then expand the list regardless of source: if an eligible
    approver has delegated their authority, the delegate is added on their behalf
    (and the delegator removed when the delegation is exclusive). De-duplication
    via a seen-set on (user_id, on_behalf_of_id) pairs prevents the same row
    appearing twice. A delegate acting for two different delegators intentionally
    appears twice — once per delegator — because the on_behalf_of field differs.
    """
    if stage.approver_source == ApproverSource.ORGANOGRAM:
        # Organogram approvers are already relative to the requester; still exclude self-approval.
        base_users = [
            u for u in _organogram_base_users(stage, instance)
            if u and u.pk != instance.requested_by_id
        ]
    else:
        if not stage.approver_permission_key:
            return []
        # RBAC approvers are resolved at activation time and then frozen.
        base_qs = _users_with_permission(
            school=instance.school,
            branch=instance.branch,
            permission_key=stage.approver_permission_key,
            scope=ApproverScope(stage.approver_scope),
        )
        base_qs = base_qs.exclude(pk=instance.requested_by_id)
        base_users = list(base_qs.distinct())

    base_ids = {u.pk for u in base_users}

    now = timezone.now()
    # Delegations only apply while active, unrevoked, and matching this document type.
    delegations = ApprovalDelegation.objects.filter(
        school=instance.school,
        starts_at__lte=now, ends_at__gte=now,
        revoked_at__isnull=True,
        delegator_id__in=base_ids,
    ).filter(
        Q(document_type="") | Q(document_type=instance.document_type),
    ).exclude(delegate_id=instance.requested_by_id).select_related("delegator", "delegate")

    result: List[EligibleApprover] = []
    seen = set()
    # Exclusive delegation removes the delegator from the active approver list.
    excluded_delegators = {d.delegator_id for d in delegations if d.exclusive}

    for u in base_users:
        if u.pk in excluded_delegators:
            continue
        key = (u.pk, None)
        if key in seen:
            continue
        seen.add(key)
        result.append(EligibleApprover(user=u, on_behalf_of=None))

    for d in delegations:
        # Keep one row per delegate/delegator pair for audit clarity.
        key = (d.delegate_id, d.delegator_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(EligibleApprover(user=d.delegate, on_behalf_of=d.delegator))

    return result
