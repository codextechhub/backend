"""Approval-rule provisioning for leave requests and for new hires.

Every absence a school records is a :class:`~schools.vs_staff.models.LeaveRequest`
submitted to the workflow engine on creation, so an absence is never both filed
and allowed by the same act. At a school that approves each hire before inviting
it, every new staff record is submitted the same way. This module publishes the
ladders that decide both.

**Leave is decided by the person's line manager.** The leave ladder has one
stage that climbs the school's organogram one level from the person the leave is
for (not from whoever filed it), so when Tunde Bello asks for a week off it is
the person his post reports to who decides, and nothing has to be configured for
it: the chart the school already keeps names them. A person with no manager on
the chart is the case the stage parks on. Leave never approves itself, so the
request waits until an administrator sets who the post reports to or chooses an
approver for that request, and the filing is told so (``NO_APPROVER``). A school
that wants a group, a role or a second stage edits its own template.

**Per tenant only, and there is no platform-wide fallback.** Who signs off an
absence is a per-school arrangement, and a fallback naming a platform-wide
authority was never the right answer for it. A submission with no ladder to
resolve to is refused (``TEMPLATE_NOT_FOUND``), so the ladder is ensured wherever a leave request is
submitted (:func:`ensure_tenant_approval_templates`), when a school is created
(:func:`provision_leave_approval_for_school`), and by the
``provision_leave_approvals`` command for any school without one.
:func:`leave_template_gaps` is what finds any that are still missing one.

**The hire ladder is group-based.** A new hire has no line manager yet, so its
one stage names an approver group (``constants.HIRE_APPROVER_GROUP_CODE``), not
a provisioned role: a role would put a row on the school's own Roles and access
screen that nobody there created and nobody can delete. The group is provisioned
by ``vs_workflow.services.groups``, which every seeded ladder on this platform
shares. A school may also route leave through a Leave Approvers group it chose;
:func:`unstaffed_leave_groups` finds those that nobody has been added to.
"""
from __future__ import annotations

import logging

from .constants import (
    HIRE_APPROVER_GROUP_CODE,
    HIRE_DOCUMENT_TYPE,
    HIRE_TEMPLATE_CODE,
    HIRE_TEMPLATE_NAME,
    LEAVE_APPROVER_GROUP_CODE,
    LEAVE_DOCUMENT_TYPE,
    LEAVE_TEMPLATE_CODE,
    LEAVE_TEMPLATE_NAME,
)

logger = logging.getLogger(__name__)

TEMPLATE_LABEL = "leave request"


