"""Spend-approval services - the bridge between procurement docs and ``vs_workflow``.

Requisitions, purchase orders and vendor invoices are *submitted* into the generic
``vs_workflow`` engine instead of being approved at a direct endpoint. The engine runs
a per-document-type template whose stages are **threshold-gated** (a senior stage only
runs when the document's amount clears a configurable bar), resolves approvers via RBAC,
collects their votes, and - on a terminal decision - calls back into the registered
handler (see :mod:`vs_procurement.workflow_handlers`). Those callbacks land here:

* :func:`apply_approved`   - the workflow fully approved the document.
* :func:`apply_rejected`   - the workflow terminally rejected it.
* :func:`reset_pending`    - the requester withdrew / an admin cancelled it.
* :func:`reset_to_pending` - an admin reversed a vote and the decision is undone.

:func:`submit_for_approval` is the hand-off the API calls.
:func:`ensure_tenant_approval_templates` gives one tenant its own approval rules, and
:func:`ensure_default_approval_templates` publishes the platform-wide fallback so no
:class:`~vs_finance.models.LedgerEntity` is ever left unroutable; a tenant's own rules
(and a branch's, where a site's rules genuinely differ) override it through the engine's
branch → tenant → platform cascade.

This module touches **no GL** - ``approval_state`` is a governance overlay independent of
the ledger ``status``; for a requisition the approval also drives the existing
``DocumentStatus`` gate that PO creation depends on.
"""
from __future__ import annotations

from django.db import transaction

from vs_finance.audit import record
from vs_finance.constants import DocumentStatus, FinanceAuditAction

from .constants import (
    ProcApprovalState,
    WF_DEFAULT_MANAGER_GROUP,
    WF_DEFAULT_SENIOR_GROUP,
    WF_DEFAULT_SENIOR_THRESHOLD,
    WF_DEFAULT_TEMPLATE_CODE,
)
from .exceptions import ApprovalTemplateMissingError, ApprovalWorkflowError


# --------------------------------------------------------------------------- #
# Default template provisioning                                               #
# --------------------------------------------------------------------------- #

#: Doc-type token → (human label, template name) for the seeded defaults.
_TEMPLATE_META = {
    "procurement.requisition": ("requisition", "Requisition approval"),
    "procurement.purchase_order": ("purchase order", "Purchase-order approval"),
    "procurement.vendor_invoice": ("vendor invoice", "Vendor-invoice approval"),
    "procurement.vendor_payment": ("vendor payment", "Vendor-payment approval"),
}


def _doc_models():
    """Return the closed set of procurement models the workflow bridge accepts.

    Lazy imports avoid the model/handler/service cycle during Django app loading.
    """
    from .models import PurchaseOrder, PurchaseRequisition, VendorInvoice, VendorPayment

    return (PurchaseRequisition, PurchaseOrder, VendorInvoice, VendorPayment)


