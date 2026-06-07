"""Spend-approval services — the bridge between procurement docs and ``vs_workflow``.

Requisitions, purchase orders and vendor invoices are *submitted* into the generic
``vs_workflow`` engine instead of being approved at a direct endpoint. The engine runs
a per-document-type template whose stages are **threshold-gated** (a senior stage only
runs when the document's amount clears a configurable bar), resolves approvers via RBAC,
collects their votes, and — on a terminal decision — calls back into the registered
handler (see :mod:`vs_procurement.workflow_handlers`). Those callbacks land here:

* :func:`apply_approved`  — the workflow fully approved the document.
* :func:`apply_rejected`  — the workflow terminally rejected it.
* :func:`reset_pending`   — the requester withdrew / an admin cancelled it.

:func:`submit_for_approval` is the hand-off the API calls; :func:`ensure_default_approval_templates`
provisions the platform-wide default templates so any :class:`~vs_finance.models.LedgerEntity`
works out of the box (branch/school templates still override via the engine's cascade).

This module touches **no GL** — ``approval_state`` is a governance overlay independent of
the ledger ``status``; for a requisition the approval also drives the existing
``DocumentStatus`` gate that PO creation depends on.
"""
from __future__ import annotations

from django.db import transaction

from vs_finance.audit import record
from vs_finance.constants import DocumentStatus, FinanceAuditAction

from .constants import (
    ProcApprovalState,
    WF_DEFAULT_MANAGER_PERMISSION,
    WF_DEFAULT_SENIOR_PERMISSION,
    WF_DEFAULT_SENIOR_THRESHOLD,
    WF_DEFAULT_TEMPLATE_CODE,
)
from .exceptions import ApprovalWorkflowError


# --------------------------------------------------------------------------- #
# Default template provisioning                                               #
# --------------------------------------------------------------------------- #

#: Doc-type token → (human label, template name) for the seeded defaults.
_TEMPLATE_META = {
    "procurement.requisition": ("requisition", "Requisition approval"),
    "procurement.purchase_order": ("purchase order", "Purchase-order approval"),
    "procurement.vendor_invoice": ("vendor invoice", "Vendor-invoice approval"),
}


def _doc_models():
    """Return the three approvable model classes (imported lazily to dodge cycles)."""
    from .models import PurchaseOrder, PurchaseRequisition, VendorInvoice

    return (PurchaseRequisition, PurchaseOrder, VendorInvoice)


def ensure_default_approval_templates(
    *,
    threshold: int = WF_DEFAULT_SENIOR_THRESHOLD,
    manager_permission: str = WF_DEFAULT_MANAGER_PERMISSION,
    senior_permission: str = WF_DEFAULT_SENIOR_PERMISSION,
    created_by=None,
) -> list:
    """Publish (idempotently) the platform-wide default approval templates.

    One template per approvable document type, each a two-stage ladder:

    * **manager** — always runs; any holder of ``manager_permission`` can approve.
    * **senior**  — gated by ``inclusion_condition`` ``amount >= threshold`` (kobo), so
      only high-value documents escalate to a holder of ``senior_permission``.

    Templates are platform-scoped (``school=None, branch=None``) so they act as the
    universal fallback; a branch- or school-specific template still wins via the engine's
    branch → school → platform cascade. Re-running upserts in place (safe to seed often).
    Returns the published :class:`~vs_workflow.models.WorkflowTemplate` objects.
    """
    from vs_workflow.services.templates import publish_template

    published = []
    for model in _doc_models():
        document_type = model.workflow_document_type
        amount_field = model.workflow_amount_field
        label, name = _TEMPLATE_META[document_type]
        stages_payload = [
            {
                "code": "manager",
                "label": "Manager approval",
                "kind": "APPROVAL",
                "order": 10,
                "approver_permission_key": manager_permission,
                "approver_scope": "PLATFORM",
                "advance_rule": "ANY",
                "on_rejection": "TERMINAL",
                "skip_if_no_approvers": True,
            },
            {
                "code": "senior",
                "label": "Senior approval",
                "kind": "APPROVAL",
                "order": 20,
                "approver_permission_key": senior_permission,
                "approver_scope": "PLATFORM",
                "advance_rule": "ANY",
                "on_rejection": "TERMINAL",
                "skip_if_no_approvers": True,
                "inclusion_condition": {
                    "op": "gte", "field": amount_field, "value": int(threshold),
                },
            },
        ]
        template = publish_template(
            school=None, branch=None, document_type=document_type,
            code=WF_DEFAULT_TEMPLATE_CODE, name=name,
            description=f"Default threshold-gated approval ladder for a {label}.",
            created_by=created_by, stages_payload=stages_payload,
        )
        published.append(template)
    return published