def _group_stages_payload(*, group_code: str, label: str) -> list:
    """One stage: somebody the school nominated says yes or no.

    One stage rather than two, which is where this differs from money. A payout
    ladder escalates by value because a larger sum deserves a second pair of
    eyes; a hire has no comparable dimension, and a school that wants a second
    stage adds one to its own template rather than inheriting a threshold nobody
    asked for.

    ``approver_scope="BRANCH"``: a hire scopes to the branch its person is
    posted to, so the resolver narrows to holders there. Somebody posted
    school-wide has a null branch and the resolver falls back to tenant-wide
    holders, which is the right answer for a registrar who belongs to the school
    rather than to a site.

    ``skip_if_no_approvers=False``: nothing approves itself. Where the group is
    empty the engine activates the stage with an empty approver snapshot and the
    request *parks* rather than reaching a terminal APPROVED decision with no
    human involved. That is the safe failure, and it is what makes provisioning
    safe to run before a school has nominated anybody. Parking is not a dead
    end: the engine's repair makes the request actionable the moment somebody
    joins the group.

    ``advance_rule="ANY"`` and ``on_rejection="TERMINAL"``: one member's vote
    carries the stage, and a refusal ends the attempt rather than routing
    onwards.
    """
    return [
        {
            "code": "approve",
            "label": label,
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


def _line_manager_stages_payload(*, label: str = "Line manager approval") -> list:
    """One stage: the line manager of the person the request is for.

    ``approver_source="ORGANOGRAM"`` with ``DIRECT_MANAGER`` climbs one level
    from the person the leave is for. The engine climbs from the document's
    subject rather than from whoever filed it, so an administrator recording
    leave on a teacher's behalf routes to the teacher's manager, and the person
    taking leave can never be their own approver.

    ``approver_scope="SCHOOL"``: a reporting line belongs to the whole school's
    chart, and a line manager posted at another branch still manages.

    ``skip_if_no_approvers=False``: leave never approves itself. A person with
    no manager on the chart parks the request until an administrator sets who
    their post reports to or chooses an approver for that request.

    ``advance_rule="ANY"`` and ``on_rejection="TERMINAL"``: one vote carries the
    stage, and a refusal ends the attempt. A teacher whose leave is refused
    files a new request rather than appealing into a queue nobody watches.
    """
    return [
        {
            "code": "approve",
            "label": label,
            "kind": "APPROVAL",
            "order": 10,
            "approver_source": "ORGANOGRAM",
            "organogram_target": "DIRECT_MANAGER",
            "approver_scope": "SCHOOL",
            "advance_rule": "ANY",
            "on_rejection": "TERMINAL",
            "skip_if_no_approvers": False,
        },
    ]


def ensure_tenant_approval_templates(tenant, *, created_by=None):
    """Give one school its leave ladder. Returns ``(template, created)``.

    **Non-destructive.** A school that already has its own ladder, whether the
    line-manager one or a group-based one an administrator chose, is left
    exactly as it is, because the alternative is that a provisioning re-run
    silently discards the rule an administrator wrote.

    A new ladder is the line-manager one (:func:`_line_manager_stages_payload`).
    It names no group, so there is nothing to provision beside the template.
    """
    from vs_workflow.models import WorkflowTemplate
    from vs_workflow.services.templates import publish_template

    existing = WorkflowTemplate.all_objects.filter(
        tenant=tenant, branch=None, document_type=LEAVE_DOCUMENT_TYPE,
        code=LEAVE_TEMPLATE_CODE,
    ).first()
    if existing is not None:
        return existing, False

    template = publish_template(
        tenant=tenant, branch=None, document_type=LEAVE_DOCUMENT_TYPE,
        code=LEAVE_TEMPLATE_CODE, name=LEAVE_TEMPLATE_NAME,
        description=f"Approval rule for a {TEMPLATE_LABEL} at this school.",
        created_by=created_by,
        stages_payload=_line_manager_stages_payload(),
    )
    return template, True


def provision_leave_approval_for_school(school, *, actor=None):
    """Give a newly created school its leave ladder. Never raises.

    Returns the template, or ``None`` when provisioning failed, in which case the
    failure is logged with the school's slug. A school is worth far more than one
    of its ladders, and the ladder is ensured again at the first filing and by
    ``manage.py provision_leave_approvals``, so a failure here costs nothing that
    is not repaired later.

    The savepoint is the point of the function. School creation runs inside one
    atomic block, and a *database* failure inside it aborts the transaction:
    catching the exception is not enough, because every later statement fails
    too and the commit would take the school down with it. The inner block rolls
    back only this work.
    """
    from django.db import transaction

    try:
        with transaction.atomic():
            template, _created = ensure_tenant_approval_templates(
                school.tenant, created_by=actor,
            )
            return template
    except Exception:
        logger.exception(
            "Could not provision the leave ladder for school %s; it is ensured "
            "again at the first leave filing.", getattr(school, "slug", school),
        )
        return None


def ensure_hire_approval_template(tenant, *, created_by=None):
    """Give one school its New staff approval ladder. Returns ``(template, created)``.

    Published when a school turns hire approval on, and again before a hire is
    submitted, so a school whose setting was written some other way still has
    a ladder to submit to. Non-destructive for the reason
    :func:`ensure_tenant_approval_templates` gives.

    One stage, one member's vote carries it, scoped to the branch the new person
    is posted to, and an EMPTY group, so a hire added before anybody is nominated
    parks rather than being invited unseen. A person nominated later makes it
    actionable.
    """
    from vs_workflow.models import WorkflowTemplate
    from vs_workflow.services.groups import ensure_approver_group
    from vs_workflow.services.templates import publish_template

    existing = WorkflowTemplate.all_objects.filter(
        tenant=tenant, branch=None, document_type=HIRE_DOCUMENT_TYPE,
        code=HIRE_TEMPLATE_CODE,
    ).first()
    if existing is not None:
        return existing, False

    group, _ = ensure_approver_group(
        tenant, HIRE_APPROVER_GROUP_CODE,
        description=(
            "Approves each new member of staff before their invitation is sent. "
            "Empty until somebody adds the people, roles or seats that should "
            "approve a hire, so new staff wait until then."
        ),
    )
    template = publish_template(
        tenant=tenant, branch=None, document_type=HIRE_DOCUMENT_TYPE,
        code=HIRE_TEMPLATE_CODE, name=HIRE_TEMPLATE_NAME,
        description="Approval rule for a new member of staff at this school.",
        created_by=created_by,
        stages_payload=_group_stages_payload(
            group_code=group.code, label="New staff approval",
        ),
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
    """Schools whose leave ladder names an approver group that nobody belongs to.

    Only a school whose live leave template has a stage approving from the
    Leave Approvers group is reported: the line-manager ladder names no group,
    and a leftover empty group that nothing routes through parks nothing. A
    separate question from :func:`leave_template_gaps`: the ladder is published
    and correct and nobody has been nominated, so requests pile up in a parked
    state that looks like a bug from the outside and is the safe failure from
    the inside.
    """
    from vs_workflow.models import WorkflowApproverGroup, WorkflowStage

    in_use = set(
        WorkflowStage.objects.filter(
            template__document_type=LEAVE_DOCUMENT_TYPE,
            template__code=LEAVE_TEMPLATE_CODE, template__is_active=True,
            retired_at__isnull=True, approver_group__code=LEAVE_APPROVER_GROUP_CODE,
        ).values_list("approver_group_id", flat=True)
    )
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
            WorkflowApproverGroup.all_objects
            .filter(
                pk__in=in_use, code=LEAVE_APPROVER_GROUP_CODE, is_active=True,
                members__isnull=True,
            )
            .select_related("tenant")
            .order_by("tenant__slug")
        )
    ]