def _default_stages_payload(
    amount_field: str, *, threshold: int, manager_group_code: str, senior_group_code: str,
) -> list:
    """The two-stage ladder a tenant is seeded with.

    * **manager** - always runs; anybody in the ``manager_group_code`` group approves.
    * **senior**  - gated by ``inclusion_condition`` ``amount >= threshold`` (kobo), so
      only high-value documents escalate to the ``senior_group_code`` group.

    Both stages name their approver by approver *group*, which is why this ladder is
    tenant-scoped and the platform row carries no steps at all: a group belongs to one
    tenant, so a shared row could not name one. A tenant composes each group from its
    own people, roles or org-chart positions, and changing who approves is one screen
    rather than a permission-model conversation.

    Both stages set ``skip_if_no_approvers=False``: spend must never approve itself.
    When the approving group is empty the engine activates the stage with an empty
    approver snapshot and the document *parks* at IN_PROGRESS instead of reaching a
    terminal APPROVED decision with no human involved. Parked work is made reachable
    again by the workflow parking service once somebody joins the group.

    Both stages are ``approver_scope="BRANCH"``, which is what makes a multi-site
    tenant route correctly: the engine forwards the *document's own* branch to the
    approver lookup, so a request raised at one site resolves to that site's approvers
    plus anybody eligible tenant-wide, and never to another site's approvers. A
    document with no branch (raised for the entity as a whole) forwards ``None`` and
    therefore resolves to tenant-wide holders only, which is also exactly what a
    tenant with no branches at all does.
    """
    return [
        {
            "code": "manager",
            "label": "Manager approval",
            "kind": "APPROVAL",
            "order": 10,
            "approver_source": "WORKFLOW_GROUP",
            "approver_group_code": manager_group_code,
            # Route by the document's own branch - see the docstring above.
            "approver_scope": "BRANCH",
            "advance_rule": "ANY",
            "on_rejection": "TERMINAL",
            # Never auto-skip: an unstaffed stage must park the document, not
            # let it approve itself.
            "skip_if_no_approvers": False,
        },
        {
            "code": "senior",
            "label": "Senior approval",
            "kind": "APPROVAL",
            "order": 20,
            "approver_source": "WORKFLOW_GROUP",
            "approver_group_code": senior_group_code,
            "approver_scope": "BRANCH",
            "advance_rule": "ANY",
            "on_rejection": "TERMINAL",
            # Never auto-skip - see the manager stage above.
            "skip_if_no_approvers": False,
            "inclusion_condition": {
                "op": "gte", "field": amount_field, "value": int(threshold),
            },
        },
    ]


def ensure_default_approval_templates(*, created_by=None) -> list:
    """Publish the **platform-wide** route for each approvable type, with no steps.

    One row per document type, platform-scoped (``tenant=None, branch=None``) so no
    document is left unroutable; a branch- or tenant-scoped template wins over it
    through the engine's branch → tenant → platform cascade.

    It takes no approver or threshold arguments because it publishes nobody to approve
    against. A step here would have to name the same authority in every tenant at
    once, which only a role key can do, and minting that role in every tenant is what
    this stopped doing. A tenant's actual steps come from
    :func:`ensure_tenant_approval_templates` and name that tenant's own approver
    groups.

    A document resolving here therefore finds no steps and is refused with
    :class:`~vs_workflow.exceptions.ApprovalNotConfiguredError` rather than approved:
    the tenant either builds its ladder or confirms the post deliberately, and the
    confirmation is recorded against whoever gives it.

    Re-running upserts in place, so it is safe to seed often. Returns the published
    :class:`~vs_workflow.models.WorkflowTemplate` objects.
    """
    from vs_workflow.services.templates import publish_template

    published = []
    for model in _doc_models():
        document_type = model.workflow_document_type
        amount_field = model.workflow_amount_field
        label, name = _TEMPLATE_META[document_type]
        template = publish_template(
            tenant=None, branch=None, document_type=document_type,
            code=WF_DEFAULT_TEMPLATE_CODE, name=name,
            description=f"Default threshold-gated approval ladder for a {label}.",
            created_by=created_by,
            # No steps. A shared row is one template every tenant runs, and a
            # step on it cannot name an approver group - a group belongs to one
            # tenant. A tenant that has not built its own ladder resolves here,
            # finds no steps, and is asked to confirm rather than approved by a
            # ladder it never chose. See the note in vs_payments.approvals.
            stages_payload=[],
        )
        published.append(template)
    return published


