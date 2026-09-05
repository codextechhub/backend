"""Approval-rule provisioning for bulk payout batches.

Every payout is represented by a :class:`~vs_payments.models.PayoutBatch`, including a
single payout as a one-line batch. Provider submission is allowed only from the
terminal approval callback, and that boundary independently validates the exact
approved workflow instance and the required distinct human votes. A missing template
therefore fails closed before any provider call.

This module publishes the default two-stage ladder: an always-on checker, followed by
a senior checker when the batch reaches the high-value threshold.

Deliberately the same shape as :mod:`vs_procurement.approvals`, because a
tenant should not have to hold two different mental models for "who signs off on money
leaving". The differences are only where the documents genuinely differ, and each is
noted at the point it applies.

* :func:`ensure_default_approval_templates` publishes the platform-wide fallback, so no
  entity is ever left unroutable. It upserts, so only platform provisioning may call it.
* :func:`ensure_tenant_approval_templates` gives one tenant its own rules, which win
  through the engine's branch to tenant to platform cascade. It is non-destructive: a
  tenant that already has a ladder keeps whatever an administrator configured.
"""
from __future__ import annotations

from .constants import (
    WF_DEFAULT_APPROVE_GROUP,
    WF_DEFAULT_HIGH_VALUE_GROUP,
    WF_DEFAULT_HIGH_VALUE_THRESHOLD,
    WF_DEFAULT_TEMPLATE_CODE,
)

#: The single approvable document type in this app, and its human labels.
DOCUMENT_TYPE = "payments.payout_batch"
TEMPLATE_NAME = "Payout-batch approval"
TEMPLATE_LABEL = "payout batch"


def payout_approval_template_gaps() -> list[dict]:
    """List active ledger entities that cannot resolve a payout template.

    This asks the workflow resolver itself rather than reproducing its tenant to
    platform cascade. The result is therefore the exact set of active entities whose
    next payout submission would stop with ``TEMPLATE_NOT_FOUND``.

    Resolution is cached per tenant because payout batches carry no branch. A tenant
    with several active ledgers should cost the same two lookups as a tenant with one,
    while every affected entity is still named in the result for an operator.
    """
    from vs_finance.models import LedgerEntity
    from vs_tenants.models import Tenant
    from vs_workflow.services.resolution import resolve_template

    entities = (
        LedgerEntity.objects
        .filter(
            is_active=True,
            tenant__status__in=Tenant.AUTHENTICABLE_STATUSES,
        )
        .select_related("tenant")
        .order_by("tenant__slug", "code", "pk")
    )
    resolved_by_tenant = {}
    gaps = []
    for entity in entities:
        if entity.tenant_id not in resolved_by_tenant:
            resolved_by_tenant[entity.tenant_id] = resolve_template(
                DOCUMENT_TYPE,
                tenant=entity.tenant,
                branch=None,
                code=WF_DEFAULT_TEMPLATE_CODE,
            )
        if resolved_by_tenant[entity.tenant_id] is None:
            gaps.append({
                "entity_id": entity.pk,
                "entity_code": entity.code,
                "entity_name": entity.name,
                "tenant_id": entity.tenant_id,
                "tenant_slug": entity.tenant.slug,
                "reason": (
                    "No active standard payout-approval template resolves at the "
                    "tenant or platform scope."
                ),
            })
    return gaps


def _default_stages_payload(
    *, approve_group_code: str, high_value_group_code: str, high_value_threshold: int,
) -> list:
    """The two-stage ladder a tenant is seeded with.

    The checker stage always runs. The senior stage runs when the batch total reaches
    the configured high-value threshold. Provider-bound enforcement independently
    requires distinct human actors, so a weakened template cannot weaken cash-out.

    Two properties are carried over from procurement on purpose.

    ``skip_if_no_approvers=False``: money must never approve itself. When the approver
    group is empty the engine activates the stage with an empty approver
    snapshot and the batch *parks* rather than reaching a terminal APPROVED decision
    with no human involved. That is the safe failure, and it is the reason seeding is
    safe to run before anybody has been appointed. Parking is not a dead end: the
    engine's repair (:mod:`vs_workflow.services.parking`) makes the batch actionable as
    soon as somebody joins the group. Payout handlers deliberately forbid continuing
    without a human vote.

    ``advance_rule="ANY"`` and ``on_rejection="TERMINAL"``: one holder's vote carries
    the stage, and a rejection ends the attempt rather than routing onwards.

    The one deliberate divergence from procurement is the engine's legacy tenant-wide
    scope token, ``approver_scope="SCHOOL"``, where procurement uses ``"BRANCH"``. A
    payout batch is entity-scoped and its ``branch`` property is always ``None``, so the
    resolver selects tenant-wide holders rather than claiming a branch the model cannot
    back.
    """
    return [
        {
            "code": "checker",
            "label": "Payout checker approval",
            "kind": "APPROVAL",
            "order": 10,
            "approver_source": "WORKFLOW_GROUP",
            "approver_group_code": approve_group_code,
            # Batches carry no branch; see the docstring above.
            "approver_scope": "SCHOOL",
            "advance_rule": "ANY",
            "on_rejection": "TERMINAL",
            # Never auto-skip: an unstaffed stage must park the batch, not let it
            # pay itself out.
            "skip_if_no_approvers": False,
        },
        {
            "code": "senior",
            "label": "Senior payout approval",
            "kind": "APPROVAL",
            "order": 20,
            "approver_source": "WORKFLOW_GROUP",
            "approver_group_code": high_value_group_code,
            "approver_scope": "SCHOOL",
            "advance_rule": "ANY",
            "on_rejection": "TERMINAL",
            "skip_if_no_approvers": False,
            "inclusion_condition": {
                "op": "gte", "field": "total_amount",
                "value": int(high_value_threshold),
            },
        },
    ]


