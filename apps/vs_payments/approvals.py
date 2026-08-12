"""Approval-rule provisioning for bulk payout batches.

The maker-checker *machinery* for a :class:`~vs_payments.models.PayoutBatch` already
exists: :mod:`vs_payments.workflow_handlers` gates the provider submission behind
``vs_workflow`` approval, and the submit-for-approval endpoint routes a batch into it.
What was missing is the thing that turns the gate on.

The gate is opt-in by template: :func:`vs_finance.approvals.approval_required` asks
whether a ``payments.payout_batch`` WorkflowTemplate exists for the batch's scope, and
with no template a batch goes straight to the provider. So an unconfigured install did
not have a jammed door on its highest-risk cash-out path, it had an open one, and a
single person could push a whole batch to the bank unreviewed. This module publishes
the ladder that closes it.

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

from .constants import WF_DEFAULT_APPROVE_PERMISSION, WF_DEFAULT_TEMPLATE_CODE

#: The single approvable document type in this app, and its human labels.
DOCUMENT_TYPE = "payments.payout_batch"
TEMPLATE_NAME = "Payout-batch approval"
TEMPLATE_LABEL = "payout batch"


def _default_stages_payload(*, approve_permission: str) -> list:
    """The one-stage ladder, shared by the platform and per-tenant seeds.

    A single always-on APPROVAL stage: any holder of ``approve_permission`` can approve,
    and that decision releases the batch.

    **Why one stage and not two.** This started as a threshold-gated pair, mirroring
    procurement: an ordinary stage plus a senior one for large runs. A second signature
    is only a real control when a second *person* holds the senior key, and in practice
    the same small finance team held both, so the extra stage bought an extra click
    rather than an extra reviewer, while doubling the ways a batch could park. The
    senior permission key is still registered, so a tenant that genuinely separates
    signing authority can publish its own ladder with that stage restored.

    Two properties are carried over from procurement on purpose.

    ``skip_if_no_approvers=False``: money must never approve itself. When nobody holds
    the approving permission the engine activates the stage with an empty approver
    snapshot and the batch *parks* rather than reaching a terminal APPROVED decision
    with no human involved. That is the safe failure, and it is the reason seeding is
    safe to run before anybody has been appointed. Parking is not a dead end: the
    engine's repair (:mod:`vs_workflow.services.parking`) releases the batch as soon as
    somebody is granted the key, and the audited override releases it when nobody can be.

    ``advance_rule="ANY"`` and ``on_rejection="TERMINAL"``: one holder's vote carries
    the stage, and a rejection ends the attempt rather than routing onwards.

    The one deliberate divergence from procurement is ``approver_scope="SCHOOL"`` where
    it uses ``"BRANCH"``. A payout batch is entity-scoped and its ``branch`` property is
    always ``None``, so the engine would forward ``None`` under either value and resolve
    to tenant-wide holders identically. SCHOOL is simply the honest declaration of what
    this document is, rather than a branch claim the model cannot back.
    """
    return [
        {
            "code": "approver",
            "label": "Payout approval",
            "kind": "APPROVAL",
            "order": 10,
            "approver_permission_key": approve_permission,
            # Batches carry no branch; see the docstring above.
            "approver_scope": "SCHOOL",
            "advance_rule": "ANY",
            "on_rejection": "TERMINAL",
            # Never auto-skip: an unstaffed stage must park the batch, not let it
            # pay itself out.
            "skip_if_no_approvers": False,
        },
    ]


def ensure_default_approval_templates(
    *,
    approve_permission: str = WF_DEFAULT_APPROVE_PERMISSION,
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
        stages_payload=_default_stages_payload(approve_permission=approve_permission),
    )


def ensure_tenant_approval_templates(
    tenant,
    *,
    approve_permission: str = WF_DEFAULT_APPROVE_PERMISSION,
    created_by=None,
):
    """Give one tenant its **own** payout-approval rules. Returns ``(template, created)``.

    Every tenant sharing one platform ladder means one tenant's administrator editing
    the permission key changes how every other tenant's payouts are
    approved. A tenant-scoped template (``tenant=<tenant>, branch=None``) wins over the
    platform row through the engine's own cascade, and nothing outside this tenant can
    reach it.

    **Non-destructive.** A tenant that already has its own ladder is left exactly as it
    is and reported with ``created=False``: re-running after an administrator pointed the stage at a different permission
    must not quietly restore the defaults. (Contrast :func:`ensure_default_approval_templates`, which upserts,
    because the platform row is provisioning's to own.)

    **Seeded blocked, not seeded open.** The rules arrive with no approver attached, so
    the first batch submitted parks and says so instead of paying itself out. Safe for
    onboarding to call on every tenant creation, and for an administrator to call again.
    """
    from vs_workflow.models import WorkflowTemplate
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

    return publish_template(
        tenant=tenant, branch=None, document_type=DOCUMENT_TYPE,
        code=WF_DEFAULT_TEMPLATE_CODE, name=TEMPLATE_NAME,
        description=f"Approval rule for a {TEMPLATE_LABEL}.",
        created_by=created_by,
        stages_payload=_default_stages_payload(approve_permission=approve_permission),
    ), True