def ensure_tenant_approval_templates(
    tenant,
    *,
    threshold: int = WF_DEFAULT_SENIOR_THRESHOLD,
    manager_group_code: str = WF_DEFAULT_MANAGER_GROUP,
    senior_group_code: str = WF_DEFAULT_SENIOR_GROUP,
    created_by=None,
) -> list:
    """Give one tenant its **own** approval rules. Returns ``[(template, created), ...]``.

    Every tenant sharing a single platform-wide ladder means one tenant's administrator
    editing the threshold, the approving roles or the stage list changes how *every*
    other tenant's spend is approved. This is the seam that ends that: a tenant-scoped
    template (``tenant=<tenant>, branch=None``) wins over the platform row through the
    engine's own cascade, and nothing outside this tenant can reach it.

    Two properties are deliberate and tested:

    * **Idempotent, and never destructive.** A document type that already has a
      tenant-scoped template is left exactly as it is and reported with
      ``created=False`` - re-running after an administrator has customised the ladder
      must not quietly restore the defaults. (Contrast
      :func:`ensure_default_approval_templates`, which upserts, because the platform
      row is provisioning's to own.)
    * **Seeded blocked, not seeded open.** The rules arrive with no approver attached:
      the stages resolve approvers through a named role, and a new tenant has
      assigned that role to nobody, so the first submitted document parks and says
      so instead of approving itself. Procurement stays deliberately blocked until the
      tenant appoints someone, which is the secure-by-default order of operations.

    A branch never needs its own template for routing: the stages are branch-scoped
    approvers over one tenant ladder (see :func:`_default_stages_payload`). Publishing a
    branch-specific template stays available for a site whose *rules* genuinely differ,
    and still wins over this one.

    Safe for onboarding to call on every tenant creation, and for an administrator to
    call again later.
    """
    from vs_workflow.models import WorkflowTemplate
    from vs_workflow.services.groups import ensure_approver_group
    from vs_workflow.services.templates import publish_template

    if tenant is None:
        raise ApprovalWorkflowError("A tenant is required to seed its approval rules.")

    # A stage will not publish against a group the tenant does not have, and a
    # brand-new tenant has none. Create them empty, so seeding works on a fresh
    # tenant without inventing approval authority.
    for group_code, what in ((manager_group_code, "ordinary spend"),
                             (senior_group_code, "high-value spend")):
        ensure_approver_group(
            tenant, group_code,
            description=f"Approves {what}. Empty until the tenant puts "
                        "somebody in it, so documents park until then.",
        )

    document_types = [model.workflow_document_type for model in _doc_models()]
    # One query for the whole set: which of this tenant's ladders already exist.
    # all_objects deliberately: the explicit tenant filter is the boundary, and a row
    # hidden by ambient request-local scoping would be re-published over, which is
    # exactly the destructive outcome this function promises never to cause.
    existing = {
        template.document_type: template
        for template in WorkflowTemplate.all_objects.filter(
            tenant=tenant, branch=None, code=WF_DEFAULT_TEMPLATE_CODE,
            document_type__in=document_types,
        )
    }

    results = []
    for model in _doc_models():
        document_type = model.workflow_document_type
        label, name = _TEMPLATE_META[document_type]
        if document_type in existing:
            results.append((existing[document_type], False))
            continue
        results.append((
            publish_template(
                tenant=tenant, branch=None, document_type=document_type,
                code=WF_DEFAULT_TEMPLATE_CODE, name=name,
                description=f"Threshold-gated approval ladder for a {label}.",
                created_by=created_by,
                stages_payload=_default_stages_payload(
                    model.workflow_amount_field, threshold=threshold,
                    manager_group_code=manager_group_code,
                    senior_group_code=senior_group_code,
                ),
            ),
            True,
        ))
    return results


# --------------------------------------------------------------------------- #
# Submission hand-off                                                          #
# --------------------------------------------------------------------------- #

def _label(document) -> str:
    """Build an audit/error label without assuming numbering has already run."""
    return f"{type(document).__name__} {document.document_number or document.pk}"


def _no_template_message(document) -> str:
    """Explain a missing approval route in the submitter's language, not the engine's.

    Names the document kind from :data:`_TEMPLATE_META` and the remedy. Deliberately
    free of engine vocabulary (template codes, ``document_type`` tokens) and of any
    domain vocabulary a non-procurement product would not share.
    """
    document_type = getattr(document, "workflow_document_type", "")
    label = _TEMPLATE_META.get(document_type, ("document", ""))[0]
    return (
        f"No approval route is set up for a {label}, so it cannot be submitted. "
        f"Ask an administrator to configure procurement approvals first."
    )


