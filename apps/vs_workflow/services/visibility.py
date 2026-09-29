"""Which approval instances a reader may see, beyond the branch they are filed under.

An instance is filed under its document's branch, and the approval reads narrow by
it. Some documents' reach is decided by their own module rather than by one branch
(see :meth:`vs_workflow.handlers.base.BaseWorkflowHandler.hidden_document_ids`), and
this is where every approval read asks each registered handler for the documents it
keeps from this reader. A read that skips it would show an approver a document the
module that owns it would answer with a 404.
"""
from __future__ import annotations

from django.db.models import Q

from vs_workflow.handlers.registry import list_registered_handlers


def exclude_hidden_documents(qs, user, tenant, *, prefix: str = ""):
    """``qs`` without the instances whose document its handler hides from ``user``.

    ``prefix`` names the route from ``qs``'s model to ``WorkflowInstance``
    (``"stage_instance__instance__"`` for approver snapshots). ``tenant`` defaults
    to the reader's own.
    """
    tenant = tenant if tenant is not None else getattr(user, "tenant", None)
    for document_type, handler in list_registered_handlers().items():
        hidden = handler.hidden_document_ids(user, tenant)
        if not hidden:
            continue
        qs = qs.exclude(
            Q(**{f"{prefix}document_type": document_type})
            & Q(**{f"{prefix}document_object_id__in": list(hidden)})
        )
    return qs
