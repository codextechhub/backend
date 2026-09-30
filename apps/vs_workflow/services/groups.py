"""Provisioning the approver groups that seeded ladders send their stages to.

A stage has to name somebody, and the seeds run before any school has decided
who. The old answer was a role: ``ensure_approver_role`` minted
``payout-approver`` and its siblings in every tenant, so a ROLE stage could
resolve. That worked and cost more than it bought.

    A school opens its roles screen and finds six roles it never created, with
    nobody in them, named after a workflow it has not read. It cannot delete
    them, because its own payout ladder resolves through them, and it cannot
    tell from the screen that this is why.

A group says the same thing without inventing a role. The stage points at
"Payout Approvers"; who that reaches is a list the school composes from its own
people, its own roles, or positions on its org chart, and changing it is one
screen rather than a permission model conversation. The group is created empty,
so a seeded ladder still parks its first document rather than approving it -
the same "seeded blocked, not seeded open" contract the roles gave.
"""
from __future__ import annotations


def ensure_approver_group(tenant, code: str, *, name: str = "", description: str = ""):
    """Return ``(group, created)`` for this tenant's group with ``code``.

    Empty by construction. A group nobody is in resolves to nobody, which the
    stage's ``skip_if_no_approvers`` policy then decides on - and the finance
    and payout ladders set that to False precisely so an unstaffed step parks
    the document instead of waving it through.

    An existing group is returned untouched, including a deactivated one. A
    school that switched a group off has said something, and provisioning
    switching it back on would be a seed overruling a person; publishing refuses
    an inactive group loudly instead, which is the better failure.
    """
    from vs_workflow.models import WorkflowApproverGroup

    if tenant is None:
        raise ValueError("A tenant is required to ensure an approver group.")
    if not code:
        raise ValueError("A group code is required to ensure an approver group.")

    existing = WorkflowApproverGroup.all_objects.filter(
        tenant=tenant, code=code,
    ).first()
    if existing is not None:
        return existing, False

    return WorkflowApproverGroup.all_objects.create(
        tenant=tenant, code=code,
        name=name or group_display_name(code),
        description=description,
    ), True


def group_display_name(code: str) -> str:
    """``payout-approvers`` -> ``Payout Approvers``."""
    return code.replace("-", " ").replace("_", " ").title()


def group_branch_ids(group) -> set:
    """The branches whose approvals *group*'s membership decides; empty means every branch.

    A group's own ``branch`` records who owns it, not where it approves:
    resolution never reads it, and a template may name any of its tenant's
    groups. So a group owned by one branch reaches that branch and the branch
    of every template with a stage, or a stage override, pointing at it. It
    reaches the whole tenant as soon as one of those templates is tenant-wide
    or shared, or a Dynamic Role (which serves every branch) sends to it.

        Bright Star's Ikeja administrator creates "Ikeja Approvers". The
        school-wide administrator later names it on the school's purchase
        ladder. From then on a member added from Ikeja approves Lekki's
        purchases too, so the group is the school-wide administrator's to
        change.

    Read by :func:`vs_rbac.scoping.caller_may_change` before a group or its
    membership is changed.
    """
    from django.db.models import Q

    from vs_workflow.models import WorkflowDynamicRoleRule, WorkflowTemplate

    if group.branch_id is None:
        return set()
    if WorkflowDynamicRoleRule.objects.filter(group=group).exists():
        return set()
    branches = set(
        WorkflowTemplate.all_objects.filter(
            Q(stages__approver_group=group, stages__retired_at__isnull=True)
            | Q(stages__tenant_overrides__approver_group=group)
        ).values_list("branch_id", flat=True).distinct()
    )
    if None in branches:
        return set()
    return branches | {group.branch_id}