@transaction.atomic
def submit_for_approval(document, *, actor_user, template_code: str | None = None):
    """Hand ``document`` to ``vs_workflow`` for approval and mark it PENDING.

    Flips ``approval_state`` to PENDING (and, for a requisition, the ledger ``status``
    DRAFT → PENDING_APPROVAL for parity), then creates the workflow instance. Raises
    :class:`ApprovalWorkflowError` if the document is already PENDING or APPROVED.

    This is the single choke point every procurement submit view funnels through
    (requisition, purchase order, vendor invoice, vendor payment), so the three
    configuration outcomes are separated here once rather than in four views:

    * **No template at all** - a genuine configuration failure. The engine's
      :class:`~vs_workflow.exceptions.TemplateNotFoundError` names internal template
      codes and document-type tokens, so it is translated into
      :class:`ApprovalTemplateMissingError`. Because it is raised inside this atomic
      block *after* the document write, the PENDING flip rolls back: a refused submit
      creates nothing.
    * **A template with no steps** - the tenant has not built its ladder and resolved
      to the shared platform row, which carries none. The engine refuses with
      :class:`~vs_workflow.exceptions.ApprovalNotConfiguredError` rather than treating
      an empty ladder as approval. The caller may retry with an explicit confirmation,
      which is recorded against whoever gives it.
    * **A template whose steps nobody can currently satisfy** - not an error.
      The document is submitted and parks on its unstaffed stage at IN_PROGRESS until
      somebody joins the approver group the stage names (see
      :mod:`vs_procurement.approval_parking`). Spend never approves itself.

    Returns the :class:`~vs_workflow.models.WorkflowInstance`.
    """
    from vs_workflow.exceptions import TemplateNotFoundError
    from vs_workflow.services.submission import submit_for_approval as wf_submit

    state = getattr(document, "approval_state", None)
    if state in (ProcApprovalState.PENDING, ProcApprovalState.APPROVED):
        raise ApprovalWorkflowError(
            f"{_label(document)} is already '{state}' for approval.",
        )
    if actor_user is None:
        raise ApprovalWorkflowError("An actor (requested_by) is required to submit for approval.")

    update_fields = ["approval_state", "updated_at"]
    document.approval_state = ProcApprovalState.PENDING

    # Requisition parity: keep the ledger status in step so the existing PO-creation
    # gate (requires APPROVED) reads correctly through the workflow.
    from .models import PurchaseRequisition

    if isinstance(document, PurchaseRequisition) and document.status == DocumentStatus.DRAFT:
        document.recompute_total(save=False)
        document.status = DocumentStatus.PENDING_APPROVAL
        update_fields += ["status", "estimated_total"]

    document.save(update_fields=update_fields)
    try:
        return wf_submit(document, actor_user, template_code=template_code)
    except TemplateNotFoundError as exc:
        raise ApprovalTemplateMissingError(_no_template_message(document)) from exc


# --------------------------------------------------------------------------- #
# Workflow callbacks (invoked from workflow_handlers)                          #
# --------------------------------------------------------------------------- #

