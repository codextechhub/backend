"""Spend-approval services - the bridge between procurement docs and ``vs_workflow``.

Requisitions, purchase orders and vendor invoices are *submitted* into the generic
``vs_workflow`` engine instead of being approved at a direct endpoint. The engine runs
a per-document-type template whose stages are **threshold-gated** (a senior stage only
runs when the document's amount clears a configurable bar), resolves approvers via RBAC,
collects their votes, and - on a terminal decision - calls back into the registered
handler (see :mod:`vs_procurement.workflow_handlers`). Those callbacks land here:

* :func:`apply_approved`  - the workflow fully approved the document.
* :func:`apply_rejected`  - the workflow terminally rejected it.
* :func:`reset_pending`   - the requester withdrew / an admin cancelled it.

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
    WF_DEFAULT_MANAGER_ROLE,
    WF_DEFAULT_SENIOR_ROLE,
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
    amount_field: str, *, threshold: int, manager_role_key: str, senior_role_key: str,
) -> list:
    """The two-stage default ladder, shared by the platform and per-tenant seeds.

    * **manager** - always runs; any holder of the ``manager_role_key`` role can approve.
    * **senior**  - gated by ``inclusion_condition`` ``amount >= threshold`` (kobo), so
      only high-value documents escalate to a holder of the ``senior_role_key`` role.

    Both stages name their approver by role *key* rather than by a role reference,
    because the platform ladder is published without a tenant: the key is resolved
    inside whichever tenant raised the document, so one definition reaches that
    tenant's own approvers. A tenant may also repoint either stage to its own role or
    approver group without cloning the template.

    Both stages set ``skip_if_no_approvers=False``: spend must never approve itself.
    When nobody currently holds the approving role the engine activates the stage
    with an empty approver snapshot and the document *parks* at IN_PROGRESS instead of
    reaching a terminal APPROVED decision with no human involved. Parked work is made
    reachable again by the workflow parking service once the role is filled.

    Both stages are ``approver_scope="BRANCH"``, which is what makes a multi-site
    tenant route correctly: the engine forwards the *document's own* branch to the role
    lookup, so a request raised at one site resolves to that site's approvers plus
    anybody holding the role tenant-wide, and never to another site's approvers. A
    document with no branch (raised for the entity as a whole) forwards ``None`` and
    therefore resolves to tenant-wide holders only, which is also exactly what a
    tenant with no branches at all has always done.
    """
    return [
        {
            "code": "manager",
            "label": "Manager approval",
            "kind": "APPROVAL",
            "order": 10,
            "approver_source": "ROLE",
            "approver_role_key": manager_role_key,
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
            "approver_source": "ROLE",
            "approver_role_key": senior_role_key,
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


def ensure_default_approval_templates(
    *,
    threshold: int = WF_DEFAULT_SENIOR_THRESHOLD,
    manager_role_key: str = WF_DEFAULT_MANAGER_ROLE,
    senior_role_key: str = WF_DEFAULT_SENIOR_ROLE,
    created_by=None,
) -> list:
    """The two-stage default ladder, shared by the platform and per-tenant seeds.

    * **manager** - always runs; any holder of the ``manager_role_key`` role can approve.
    * **senior**  - gated by ``inclusion_condition`` ``amount >= threshold`` (kobo), so
      only high-value documents escalate to a holder of the ``senior_role_key`` role.

    Both stages name their approver by role *key*. These templates are central
    (no tenant), so the key is resolved inside whichever tenant raised the
    document; a tenant can repoint either stage to its own role or approver
    group without cloning the template.

    Templates are platform-scoped (``school=None, branch=None``) so they act as the
    universal fallback; a branch- or school-specific template still wins via the engine's
    branch → school → platform cascade. Re-running upserts in place (safe to seed often).
    ``threshold`` is integer kobo, matching every model's workflow amount field. Returns
    the published :class:`~vs_workflow.models.WorkflowTemplate` objects.
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
            stages_payload=_default_stages_payload(
                model.workflow_amount_field, threshold=threshold,
                manager_role_key=manager_role_key, senior_role_key=senior_role_key,
            ),
        )
        published.append(template)
    return published


def ensure_tenant_approval_templates(
    tenant,
    *,
    threshold: int = WF_DEFAULT_SENIOR_THRESHOLD,
    manager_role_key: str = WF_DEFAULT_MANAGER_ROLE,
    senior_role_key: str = WF_DEFAULT_SENIOR_ROLE,
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
    from vs_workflow.services.roles import ensure_approver_role
    from vs_workflow.services.templates import publish_template

    if tenant is None:
        raise ApprovalWorkflowError("A tenant is required to seed its approval rules.")

    # A tenant-scoped ROLE stage will not publish against a role key the tenant does
    # not have, and a brand-new tenant has no roles at all. Create both roles (holder-
    # less) so seeding works on a fresh tenant without inventing approval authority.
    for role_key, what in ((manager_role_key, "ordinary spend"),
                           (senior_role_key, "high-value spend")):
        ensure_approver_role(
            tenant, role_key,
            description=f"Approves {what}. Nobody holds it until an administrator "
                        "assigns someone, so documents park until then.",
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
                    manager_role_key=manager_role_key,
                    senior_role_key=senior_role_key,
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
    (requisition, purchase order, vendor invoice, vendor payment), so the two
    configuration outcomes are separated here once rather than in four views:

    * **No template at all** - a genuine configuration failure. The engine's
      :class:`~vs_workflow.exceptions.TemplateNotFoundError` names internal template
      codes and document-type tokens, so it is translated into
      :class:`ApprovalTemplateMissingError`. Because it is raised inside this atomic
      block *after* the document write, the PENDING flip rolls back: a refused submit
      creates nothing.
    * **A template exists but nobody holds the approving role** - not an error.
      The document is submitted and parks on its unstaffed stage at IN_PROGRESS until
      somebody is assigned the role (see
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
