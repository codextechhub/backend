"""The ``?approval=`` filter every document list shares.

A document an approver has returned to whoever sent it is still a document
with an open request, so no status field of its own says so: a finance
document is a DRAFT again and a procurement document keeps its PENDING
approval state. Only its latest approval request knows. A list that offers
"Sent back" therefore filters on that request, and every list filters the
same way, through :func:`filter_by_approval`, so a returned credit note and a
returned purchase order are found by one rule and the filter always agrees
with the ``approval_returned`` each row carries.

The filter is a subquery on the document's latest request, applied to the
queryset before it is paginated, so the page count and the pages are right.
It narrows; it never widens: the caller's tenant, branch reach and other
filters stay as they were.
"""
from __future__ import annotations

from django.contrib.contenttypes.models import ContentType
from django.db.models import CharField, OuterRef, Subquery
from django.db.models.functions import Cast
from rest_framework.exceptions import ValidationError

#: The query parameter, and the values it takes.
APPROVAL_PARAM = "approval"
APPROVAL_RETURNED = "returned"


def latest_request_status(model):
    """A subquery: the status of each row's latest approval request, or NULL if never sent.

    The latest request is the one created last, ties falling to the higher id,
    which is the order every approval read uses (``approval_returned``,
    ``workflow_instance_id``): a document withdrawn and sent again is judged by
    the request it was sent again under.
    """
    from vs_workflow.models import WorkflowInstance

    content_type = ContentType.objects.get_for_model(model, for_concrete_model=True)
    return Subquery(
        WorkflowInstance.all_objects.filter(
            document_content_type_id=content_type.pk,
            document_object_id=Cast(OuterRef("pk"), output_field=CharField()),
        ).order_by("-created_at", "-pk").values("status")[:1]
    )


def filter_by_approval(queryset, value):
    """``queryset`` narrowed by an ``?approval=`` value; unchanged when none is given.

    ``returned`` keeps only the documents whose latest approval request an
    approver has returned to its sender (the rows reading ``approval_returned``
    true). Any other value is refused (400 on ``approval``), so a typo never
    quietly returns the whole list.
    """
    from vs_workflow.constants import WorkflowInstanceStatus

    if value in (None, ""):
        return queryset
    if str(value).strip().lower() != APPROVAL_RETURNED:
        raise ValidationError({
            APPROVAL_PARAM: "Use approval=returned to list the documents sent back to whoever sent them.",
        })
    return queryset.alias(
        _latest_request_status=latest_request_status(queryset.model),
    ).filter(_latest_request_status=WorkflowInstanceStatus.RETURNED)


def filter_by_approval_param(queryset, params):
    """:func:`filter_by_approval` with the value read from a request's query ``params``."""
    return filter_by_approval(queryset, params.get(APPROVAL_PARAM))