def apply_approved(document, *, actor_user=None) -> None:
    """Apply a fully-approved workflow outcome to ``document``.

    Sets ``approval_state`` APPROVED, then runs the document-type effect: a requisition
    advances to ``DocumentStatus.APPROVED`` (so a PO can be raised), a PO likewise, a
    vendor invoice records an approval audit, and a vendor payment becomes postable.
    Invoice/payment ledger status remains independent from workflow approval: governance
    makes them eligible to post but never manufactures a journal here.
    """
    from .models import PurchaseOrder, PurchaseRequisition, VendorInvoice
    from .purchasing import approve_purchase_order, approve_requisition

    document.approval_state = ProcApprovalState.APPROVED
    document.save(update_fields=["approval_state", "updated_at"])

    if isinstance(document, PurchaseRequisition):
        approve_requisition(document, actor_user=actor_user)
    elif isinstance(document, PurchaseOrder):
        approve_purchase_order(document, actor_user=actor_user)
        from .po_email import release_after_commit
        release_after_commit(document.pk, actor_user=actor_user)
    elif isinstance(document, VendorInvoice):
        record(
            entity=document.entity, action=FinanceAuditAction.VENDOR_INVOICE_APPROVED,
            actor_user=actor_user, target=document,
            message=f"Approved vendor invoice {document.document_number or document.pk}.",
        )


def apply_rejected(document, *, reason: str = "", actor_user=None) -> None:
    """Apply a terminal-rejection workflow outcome to ``document``.

    Sets ``approval_state`` REJECTED and, for a requisition that was sitting in
    PENDING_APPROVAL, cancels the ledger document (there is no REJECTED ledger status).
    Other document types retain their independent ledger status and only change the
    approval overlay.
    """
    from .models import PurchaseOrder, PurchaseRequisition

    update_fields = ["approval_state", "updated_at"]
    document.approval_state = ProcApprovalState.REJECTED
    if isinstance(document, PurchaseRequisition) and document.status == DocumentStatus.PENDING_APPROVAL:
        document.status = DocumentStatus.CANCELLED
        update_fields.append("status")
    document.save(update_fields=update_fields)
    if isinstance(document, PurchaseOrder):
        from .po_email import cancel_awaiting
        cancel_awaiting(document, reason=reason or "Approval was rejected.", actor_user=actor_user)


def reset_to_pending(document) -> None:
    """Return a decided document to PENDING after its approving vote is reversed.

    The mirror of :func:`apply_approved` and :func:`apply_rejected`: those wrote a
    decision onto the document, and an administrator reversing the vote behind it
    withdraws that decision. Without this the overlay keeps saying APPROVED while
    the workflow shows the document back under review, and the PO-creation gate
    reads the stale half.

    Distinct from :func:`reset_pending`, which unwinds the *submission* and leaves
    the document NOT_SUBMITTED. Here the instance is still in flight, so the
    document goes back to waiting on it.
    """
    from .models import PurchaseOrder, PurchaseRequisition

    if getattr(document, "approval_state", None) not in (
        ProcApprovalState.APPROVED, ProcApprovalState.REJECTED,
    ):
        return
    update_fields = ["approval_state", "updated_at"]
    document.approval_state = ProcApprovalState.PENDING
    # A requisition's ledger status moved with the decision, so it moves back.
    if isinstance(document, (PurchaseRequisition, PurchaseOrder)) and document.status in (
        DocumentStatus.APPROVED, DocumentStatus.CANCELLED,
    ):
        document.status = DocumentStatus.PENDING_APPROVAL
        update_fields.append("status")
    document.save(update_fields=update_fields)


def reset_pending(document) -> None:
    """Return a PENDING document to NOT_SUBMITTED (requester withdrew / admin cancelled).

    Reverses the submission bookkeeping: a requisition's ledger status rolls back
    PENDING_APPROVAL → DRAFT so it can be edited and re-submitted.
    """
    from .models import PurchaseOrder, PurchaseRequisition

    if getattr(document, "approval_state", None) != ProcApprovalState.PENDING:
        return
    update_fields = ["approval_state", "updated_at"]
    document.approval_state = ProcApprovalState.NOT_SUBMITTED
    if isinstance(document, PurchaseRequisition) and document.status == DocumentStatus.PENDING_APPROVAL:
        document.status = DocumentStatus.DRAFT
        update_fields.append("status")
    document.save(update_fields=update_fields)
    if isinstance(document, PurchaseOrder):
        from .po_email import cancel_awaiting
        cancel_awaiting(document, reason="Approval request was withdrawn or cancelled.")
