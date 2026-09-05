"""Approval-rule provisioning for leave requests.

Every absence a school records is a :class:`~schools.vs_staff.models.LeaveRequest`
submitted to the workflow engine on creation, so an absence is never both filed
and allowed by the same act. This module publishes the ladder that decides it.

**Per tenant only, and there is no platform-wide fallback.** A WORKFLOW_GROUP
stage cannot live on a global template, because a group is owned by one school
and a shared template cannot reference one. That is a constraint rather than an
oversight, and it suits leave: who signs off an absence is a per-school
arrangement, and a fallback naming a platform-wide authority was never the right
answer for it. A school with no ladder is refused loudly at submission, and
:func:`leave_template_gaps` is what finds them before a teacher does.

The approver is a **group**, not a provisioned role. A role would put a row on
the school's own Roles and access screen that nobody there created and nobody
can delete; this module renders that screen, so it would have been shipping its
own confusion. See ``constants.LEAVE_APPROVER_GROUP_CODE``.

The group itself is provisioned by ``vs_workflow.services.groups``, which every
seeded ladder on this platform shares. This module briefly carried its own copy
of that helper because the shared one had not landed yet; it has, and one way to
create a group is the right number.
"""
from __future__ import annotations

from .constants import (
    LEAVE_APPROVER_GROUP_CODE,
    LEAVE_DOCUMENT_TYPE,
    LEAVE_TEMPLATE_CODE,
    LEAVE_TEMPLATE_NAME,
)

TEMPLATE_LABEL = "leave request"


def _default_stages_payload(*, group_code: str) -> list:
    """One stage: somebody the school nominated says yes or no.

    One stage rather than two, which is where this differs from money. A payout
    ladder escalates by value because a larger sum deserves a second pair of
    eyes; leave has no comparable dimension, and a school that wants a second
    stage adds one to its own template rather than inheriting a threshold nobody
    asked for.

    ``approver_scope="BRANCH"``: a leave request scopes to the branch its person
    is posted to, so the resolver narrows to holders there. Somebody posted
    school-wide has a null branch and the resolver falls back to tenant-wide
    holders, which is the right answer for a registrar who belongs to the school
    rather than to a site.

    ``skip_if_no_approvers=False``: leave must never approve itself. Where the
    group is empty the engine activates the stage with an empty approver
    snapshot and the request *parks* rather than reaching a terminal APPROVED
    decision with no human involved. That is the safe failure, and it is what
    makes provisioning safe to run before a school has nominated anybody.
    Parking is not a dead end: the engine's repair makes the request actionable
    the moment somebody joins the group.

    ``advance_rule="ANY"`` and ``on_rejection="TERMINAL"``: one member's vote
    carries the stage, and a refusal ends the attempt rather than routing
    onwards. A teacher whose leave is refused files a new request rather than
    appealing into a queue nobody watches.
    """
    return [
        {
            "code": "approve",
            "label": "Leave approval",
            "kind": "APPROVAL",
            "order": 10,
            "approver_source": "WORKFLOW_GROUP",
            "approver_group_code": group_code,
            "approver_scope": "BRANCH",
            "advance_rule": "ANY",
            "on_rejection": "TERMINAL",
            "skip_if_no_approvers": False,
        },
    ]


def ensure_tenant_approval_templates(tenant, *, created_by=None):
    """Give one school its leave ladder. Returns ``(template, created)``.

    **Non-destructive.** A school that already has its own ladder is left
    exactly as it is, because the alternative is that a provisioning re-run
    silently discards the rule an administrator wrote.

    The group is ensured first, because publishing refuses a stage whose group
    does not exist rather than degrading to nobody: a group stage that lost its
    group would stall every future request, so the publish fails loudly instead.
    """
    from vs_workflow.models import WorkflowTemplate
    from vs_workflow.services.groups import ensure_approver_group
    from vs_workflow.services.templates import publish_template

    existing = WorkflowTemplate.all_objects.filter(
        tenant=tenant, branch=None, document_type=LEAVE_DOCUMENT_TYPE,
        code=LEAVE_TEMPLATE_CODE,
    ).first()
    if existing is not None:
        return existing, False

    # Empty by construction, so a request filed before anybody is nominated
    # parks rather than being approved unseen. The name derives from the code,
    # which is why only the description is passed.
    group, _ = ensure_approver_group(
        tenant, LEAVE_APPROVER_GROUP_CODE,
        description=(
            "Decides leave requests for this school. Empty until somebody adds "
            "the people, roles or seats that should approve an absence, so "
            "leave parks until then."
        ),
    )
    template = publish_template(
        tenant=tenant, branch=None, document_type=LEAVE_DOCUMENT_TYPE,
        code=LEAVE_TEMPLATE_CODE, name=LEAVE_TEMPLATE_NAME,
        description=f"Approval rule for a {TEMPLATE_LABEL} at this school.",
        created_by=created_by,
        stages_payload=_default_stages_payload(group_code=group.code),
    )
    return template, True


def leave_template_gaps() -> list[dict]:
    """Schools whose next leave submission would stop with TEMPLATE_NOT_FOUND.

    Asks the workflow resolver itself rather than reproducing its branch to
    tenant to platform cascade, so the answer is the one a real submission would
    get. Matters more here than for the money ladders: those have a platform
    fallback behind them and this one deliberately does not, so a school that
    was never provisioned has nothing at all to fall back to.
    """
    from vs_tenants.models import Tenant
    from vs_workflow.services.resolution import resolve_template

    gaps = []
    schools = Tenant.objects.filter(
        kind=Tenant.Kind.SCHOOL, status__in=Tenant.AUTHENTICABLE_STATUSES,
    ).order_by("slug")
    for tenant in schools:
        if resolve_template(
            LEAVE_DOCUMENT_TYPE, tenant=tenant, branch=None,
            code=LEAVE_TEMPLATE_CODE,
        ) is None:
            gaps.append({
                "tenant_id": tenant.pk,
                "tenant_slug": tenant.slug,
                "reason": (
                    "No active leave-approval template for this school, and "
                    "there is no platform fallback for leave."
                ),
            })
    return gaps


def unstaffed_leave_groups() -> list[dict]:
    """Schools whose leave-approver group is empty, so every request parks.

    A separate question from :func:`leave_template_gaps` and the one an operator
    is more likely to need: the ladder is published and correct, and nobody has
    been nominated, so requests pile up in a parked state that looks like a bug
    from the outside and is the safe failure from the inside.
    """
    from vs_workflow.models import WorkflowApproverGroup

    return [
        {
            "tenant_id": group.tenant_id,
            "tenant_slug": group.tenant.slug,
            "reason": (
                "The Leave Approvers group is empty, so every leave request "
                "parks until somebody is added to it."
            ),
        }
        for group in (
            WorkflowApproverGroup.objects
            .filter(code=LEAVE_APPROVER_GROUP_CODE, is_active=True, members__isnull=True)
            .select_related("tenant")
            .order_by("tenant__slug")
        )
    ]
