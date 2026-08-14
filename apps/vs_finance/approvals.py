"""The approval opt-in gate for finance documents.  # Decide whether a finance document must go through workflow.

Finance approvals are **opt-in by template** (design §7): a document type is
approval-gated *iff* a :class:`~vs_workflow.models.WorkflowTemplate` exists for it
at the document's ``(school, branch)`` scope, with the same branch → school →
platform cascade the engine's ``submit_for_approval`` uses. When no template
exists, the direct-post path behaves exactly as it did before - so approvals can
be switched on one document type and one school at a time, with zero migration.  # Keep the gate template-driven.

:func:`approval_required` is the single place that decision is made; both the
submit endpoint and the direct-post view read it so they can never disagree.  # Single source of truth.
"""
from __future__ import annotations


# Handle the approval required workflow.
def approval_required(document) -> bool:
    """Return ``True`` iff ``document`` must go through workflow approval.

    True when a published :class:`~vs_workflow.models.WorkflowTemplate` exists for
    the document's ``workflow_document_type`` at its ``(school, branch)`` scope -
    matched with the same branch-specific → school-wide → platform-wide cascade as
    :func:`vs_workflow.services.submission.submit_for_approval`, so the gate and the
    engine always resolve the same template. ``False`` when the document declares no
    ``workflow_document_type`` or no matching template is published.

    ``WorkflowTemplate`` is imported lazily to avoid an import cycle at app load.
    """
    document_type = getattr(document, "workflow_document_type", None)  # Read the document type if the model exposes one.
    if not document_type:  # Documents without a workflow type never require approval.
        return False

    from vs_workflow.models import WorkflowTemplate

    # Direct tenant attribute wins - including an explicit None (platform
    # documents gate on the platform template only). Finance documents scope
    # through their ledger entity's owning tenant. Must resolve identically to
    # submission.submit_for_approval or the gate and engine disagree.
    if hasattr(document, "tenant"):
        tenant = document.tenant
    else:
        tenant = getattr(getattr(document, "entity", None), "tenant", None)
    branch = getattr(document, "branch", None)  # The document's branch scope, if any.

    # Cascade: branch-specific → tenant-wide → platform-wide (mirrors submission.py).  # Match the workflow engine order.
    scopes = [{"tenant": tenant, "branch": branch}]  # Start with the most specific scope.
    if branch is not None:  # Fall back to tenant-wide when a branch is present.
        scopes.append({"tenant": tenant, "branch": None})  # Add the tenant-wide scope.
    if tenant is not None or branch is not None:  # Finally fall back to platform-wide.
        scopes.append({"tenant": None, "branch": None})  # Add the global scope.

    for scope in scopes:  # Test each scope in order until a template is found.
        if WorkflowTemplate.objects.filter(document_type=document_type, **scope).exists():
            return True  # Approval is required when a matching template exists.
    return False  # No matching template means direct posting stays allowed.


# --------------------------------------------------------------------------- #
# Default ladder provisioning                                                  #
# --------------------------------------------------------------------------- #

#: Doc-type token → (human label, template name, threshold-gated?) for the seeds.
#:
#: Refunds and write-offs are always gated: one moves cash out, the other concedes
#: income, and neither has a size at which a second pair of eyes stops being worth it.
#: Concessions and credit notes are gated only above a threshold, because a ₦2,000
#: goodwill allowance should not need a meeting and a ₦400,000 waiver should.
_ADJUSTMENT_TEMPLATES = {
    "finance.refund": ("refund", "Refund approval", False),
    "finance.write_off": ("write-off", "Write-off approval", False),
    "finance.concession": ("concession", "Concession approval", True),
    "finance.credit_note": ("credit note", "Credit-note approval", True),
}


def _adjustment_models():
    """The finance documents these ladders route, keyed by their workflow type."""
    from .models import Concession, CreditNote, Refund, WriteOffRequest

    return {m.workflow_document_type: m for m in
            (Refund, WriteOffRequest, Concession, CreditNote)}