def ensure_default_approval_templates(*, created_by=None):
    """Publish (idempotently) the **platform-wide** payout-batch route, with no steps.

    The last-resort route, not a ladder: platform-scoped
    (``tenant=None, branch=None``) so no entity is left with an unroutable batch, and
    a tenant's own template overrides it through the engine's cascade.

    It takes no approver arguments because it publishes nobody to approve against. A
    step here would have to name the same authority in every tenant at once, which
    only a role key can do, and minting that role in every tenant is what this
    stopped doing. What a tenant gets instead comes from
    :func:`ensure_tenant_approval_templates`, whose steps name that tenant's own
    approver groups.

    A batch resolving here therefore finds no steps and is refused with
    :class:`~vs_workflow.exceptions.ApprovalNotConfiguredError` rather than parked or
    paid: the tenant either builds its ladder or confirms the post deliberately, and
    the confirmation is recorded against whoever gives it.

    Re-running upserts one shared row that every tenant without its own template reads,
    which is why only platform-level provisioning may call it. Returns the published
    :class:`~vs_workflow.models.WorkflowTemplate`.
    """
    from vs_workflow.services.templates import publish_template

    return publish_template(
        tenant=None, branch=None, document_type=DOCUMENT_TYPE,
        code=WF_DEFAULT_TEMPLATE_CODE, name=TEMPLATE_NAME,
        description=f"Default approval rule for a {TEMPLATE_LABEL}.",
        created_by=created_by,
        # No steps, deliberately.
        #
        # The shared row is one template every tenant runs, so a step on it
        # cannot name an approver group: a group belongs to one tenant. The only
        # thing that can name the same authority everywhere is a role key, and
        # minting a role in every tenant so a central ladder can resolve is what
        # this stopped doing.
        #
        # So the shared row carries the document type and nothing else. A tenant
        # that has not built its own ladder resolves to this, finds no steps, and
        # is asked to confirm - ApprovalNotConfiguredError, recorded against
        # whoever confirms. That is somebody deciding, rather than a seeded
        # ladder deciding for them, and it is why the empty case had to stop
        # being silent before this was safe.
        stages_payload=[],
    )


def ensure_tenant_approval_templates(
    tenant,
    *,
    approve_group_code: str = WF_DEFAULT_APPROVE_GROUP,
    high_value_group_code: str = WF_DEFAULT_HIGH_VALUE_GROUP,
    high_value_threshold: int = WF_DEFAULT_HIGH_VALUE_THRESHOLD,
    created_by=None,
):
    """Give one tenant its **own** payout-approval ladder. Returns ``(template, created)``.

    This is where a tenant's payout steps come from. The platform row carries none
    (see :func:`ensure_default_approval_templates`), because a shared row cannot name
    a tenant's approver group; a tenant-scoped template
    (``tenant=<tenant>, branch=None``) wins over it through the engine's own cascade,
    and nothing outside this tenant can reach it.

    **Non-destructive.** A tenant that already has its own ladder is left exactly as it
    is and reported with ``created=False``: re-running after an administrator repointed
    a stage must not quietly restore the defaults. (Contrast
    :func:`ensure_default_approval_templates`, which upserts, because the platform row
    is provisioning's to own.)

    **Seeded blocked, not seeded open.** Each step names an approver group that is
    created empty, so the first batch submitted parks and says so instead of paying
    itself out. Safe for onboarding to call on every tenant creation, and for an
    administrator to call again.
    """
    from vs_workflow.models import WorkflowTemplate
    from vs_workflow.services.groups import ensure_approver_group
    from vs_workflow.services.templates import publish_template

    if tenant is None:
        raise ValueError("A tenant is required to seed its payout-approval rules.")

    # all_objects deliberately: the explicit tenant filter is the boundary, and a row
    # hidden by ambient request-local scoping would be re-published over, which is
    # exactly the destructive outcome this function promises never to cause.
    existing = WorkflowTemplate.all_objects.filter(
        tenant=tenant, branch=None, document_type=DOCUMENT_TYPE,
        code=WF_DEFAULT_TEMPLATE_CODE,
    ).first()
    if existing is not None:
        return existing, False

    # A stage will not publish against a group the tenant does not have, and a
    # brand-new tenant has none. Create them empty, so seeding works on a fresh
    # tenant without inventing approval authority.
    for group_code, label in (
        (approve_group_code, "payout batches"),
        (high_value_group_code, "high-value payout batches"),
    ):
        ensure_approver_group(
            tenant, group_code,
            description=f"Approves {label}. Empty until the tenant puts "
                        "somebody in it, so batches park until then.",
        )

    return publish_template(
        tenant=tenant, branch=None, document_type=DOCUMENT_TYPE,
        code=WF_DEFAULT_TEMPLATE_CODE, name=TEMPLATE_NAME,
        description=f"Approval rule for a {TEMPLATE_LABEL}.",
        created_by=created_by,
        stages_payload=_default_stages_payload(
            approve_group_code=approve_group_code,
            high_value_group_code=high_value_group_code,
            high_value_threshold=high_value_threshold,
        ),
    ), True