# --------------------------------------------------------------------------- #
# Submission hand-off                                                          #
# --------------------------------------------------------------------------- #

def _label(document) -> str:
    return f"{type(document).__name__} {document.document_number or document.pk}"


@transaction.atomic
def submit_for_approval(document, *, actor_user, template_code: str | None = None):
    """Hand ``document`` to ``vs_workflow`` for approval and mark it PENDING.

    Flips ``approval_state`` to PENDING (and, for a requisition, the ledger ``status``
    DRAFT → PENDING_APPROVAL for parity), then creates the workflow instance. If the
    seeded template's stages all auto-skip (no eligible approvers), the engine may reach
    a terminal decision synchronously and the on-approved callback runs before this
    returns. Raises :class:`ApprovalWorkflowError` if the document is already PENDING or
    APPROVED. Returns the :class:`~vs_workflow.models.WorkflowInstance`.
    """
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
    return wf_submit(document, actor_user, template_code=template_code)


# --------------------------------------------------------------------------- #
# Workflow callbacks (invoked from workflow_handlers)                          #
# --------------------------------------------------------------------------- #

def apply_approved(document, *, actor_user=None) -> None:
    """Apply a fully-approved workflow outcome to ``document``.

    Sets ``approval_state`` APPROVED, then runs the document-type effect: a requisition
    advances to ``DocumentStatus.APPROVED`` (so a PO can be raised), a PO likewise, and a
    vendor invoice records an approval audit without touching its posting status.
    """
    from .models import PurchaseOrder, PurchaseRequisition, VendorInvoice
    from .purchasing import approve_purchase_order, approve_requisition

    document.approval_state = ProcApprovalState.APPROVED
    document.save(update_fields=["approval_state", "updated_at"])

    if isinstance(document, PurchaseRequisition):
        approve_requisition(document, actor_user=actor_user)
    elif isinstance(document, PurchaseOrder):
        approve_purchase_order(document, actor_user=actor_user)
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
    """
    from .models import PurchaseRequisition

    update_fields = ["approval_state", "updated_at"]
    document.approval_state = ProcApprovalState.REJECTED
    if isinstance(document, PurchaseRequisition) and document.status == DocumentStatus.PENDING_APPROVAL:
        document.status = DocumentStatus.CANCELLED
        update_fields.append("status")
    document.save(update_fields=update_fields)


def reset_pending(document) -> None:
    """Return a PENDING document to NOT_SUBMITTED (requester withdrew / admin cancelled).

    Reverses the submission bookkeeping: a requisition's ledger status rolls back
    PENDING_APPROVAL → DRAFT so it can be edited and re-submitted.
    """
    from .models import PurchaseRequisition

    if getattr(document, "approval_state", None) != ProcApprovalState.PENDING:
        return
    update_fields = ["approval_state", "updated_at"]
    document.approval_state = ProcApprovalState.NOT_SUBMITTED
    if isinstance(document, PurchaseRequisition) and document.status == DocumentStatus.PENDING_APPROVAL:
        document.status = DocumentStatus.DRAFT
        update_fields.append("status")
    document.save(update_fields=update_fields)
