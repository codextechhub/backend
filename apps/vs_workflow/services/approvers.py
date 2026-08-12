"""
Approver resolution - builds the eligible approver list for a stage at activation time.

The list is frozen into WorkflowStageApprover rows the moment a stage activates.
All subsequent eligibility checks read that snapshot rather than re-querying RBAC
live, so mid-workflow permission changes don't retroactively affect who can vote.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

from django.db.models import Q
from django.utils import timezone

from vs_workflow.constants import (
    ApproverScope, ApproverSource, GroupMemberKind, OrganogramTarget,
)
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
def _users_with_permission(tenant, branch, permission_key: str, scope: ApproverScope):
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
        if tenant is not None:
            qs = qs.filter(tenant=tenant)
        return qs

    branch_arg = branch if scope == ApproverScope.BRANCH else None
    return resolve_users_with_permission(
        tenant=tenant, branch=branch_arg, permission_key=permission_key,
    )


# Resolve the active assignees of one or more tenant roles.
def _users_for_roles(role_ids, tenant, branch) -> list:
    """Active assignees of the given roles within one tenant.

    Shared by the ROLE stage source and by ROLE members of an approver group,
    so both honour the same rules: the role itself must be ACTIVE, the
    assignment must be ACTIVE, and the user must be active. ``branch`` narrows
    to branch-limited assignments for that branch (plus tenant-wide ones);
    pass None to count tenant-wide assignments only.
    """
    role_ids = [r for r in role_ids if r]
    if not role_ids:
        return []

    from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment

    assignments = (
        TenantUserRoleAssignment.objects.filter(
            tenant=tenant,
            role_id__in=role_ids,
            role__status=TenantRoleTemplate.Status.ACTIVE,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            user__is_active=True,
        )
        .filter(Q(branch__isnull=True) | Q(branch=branch))
        .select_related("user")
    )
    # De-dup by user id - a user can hold tenant-wide and branch-limited
    # assignments of the same role simultaneously.
    return list({a.user_id: a.user for a in assignments}.values())


# Resolve the active assignees of a named tenant role.
def _role_base_users(stage: WorkflowStage, instance: WorkflowInstance) -> list:
    """Resolve base approvers as the active assignees of stage.approver_role.

    Opt-in strategy (ApproverSource.ROLE) - the human-facing replacement for
    permission keys: the stage points straight at a named tenant role and the
    engine reads its ACTIVE assignments. Branch handling mirrors the RBAC
    path exactly: BRANCH scope honours branch-limited assignments for the
    instance's branch, every other scope counts tenant-wide assignments only.
    An archived or deactivated role resolves to no approvers, so the stage's
    skip_if_no_approvers policy decides what happens next.
    """
    branch_arg = instance.branch if stage.approver_scope == ApproverScope.BRANCH else None
    return _users_for_roles([stage.approver_role_id], instance.tenant, branch_arg)


# Keep only active users belonging to the resolving tenant.
def _tenant_members(users, tenant_id) -> list:
    """De-dupe and enforce tenant containment on a resolved user list.

    Containment is applied at resolution rather than trusted from the stored
    rows: organogram positions are platform-global seats, so a tenant's group
    must never route approval authority to somebody outside that tenant.
    """
    return list({
        u.pk: u for u in users
        if u is not None and u.is_active and u.tenant_id == tenant_id
    }.values())


# Resolve the mixed membership of a named approver group.
def resolve_group_users(group, tenant, branch=None) -> list:
    """Live membership of an approver group as a flat, de-duped user list.

    The group's rows are heterogeneous and resolved together:
      * USER     - the named person, taken as-is.
      * ROLE     - every active assignee of that role (same rules as the ROLE
                   stage source).
      * POSITION - the current holder(s) of that organogram seat.

    A deactivated group resolves to nobody, leaving the stage's
    skip_if_no_approvers policy to decide what happens next. Shared by stage
    activation and by the Workflow Approver screen's live preview, so what an
    admin sees is exactly what the engine will use.
    """
    if group is None or not group.is_active:
        return []

    members = list(group.members.select_related("user", "position").all())

    users: list = [m.user for m in members if m.kind == GroupMemberKind.USER and m.user]
    users += _users_for_roles(
        [m.role_id for m in members if m.kind == GroupMemberKind.ROLE], tenant, branch,
    )
    for m in members:
        if m.kind == GroupMemberKind.POSITION and m.position is not None:
            users += m.position.current_holders

    return _tenant_members(users, getattr(tenant, "pk", tenant))


# Per-member breakdown of a group's live resolution.
def describe_group_members(group, tenant, branch=None) -> list:
    """Explain a group row by row: what each member points at and who it
    resolves to right now.

    Powers the Workflow Approver screen, where a ROLE or POSITION row shows
    "resolves to N people" and expands to name them. Runs the same resolution
    the engine runs, so the screen can never disagree with an activation.
    """
    if group is None:
        return []

    tenant_id = getattr(tenant, "pk", tenant)
    rows = []
    for m in group.members.select_related("user", "role", "position").all():
        if m.kind == GroupMemberKind.USER:
            label = _display_name(m.user)
            target_code, resolved = None, _tenant_members([m.user], tenant_id)
        elif m.kind == GroupMemberKind.ROLE:
            label = m.role.name if m.role else ""
            target_code = m.role.key if m.role else None
            resolved = _tenant_members(
                _users_for_roles([m.role_id], tenant, branch), tenant_id)
        else:
            label = m.position.title if m.position else ""
            target_code = m.position.code if m.position else None
            resolved = _tenant_members(
                m.position.current_holders if m.position else [], tenant_id)
        rows.append({
            "id": str(m.pk),
            "kind": m.kind,
            "label": label,
            "target_code": target_code,
            "resolved_count": len(resolved),
            "resolved_users": [
                {"id": str(u.pk), "name": _display_name(u), "email": u.email}
                for u in resolved
            ],
        })
    return rows


def _display_name(user) -> str:
    if user is None:
        return ""
    return getattr(user, "full_name", "") or user.get_username()


# Pick the rule a DYNAMIC_ROLE stage fires for a given document.
def match_dynamic_rule(stage: WorkflowStage, document):
    """First rule whose condition matches *document*, with the evaluation trace.

    Returns ``(rule, evaluations)``. ``rule`` is None when nothing matched,
    which happens only when the stage has no fallback rule. ``evaluations`` is
    the per-rule trace list, shaped like the route-evaluation audit entry, so
    "why did this go to the Bursar" is answerable after the fact.
    """
    from vs_workflow.conditions.evaluator import evaluate_condition

    evaluations = []
    chosen = None
    for rule in stage.dynamic_rules.select_related("role").all():
        matched, trace = evaluate_condition(rule.condition, document)
        evaluations.append({
            "rule_id": str(rule.pk),
            "order": rule.order,
            "role_key": rule.role.key,
            "is_fallback": rule.is_fallback,
            "trace": trace,
            "picked": False,
        })
        if matched:
            chosen = rule
            evaluations[-1]["picked"] = True
            break
    return chosen, evaluations


def _dynamic_role_base_users(stage: WorkflowStage, instance: WorkflowInstance) -> list:
    """Resolve base approvers by letting the document choose the role.

    Opt-in strategy (ApproverSource.DYNAMIC_ROLE): ordered rules are evaluated
    against the document and the first match names the role, whose active
    assignees then resolve exactly as they do for the ROLE source. No match and
    no fallback resolves to nobody, leaving skip_if_no_approvers to decide -
    the same outcome as a role nobody holds.
    """
    rule, _ = match_dynamic_rule(stage, instance.document)
    if rule is None:
        return []
    branch_arg = instance.branch if stage.approver_scope == ApproverScope.BRANCH else None
    return _users_for_roles([rule.role_id], instance.tenant, branch_arg)


def _group_base_users(stage: WorkflowStage, instance: WorkflowInstance) -> list:
    """Resolve base approvers from stage.approver_group's membership.

    Opt-in strategy (ApproverSource.WORKFLOW_GROUP). approver_scope narrows
    ROLE members to the instance's branch exactly as it does for the ROLE
    source; USER and POSITION members are unaffected by scope.
    """
    branch_arg = instance.branch if stage.approver_scope == ApproverScope.BRANCH else None
    return resolve_group_users(stage.approver_group, instance.tenant, branch_arg)


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
      - ROLE (opt-in): the active assignees of the named tenant role in
        stage.approver_role, honouring approver_scope for branch narrowing.
      - WORKFLOW_GROUP (opt-in): the resolved membership of the named approver
        group in stage.approver_group, mixing people, roles, and positions.
      - DYNAMIC_ROLE (opt-in): the role named by the first of the stage's
        ordered rules whose condition matches the document.

    The requester is always excluded - they cannot approve their own submission.
    Active delegations then expand the list regardless of source: if an eligible
    approver has delegated their authority, the delegate is added on their behalf
    (and the delegator removed when the delegation is exclusive). De-duplication
    via a seen-set on (user_id, on_behalf_of_id) pairs prevents the same row
    appearing twice. A delegate acting for two different delegators intentionally
    appears twice - once per delegator - because the on_behalf_of field differs.
    """
    if stage.approver_source == ApproverSource.ORGANOGRAM:
        # Organogram approvers are already relative to the requester; still exclude self-approval.
        base_users = [
            u for u in _organogram_base_users(stage, instance)
            if u and u.pk != instance.requested_by_id
        ]
    elif stage.approver_source == ApproverSource.ROLE:
        base_users = [
            u for u in _role_base_users(stage, instance)
            if u.pk != instance.requested_by_id
        ]
    elif stage.approver_source == ApproverSource.WORKFLOW_GROUP:
        base_users = [
            u for u in _group_base_users(stage, instance)
            if u.pk != instance.requested_by_id
        ]
    elif stage.approver_source == ApproverSource.DYNAMIC_ROLE:
        base_users = [
            u for u in _dynamic_role_base_users(stage, instance)
            if u.pk != instance.requested_by_id
        ]
    else:
        if not stage.approver_permission_key:
            return []
        # RBAC approvers are resolved at activation time and then frozen.
        base_qs = _users_with_permission(
            tenant=instance.tenant,
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
        tenant=instance.tenant,
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
