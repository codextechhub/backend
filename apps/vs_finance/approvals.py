"""The approval opt-in gate for finance documents.

Finance approvals are **opt-in by template** (design §7): a document type is
approval-gated *iff* a :class:`~vs_workflow.models.WorkflowTemplate` exists for it
at the document's ``(school, branch)`` scope, with the same branch → school →
platform cascade the engine's ``submit_for_approval`` uses. When no template
exists, the direct-post path behaves exactly as it did before — so approvals can
be switched on one document type and one school at a time, with zero migration.

:func:`approval_required` is the single place that decision is made; both the
submit endpoint and the direct-post view read it so they can never disagree.
"""
from __future__ import annotations


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
    document_type = getattr(document, "workflow_document_type", None)
    if not document_type:
        return False

    from vs_workflow.models import WorkflowTemplate

    school = getattr(document, "school", None)
    branch = getattr(document, "branch", None)

    # Cascade: branch-specific → school-wide → platform-wide (mirrors submission.py).
    scopes = [{"school": school, "branch": branch}]
    if branch is not None:
        scopes.append({"school": school, "branch": None})
    if school is not None or branch is not None:
        scopes.append({"school": None, "branch": None})

    for scope in scopes:
        if WorkflowTemplate.objects.filter(document_type=document_type, **scope).exists():
            return True
    return False
