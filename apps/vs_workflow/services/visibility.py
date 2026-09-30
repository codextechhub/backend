"""Which approval instances a reader may see, beyond the branch they are filed under.

An instance is filed under its document's branch, and the approval reads narrow by
it inclusively. Some documents' reach is decided by their own module rather than by
that reading (see
:meth:`vs_workflow.handlers.base.BaseWorkflowHandler.hidden_document_ids`): a
payout batch has no branch of its own, and a transaction with no branch is not
shared. This is where every approval read asks each registered handler for the
documents it keeps from this reader. A read that skips it would show an approver a
document the module that owns it would answer with a 404.
"""
from __future__ import annotations

from django.db.models import CharField, Q, QuerySet
from django.db.models.functions import Cast

from vs_workflow.handlers.registry import list_registered_handlers


def exclude_hidden_documents(qs, user, tenant, *, prefix: str = ""):
    """``qs`` without the instances whose document its handler hides from ``user``.

    ``prefix`` names the route from ``qs``'s model to ``WorkflowInstance``
    (``"stage_instance__instance__"`` for approver snapshots). ``tenant`` defaults
    to the reader's own. A handler may answer with ids or with a queryset of them
    (:func:`documents_outside_transaction_reach`); a queryset is used as a
    subquery, so the ids never leave the database.
    """
    tenant = tenant if tenant is not None else getattr(user, "tenant", None)
    for document_type, handler in list_registered_handlers().items():
        hidden = handler.hidden_document_ids(user, tenant)
        if hidden is None:
            continue
        if not isinstance(hidden, QuerySet):
            hidden = list(hidden)
            if not hidden:
                continue
        qs = qs.exclude(
            Q(**{f"{prefix}document_type": document_type})
            & Q(**{f"{prefix}document_object_id__in": hidden})
        )
    return qs


def documents_outside_transaction_reach(documents, user, tenant, *, field: str = "branch"):
    """The ids of ``documents`` outside ``user``'s transaction reach, for a handler.

    For a handler whose document is a transaction (an invoice's adjustment, a
    journal, a requisition, an order, a vendor bill or payment) to answer
    :meth:`~vs_workflow.handlers.base.BaseWorkflowHandler.hidden_document_ids`
    with. The engine files an instance under its document's branch and reads
    instances inclusively, because a workflow row with no branch is usually the
    school's configuration. A transaction with no branch is not shared, though: it
    is one not yet given its branch, and only a whole-school reader reaches it
    (:func:`vs_rbac.scoping.transaction_branch_scope`). So the Lekki approver does
    not see the approval of a journal raised before journals named a branch, nor
    of one given Ikeja since it was submitted.

    ``documents`` is the handler's documents in ``tenant``. Returns a queryset of
    string ids (``document_object_id`` is text), or ``None`` for a reader nothing
    narrows.
    """
    from vs_rbac.scoping import transaction_branch_scope_for_user

    if tenant is None or user is None:
        return None
    scope = transaction_branch_scope_for_user(user, tenant=tenant)
    if not scope.is_narrowed:
        return None
    return (
        documents.exclude(scope.q(field=field))
        .annotate(_workflow_document_id=Cast("pk", CharField()))
        .values("_workflow_document_id")
    )
