"""
Approver resolution - builds the eligible approver list for a stage at activation time.

The list is frozen into WorkflowStageApprover rows the moment a stage activates.
All subsequent eligibility checks read that snapshot rather than re-querying RBAC
live, so mid-workflow role changes don't retroactively affect who can vote.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

from django.db.models import Q
from django.utils import timezone

from vs_workflow.constants import (
    ApproverScope, ApproverSource, GroupMemberKind, OrganogramTarget,
)
from vs_workflow.exceptions import UnknownApproverSourceError
from vs_workflow.models import ApprovalDelegation, WorkflowInstance, WorkflowStage

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser


# Store one eligible actor and any delegation context for the stage snapshot.
@dataclass
class EligibleApprover:
    """Carries one resolved approver and, when delegation is active, who they act for.

    on_behalf_of is set when the approver was added via an ApprovalDelegation row
    rather than being an approver in their own right. It is stored in the
    WorkflowStageApprover snapshot so the audit trail shows both names.
    """
    user: AbstractBaseUser
    on_behalf_of: Optional[AbstractBaseUser] = None


# Resolve the active assignees of one or more roles.
def _users_for_roles(role_ids, tenant, branch) -> list:
    """Active assignees of the given roles within one tenant.

    The role itself must be ACTIVE, the assignment must be ACTIVE, and the
    user must be active. ``branch`` narrows to branch-limited assignments for
    that branch (plus tenant-wide ones); pass None to count tenant-wide
    assignments only.

    The branch condition comes from ``vs_rbac`` rather than being spelled out
    here. It was a fourth copy of one rule, and a copy is free to drift from the
    permission gate - which would mean nominating an approver that
    ``has_permission`` then refuses, parking the document nobody can explain.
    """
    role_ids = [r for r in role_ids if r]
    if not role_ids:
        return []

    from vs_rbac.evaluator import _assignment_branch_q
    from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment

    assignments = (
        TenantUserRoleAssignment.objects.filter(
            tenant=tenant,
            role_id__in=role_ids,
            role__status=TenantRoleTemplate.Status.ACTIVE,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            user__is_active=True,
        )
        .filter(_assignment_branch_q(branch))
        .select_related("user")
    )
    # De-dup by user id - a user can hold tenant-wide and branch-limited
    # assignments of the same role simultaneously.
    return list({a.user_id: a.user for a in assignments}.values())


# Resolve a role key inside one tenant.
def _users_for_role_key(role_key: str, tenant, branch) -> list:
    """Holders of the role with *role_key* in *tenant*.

    Resolution goes through the key rather than a foreign key because a central
    template is published once, without a tenant, and has to name the same
    authority in every tenant that runs it. A tenant with no such role resolves
    to nobody, which the stage's skip_if_no_approvers policy then decides on -
    run ``check_workflow_role_coverage`` to find those gaps before they bite.
    """
    if not role_key:
        return []

    from vs_rbac.models import TenantRoleTemplate

    role = TenantRoleTemplate.objects.filter(
        tenant=tenant, key=role_key, status=TenantRoleTemplate.Status.ACTIVE,
    ).values_list("pk", flat=True).first()
    if role is None:
        return []
    return _users_for_roles([role], tenant, branch)


# Resolve the active assignees of a named tenant role.
def stage_role_key(stage: WorkflowStage) -> str:
    """The role key a ROLE stage resolves, preferring the portable key."""
    if stage.approver_role_key:
        return stage.approver_role_key
    return stage.approver_role.key if stage.approver_role_id else ""


def _role_base_users(stage: WorkflowStage, instance: WorkflowInstance) -> list:
    """Resolve base approvers as the holders of the stage's role.

    The default strategy. The stage names a role by key and the engine reads
    that role's ACTIVE assignments in the tenant that raised the request, so
    one central template serves every tenant. BRANCH scope also honours
    branch-limited assignments; every other scope counts tenant-wide
    assignments only. A missing, archived or unassigned role resolves to no
    approvers, leaving skip_if_no_approvers to decide.
    """
    branch_arg = instance.branch if stage.approver_scope == ApproverScope.BRANCH else None
    return _users_for_role_key(stage_role_key(stage), instance.tenant, branch_arg)


# Keep only active users belonging to the resolving tenant.
def _tenant_members(users, tenant_id) -> list:
    """De-dupe and enforce tenant containment on a resolved user list.

    The single definition of "this person may hold approval authority here", so
    every door into the eligible list runs through this one function and the two
    conditions cannot drift apart between them.

    Containment is applied at resolution rather than trusted from the stored
    rows: organogram positions are platform-global seats, so a tenant's group
    must never route approval authority to somebody outside that tenant. The
    same reasoning covers a delegation row, whose tenant column scopes the row
    and says nothing about the user it names.
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
    for rule in stage.dynamic_rules.all():
        matched, trace = evaluate_condition(rule.condition, document)
        evaluations.append({
            "rule_id": str(rule.pk),
            "order": rule.order,
            "role_key": rule.role_key,
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
    return _users_for_role_key(rule.role_key, instance.tenant, branch_arg)


def _group_base_users(stage: WorkflowStage, instance: WorkflowInstance) -> list:
    """Resolve base approvers from stage.approver_group's membership.

    Opt-in strategy (ApproverSource.WORKFLOW_GROUP). approver_scope narrows
    ROLE members to the instance's branch exactly as it does for the ROLE
    source; USER and POSITION members are unaffected by scope.
    """
    branch_arg = instance.branch if stage.approver_scope == ApproverScope.BRANCH else None
    return resolve_group_users(stage.approver_group, instance.tenant, branch_arg)


# Public name for the role lookup, for callers outside the engine.
def role_holder_ids(*, role_key: str, tenant, branch) -> frozenset:
    """Ids of the people the engine would treat as holders of ``role_key`` here.

    The supported, read-only entry point for apps that must answer "can anybody
    approve here, and where can nobody?" without copying the engine's scope
    mapping. Same lookup the resolver uses, so an administrative coverage screen
    or the parking repair can never disagree with live routing.

    Returns ids rather than users because every caller so far only needs set
    arithmetic against the requester, and ids keep the memo cheap.
    """
    return frozenset(
        u.pk for u in _users_for_role_key(role_key, tenant, branch)
    )


# Resolve organogram-based approvers relative to the requester.
def _organogram_base_users(stage: WorkflowStage, instance: WorkflowInstance) -> list:
    """Resolve base approvers by climbing the CX organogram relative to the requester.

    Opt-in strategy (ApproverSource.ORGANOGRAM). Degrades gracefully to an empty
    list if vs_user / the organogram service is unavailable, mirroring the RBAC
    path's defensive ImportError handling. The requester is excluded inside the
    service helpers, so they can never approve their own submission.

    The seats this climbs are platform-global, so the holders it returns are not
    contained by anything here. ``resolve_approvers`` contains them, once, for
    every source; do not add a second containment filter to this function.
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


# Find the tenant's own approver choice for a stage, if it has made one.
def stage_override_for(stage: WorkflowStage, tenant):
    """The tenant's override for this stage, or None.

    Central templates are shared, so a tenant that wants a different approver
    on one step records it here instead of cloning the template. Only the
    approver changes; advance rule, rejection policy and routing stay with the
    template.
    """
    from vs_workflow.models import WorkflowStageApproverOverride

    if tenant is None:
        return None
    return (WorkflowStageApproverOverride.all_objects
            .select_related("approver_group")
            .filter(tenant=tenant, stage=stage).first())


def _override_base_users(override, stage: WorkflowStage, instance: WorkflowInstance) -> list:
    """Base approvers from a tenant's override, honouring the stage's scope."""
    branch_arg = instance.branch if stage.approver_scope == ApproverScope.BRANCH else None
    if override.approver_source == ApproverSource.WORKFLOW_GROUP:
        return resolve_group_users(override.approver_group, instance.tenant, branch_arg)
    return _users_for_role_key(override.approver_role_key, instance.tenant, branch_arg)


# Build the frozen approver snapshot for a stage activation.
def resolve_approvers(stage: WorkflowStage, instance: WorkflowInstance) -> List[EligibleApprover]:
    """Build the full eligible approver list for a stage at the moment it activates.

    A tenant override wins first, if the tenant has recorded one for this
    stage. Otherwise the base approver set comes from the stage's
    `approver_source`:
      - ROLE (default): holders of the stage's role key, resolved inside the
        requesting tenant so one central template serves everybody.
      - WORKFLOW_GROUP: the resolved membership of the named approver group,
        mixing people, roles, and positions.
      - DYNAMIC_ROLE: the role named by the first of the stage's ordered rules
        whose condition matches the document.
      - ORGANOGRAM: the holder(s) of the seat reached by climbing the CX
        organogram relative to the requester.

    Any other source raises :class:`~vs_workflow.exceptions.UnknownApproverSourceError`
    rather than resolving to nobody. An empty list is a legitimate answer that a
    skip-enabled stage acts on by skipping itself, so a source this function has
    not been taught must never be able to produce one. Adding a source means
    adding a branch below.

    Every source is then contained to ``instance.tenant`` and the requester is
    always excluded - they cannot approve their own submission. Both rules are
    applied once, after the dispatch, because they hold for every source: an
    approver from outside the requesting tenant is never eligible, whichever
    branch above produced them.

    Active delegations then expand the list regardless of source: if an eligible
    approver has delegated their authority, the delegate is added on their behalf
    (and the delegator removed when the delegation is exclusive). De-duplication
    via a seen-set on (user_id, on_behalf_of_id) pairs prevents the same row
    appearing twice. A delegate acting for two different delegators intentionally
    appears twice - once per delegator - because the on_behalf_of field differs.

    Containment is one rule, not one rule per code path, so it is applied to the
    delegates too: an approver may hand their authority to a colleague, never
    across a tenant boundary, and a delegation row cannot buy an outsider the
    eligibility the sources above are forbidden to give them directly.
    """
    override = stage_override_for(stage, instance.tenant)
    if override is not None:
        base_users = _override_base_users(override, stage, instance)
    elif stage.approver_source == ApproverSource.ROLE:
        base_users = _role_base_users(stage, instance)
    elif stage.approver_source == ApproverSource.WORKFLOW_GROUP:
        base_users = _group_base_users(stage, instance)
    elif stage.approver_source == ApproverSource.DYNAMIC_ROLE:
        base_users = _dynamic_role_base_users(stage, instance)
    elif stage.approver_source == ApproverSource.ORGANOGRAM:
        # Organogram approvers are already relative to the requester.
        base_users = _organogram_base_users(stage, instance)
    else:
        # Deliberately not a catch-all returning nobody: a skip-enabled stage
        # would act on that by skipping itself, so an unrecognised source must
        # fail loudly instead of quietly waving the document through.
        raise UnknownApproverSourceError(
            f"Stage '{stage.code}' names approver source "
            f"'{stage.approver_source}', which the engine cannot resolve.",
            stage=stage.code, approver_source=stage.approver_source,
        )

    # Tenant containment is barred from being per-source for the same reason
    # self-approval is: a rule each branch has to remember is a rule a new
    # branch eventually forgets. ORGANOGRAM forgot. Organogram positions are
    # platform-global seats, so a climb (SPECIFIC_POSITION above all, which does
    # not depend on the requester at all) could hand a tenant's document to
    # somebody outside that tenant. The role, group and override paths already
    # resolve inside instance.tenant, and _tenant_members is a pure filter, so
    # applying it here is a no-op for them and the one missing guard for
    # ORGANOGRAM. This is the first of the two doors into the eligible list; the
    # delegation block below is the second, and runs the same filter.
    base_users = _tenant_members(base_users, instance.tenant_id)

    # Self-approval is barred on every source, so the filter lives here once
    # rather than being repeated (and one day forgotten) per branch.
    base_users = [u for u in base_users if u is not None and u.pk != instance.requested_by_id]

    base_ids = {u.pk for u in base_users}

    now = timezone.now()
    # Delegations only apply while active, unrevoked, and matching this document type.
    delegations = list(ApprovalDelegation.objects.filter(
        tenant=instance.tenant,
        starts_at__lte=now, ends_at__gte=now,
        revoked_at__isnull=True,
        delegator_id__in=base_ids,
    ).filter(
        Q(document_type="") | Q(document_type=instance.document_type),
    ).exclude(delegate_id=instance.requested_by_id).select_related("delegator", "delegate"))

    # The second door, running the same containment filter as the first.
    # ``tenant=instance.tenant`` above scopes the delegation ROW and reads like
    # containment without being it: nothing constrained the user that row names,
    # so an approver could hand their authority to somebody in another tenant and
    # resolution would seat that outsider as an eligible approver. A delegate is
    # a person entering the eligible list, so they go through _tenant_members
    # exactly as the base users did, which also means "active" and "home tenant"
    # cannot come to mean different things at the two doors.
    #
    # Rows written before this rule existed may already name a foreign delegate,
    # and they are neutralised here rather than deleted or rewritten by a data
    # migration: ignoring a row at resolution is safe and reversible, whereas
    # destroying somebody's delegation history is neither.
    contained_delegate_ids = {
        u.pk for u in _tenant_members(
            [d.delegate for d in delegations], instance.tenant_id,
        )
    }
    # Deliberately ahead of excluded_delegators: an exclusive delegation naming
    # an outsider must not strip the delegator while contributing nobody. A row
    # that is not allowed to add an approver is not allowed to remove one either,
    # or refusing the outsider would empty the stage instead of leaving it as it
    # was before the row was written.
    delegations = [d for d in delegations if d.delegate_id in contained_delegate_ids]

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