def _stages_payload(*, amount_field, threshold, gated, approver_role_key,
                    senior_role_key):
    """The stage list for one adjustment ladder.

    An always-gated type gets a single always-on stage. A threshold-gated type gets
    the same first stage plus a senior stage whose ``inclusion_condition`` only admits
    documents at or above ``threshold``, which is the engine's own mechanism and the
    same shape procurement uses for high-value spend.

    ``skip_if_no_approvers=False`` on every stage: an adjustment must never approve
    itself because nobody happens to hold the role. An unstaffed stage parks the
    document and names the role to fill, and the engine's repair releases it the
    moment somebody is appointed.
    """
    stages = [{
        "code": "approver",
        "label": "Adjustment approval",
        "kind": "APPROVAL",
        "order": 10,
        "approver_source": "ROLE",
        "approver_role_key": approver_role_key,
        # Receivable adjustments are entity-scoped; a customer is not a branch.
        "approver_scope": "SCHOOL",
        "advance_rule": "ANY",
        "on_rejection": "TERMINAL",
        "skip_if_no_approvers": False,
    }]
    if not gated:
        return stages
    stages.append({
        "code": "senior",
        "label": "Senior adjustment approval",
        "kind": "APPROVAL",
        "order": 20,
        "approver_source": "ROLE",
        "approver_role_key": senior_role_key,
        "approver_scope": "SCHOOL",
        "advance_rule": "ANY",
        "on_rejection": "TERMINAL",
        "skip_if_no_approvers": False,
        "inclusion_condition": {
            "op": "gte", "field": amount_field, "value": int(threshold),
        },
    })
    return stages


def ensure_tenant_approval_templates(
    tenant,
    *,
    threshold: int | None = None,
    approver_role_key: str | None = None,
    senior_role_key: str | None = None,
    created_by=None,
) -> list:
    """Give one tenant its own adjustment-approval rules. Returns ``[(template, created)]``.

    Publishes one ladder per adjustment document type. **Non-destructive**: a document
    type that already has a tenant-scoped template is reported with ``created=False``
    and left exactly as an administrator configured it, so this is safe to re-run and
    safe to call again for a second entity in the same tenant.

    **Seeded blocked, not seeded open.** The approving roles are created with nobody
    appointed, so the first adjustment submitted parks and says which role to fill
    rather than approving itself.

    Until this existed, finance published no ladders at all. Refunds and write-offs
    carried a submit endpoint and a handler, but with no template ``approval_required``
    answered False and both posted directly - the gate was built and never switched on.
    """
    from vs_workflow.models import WorkflowTemplate
    from vs_workflow.services.roles import ensure_approver_role
    from vs_workflow.services.templates import publish_template

    from .constants import (
        WF_ADJUSTMENT_APPROVER_ROLE,
        WF_ADJUSTMENT_THRESHOLD,
        WF_DEFAULT_TEMPLATE_CODE,
        WF_SENIOR_ADJUSTMENT_APPROVER_ROLE,
    )

    if tenant is None:
        raise ValueError("A tenant is required to seed its adjustment-approval rules.")

    threshold = WF_ADJUSTMENT_THRESHOLD if threshold is None else threshold
    approver_role_key = approver_role_key or WF_ADJUSTMENT_APPROVER_ROLE
    senior_role_key = senior_role_key or WF_SENIOR_ADJUSTMENT_APPROVER_ROLE

    models = _adjustment_models()
    document_types = list(_ADJUSTMENT_TEMPLATES)
    # all_objects deliberately: the explicit tenant filter is the boundary, and a row
    # hidden by ambient request-local scoping would be re-published over, which is the
    # destructive outcome this function promises never to cause.
    existing = {
        t.document_type: t
        for t in WorkflowTemplate.all_objects.filter(
            tenant=tenant, branch=None, code=WF_DEFAULT_TEMPLATE_CODE,
            document_type__in=document_types,
        )
    }

    # A tenant-scoped ROLE stage will not publish against a role the tenant does not
    # have, and a brand-new tenant has no roles at all. Create them holder-less.
    ensure_approver_role(
        tenant, approver_role_key,
        description="Approves receivable adjustments. Nobody holds it until an "
                    "administrator assigns someone, so adjustments park until then.",
    )
    ensure_approver_role(
        tenant, senior_role_key,
        description="Approves high-value concessions and credit notes. Nobody holds "
                    "it until an administrator assigns someone.",
    )

    results = []
    for document_type, (label, name, gated) in _ADJUSTMENT_TEMPLATES.items():
        if document_type in existing:
            results.append((existing[document_type], False))
            continue
        results.append((
            publish_template(
                tenant=tenant, branch=None, document_type=document_type,
                code=WF_DEFAULT_TEMPLATE_CODE, name=name,
                description=f"Approval rule for a {label}.",
                created_by=created_by,
                stages_payload=_stages_payload(
                    amount_field=models[document_type].workflow_amount_field,
                    threshold=threshold, gated=gated,
                    approver_role_key=approver_role_key,
                    senior_role_key=senior_role_key,
                ),
            ),
            True,
        ))
    return results
