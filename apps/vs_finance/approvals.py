"""The approval opt-in gate for finance documents.  # Decide whether a finance document must go through workflow.

Finance approvals are **opt-in by template** (design §7): a document type is
approval-gated *iff* a :class:`~vs_workflow.models.WorkflowTemplate` exists for it
at the document's ``(school, branch)`` scope, with the same branch → school →
platform cascade the engine's ``submit_for_approval`` uses. When no template
exists, the direct-post path behaves exactly as it did before — so approvals can
be switched on one document type and one school at a time, with zero migration.  # Keep the gate template-driven.

:func:`approval_required` is the single place that decision is made; both the
submit endpoint and the direct-post view read it so they can never disagree.  # Single source of truth.
"""
from __future__ import annotations  # Defer annotation evaluation for forward references.


def approval_required(document) -> bool:
    """Return ``True`` iff ``document`` must go through workflow approval.

    True when a published :class:`~vs_workflow.models.WorkflowTemplate` exists for
    the document's ``workflow_document_type`` at its ``(school, branch)`` scope —
    matched with the same branch-specific → school-wide → platform-wide cascade as
    :func:`vs_workflow.services.submission.submit_for_approval`, so the gate and the
    engine always resolve the same template. ``False`` when the document declares no
    ``workflow_document_type`` or no matching template is published.

    ``WorkflowTemplate`` is imported lazily to avoid an import cycle at app load.
    """
    document_type = getattr(document, "workflow_document_type", None)  # Read the document type if the model exposes one.
    if not document_type:  # Documents without a workflow type never require approval.
        return False

    from vs_workflow.models import WorkflowTemplate  # Import lazily to avoid app-load cycles.

    school = getattr(document, "school", None)  # The document's school scope, if any.
    branch = getattr(document, "branch", None)  # The document's branch scope, if any.

    # Cascade: branch-specific → school-wide → platform-wide (mirrors submission.py).  # Match the workflow engine order.
    scopes = [{"school": school, "branch": branch}]  # Start with the most specific scope.
    if branch is not None:  # Fall back to school-wide when a branch is present.
        scopes.append({"school": school, "branch": None})  # Add the school-wide scope.
    if school is not None or branch is not None:  # Finally fall back to platform-wide.
        scopes.append({"school": None, "branch": None})  # Add the global scope.

    for scope in scopes:  # Test each scope in order until a template is found.
        if WorkflowTemplate.objects.filter(document_type=document_type, **scope).exists():  # Published template exists.
            return True  # Approval is required when a matching template exists.
    return False  # No matching template means direct posting stays allowed.
