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
    WF_DEFAULT_APPROVE_ROLE,
    WF_DEFAULT_HIGH_VALUE_ROLE,
    WF_DEFAULT_HIGH_VALUE_THRESHOLD,
    WF_DEFAULT_TEMPLATE_CODE,
)

#: The single approvable document type in this app, and its human labels.
DOCUMENT_TYPE = "payments.payout_batch"
TEMPLATE_NAME = "Payout-batch approval"
TEMPLATE_LABEL = "payout batch"


def _default_stages_payload(
    *, approve_role_key: str, high_value_role_key: str, high_value_threshold: int,
) -> list:
    """The two-stage ladder shared by platform and per-tenant provisioning.

    The checker stage always runs. The senior stage runs when the batch total reaches
    the configured high-value threshold. Provider-bound enforcement independently
    requires distinct human actors, so a weakened template cannot weaken cash-out.

    Two properties are carried over from procurement on purpose.

    ``skip_if_no_approvers=False``: money must never approve itself. When nobody holds
    the approving role the engine activates the stage with an empty approver
    snapshot and the batch *parks* rather than reaching a terminal APPROVED decision
    with no human involved. That is the safe failure, and it is the reason seeding is
    safe to run before anybody has been appointed. Parking is not a dead end: the
    engine's repair (:mod:`vs_workflow.services.parking`) makes the batch actionable as
    soon as somebody is appointed. Payout handlers deliberately forbid continuing
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
            "approver_source": "ROLE",
            "approver_role_key": approve_role_key,
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
            "approver_source": "ROLE",
            "approver_role_key": high_value_role_key,
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


def ensure_default_approval_templates(
    *,
    approve_role_key: str = WF_DEFAULT_APPROVE_ROLE,
    high_value_role_key: str = WF_DEFAULT_HIGH_VALUE_ROLE,
    high_value_threshold: int = WF_DEFAULT_HIGH_VALUE_THRESHOLD,
    created_by=None,
):
    """Publish (idempotently) the **platform-wide** default payout-batch ladder.

    The last-resort fallback, not a tenant's rules: platform-scoped
    (``tenant=None, branch=None``) so no entity is left with an unroutable batch, and
    a tenant's own template overrides it through the engine's cascade. Since the stages
    never auto-skip, falling back here parks a batch rather than paying it, so the
    fallback is safe to keep in place.

    Re-running upserts one shared row that every tenant without its own template reads,
    which is why only platform-level provisioning may call it. Returns the published :class:`~vs_workflow.models.WorkflowTemplate`.
    """
    from vs_workflow.services.templates import publish_template

    return publish_template(
        tenant=None, branch=None, document_type=DOCUMENT_TYPE,
        code=WF_DEFAULT_TEMPLATE_CODE, name=TEMPLATE_NAME,
        description=f"Default approval rule for a {TEMPLATE_LABEL}.",
        created_by=created_by,
        stages_payload=_default_stages_payload(
            approve_role_key=approve_role_key,
            high_value_role_key=high_value_role_key,
            high_value_threshold=high_value_threshold,
        ),
    )


def ensure_tenant_approval_templates(
    tenant,
    *,
    approve_role_key: str = WF_DEFAULT_APPROVE_ROLE,
    high_value_role_key: str = WF_DEFAULT_HIGH_VALUE_ROLE,
    high_value_threshold: int = WF_DEFAULT_HIGH_VALUE_THRESHOLD,
    created_by=None,
):
    """Give one tenant its **own** payout-approval rules. Returns ``(template, created)``.

    Every tenant sharing one platform ladder means one tenant's administrator editing
    the approving role changes how every other tenant's payouts are
    approved. A tenant-scoped template (``tenant=<tenant>, branch=None``) wins over the
    platform row through the engine's own cascade, and nothing outside this tenant can
    reach it.

    **Non-destructive.** A tenant that already has its own ladder is left exactly as it
    is and reported with ``created=False``: re-running after an administrator pointed
    the stage at a different role must not quietly restore the defaults. (Contrast :func:`ensure_default_approval_templates`, which upserts,
    because the platform row is provisioning's to own.)

    **Seeded blocked, not seeded open.** The rules arrive with no approver attached, so
    the first batch submitted parks and says so instead of paying itself out. Safe for
    onboarding to call on every tenant creation, and for an administrator to call again.
    """
    from vs_workflow.models import WorkflowTemplate
    from vs_workflow.services.roles import ensure_approver_role
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

    # A tenant-scoped ROLE stage will not publish against a role key the tenant does
    # not have, and a brand-new tenant has no roles at all. Create the role (holder-
    # less) so seeding works on a fresh tenant without inventing approval authority.
    for role_key, label in (
        (approve_role_key, "payout batches"),
        (high_value_role_key, "high-value payout batches"),
    ):
        ensure_approver_role(
            tenant, role_key,
            description=f"Approves {label}. Nobody holds it until an administrator "
                        "assigns someone, so batches park until then.",
        )

    return publish_template(
        tenant=tenant, branch=None, document_type=DOCUMENT_TYPE,
        code=WF_DEFAULT_TEMPLATE_CODE, name=TEMPLATE_NAME,
        description=f"Approval rule for a {TEMPLATE_LABEL}.",
        created_by=created_by,
        stages_payload=_default_stages_payload(
            approve_role_key=approve_role_key,
            high_value_role_key=high_value_role_key,
            high_value_threshold=high_value_threshold,
        ),
    ), True
