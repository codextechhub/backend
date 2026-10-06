"""The ``?approval=`` filter every document list, and every export behind one, shares.

A document an approver has returned to whoever sent it is still a document
with an open request, so no status field of its own says so: a finance
document is a DRAFT again and a procurement document keeps its PENDING
approval state. Only its latest approval request knows. A list that offers
"Sent back" therefore filters on that request, and every list filters the
same way, through :func:`filter_by_approval`, so a returned credit note and a
returned purchase order are found by one rule and the filter always agrees
with the ``approval_returned`` each row carries.

The export of a list reads the same rule. :func:`export_filter` declares it on
an Export Centre dataset, so a list filtered to Sent back exports exactly the
rows it shows: a quick export from the screen carries ``approval=returned``
across, and a value the list refuses is refused there in the same words.

The filter is a subquery on the document's latest request, applied to the
queryset before it is paginated, so the page count and the pages are right.
It narrows; it never widens: the caller's tenant, branch reach and other
filters stay as they were.
"""
from __future__ import annotations

from django.db.models import CharField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, Coalesce
from django.db.models.lookups import Exact
from rest_framework.exceptions import ValidationError

#: The query parameter, and the values it takes.
APPROVAL_PARAM = "approval"
APPROVAL_RETURNED = "returned"
#: The export-only value for the rows NOT sent back, which a status word reads.
APPROVAL_NOT_RETURNED = "not_returned"

#: What a reader is told when ``approval`` holds anything but ``returned``.
APPROVAL_REFUSAL = (
    "Use approval=returned to list the documents sent back to whoever sent them."
)


def asks_for_returned(value) -> bool:
    """Whether an ``?approval=`` value asks for the documents sent back.

    ``None`` or blank asks for nothing and answers False. ``returned`` (in any
    case, surrounding spaces ignored) answers True. Anything else is refused
    with a 400 on ``approval``, so a typo never quietly returns the whole list.
    """
    if value in (None, ""):
        return False
    if str(value).strip().lower() != APPROVAL_RETURNED:
        raise ValidationError({APPROVAL_PARAM: APPROVAL_REFUSAL})
    return True


def latest_request_status(model, *, path=""):
    """A subquery: the status of each row's latest approval request, or NULL if never sent.

    ``model`` is the document the request governs. ``path`` is the route from
    the queryset's row to that document, for a row that is part of one (a
    journal line reaches its journal through ``entry``); blank means the row
    is the document.

    The latest request is the one created last, ties falling to the higher id,
    which is the order every approval read uses (``approval_returned``,
    ``workflow_instance_id``): a document withdrawn and sent again is judged by
    the request it was sent again under.
    """
    from django.contrib.contenttypes.models import ContentType

    from vs_workflow.models import WorkflowInstance

    content_type = ContentType.objects.get_for_model(model, for_concrete_model=True)
    return Subquery(
        WorkflowInstance.all_objects.filter(
            document_content_type_id=content_type.pk,
            document_object_id=Cast(
                OuterRef(f"{path}__pk" if path else "pk"), output_field=CharField(),
            ),
        ).order_by("-created_at", "-pk").values("status")[:1]
    )


def returned_condition(model, *, path=""):
    """A ``Q`` keeping the rows whose document's latest request was returned to its sender.

    ``model`` and ``path`` are as for :func:`latest_request_status`. This is
    the one statement of "sent back" as a query: the lists apply it through
    :func:`filter_by_approval` and the exports through :func:`export_filter`.
    A document never sent reads as blank rather than NULL, so the negation
    (:func:`excluding_returned`) keeps it instead of losing it to SQL's
    three-valued NOT.
    """
    from vs_workflow.constants import WorkflowInstanceStatus

    status = Coalesce(latest_request_status(model, path=path), Value(""),
                      output_field=CharField())
    return Q(Exact(status, WorkflowInstanceStatus.RETURNED))


def excluding_returned(queryset, *, path=""):
    """``queryset`` without the documents an approver sent back to whoever sent them."""
    return queryset.exclude(returned_condition(queryset.model, path=path))


def filter_by_status_word(queryset, value, rules: dict, *, param: str = "status"):
    """A document list's status filter: the rows wearing word ``value``, never one sent back.

    ``rules`` maps each word the list offers to the rows it means, as for
    :func:`core.list_filters.filter_by_word`, which reads the value (any case;
    an unknown word is a 400). A document an approver sent back wears "Sent
    back" whatever its stored status (a finance document is a DRAFT again, a
    procurement one still PENDING_APPROVAL), so no other word selects it: any
    word given leaves it out, and ``?approval=returned`` finds it. Mrs Okafor's
    journal returned by Mr Adeyemi is listed under Sent back, not under Draft
    beside the drafts she has never sent.
    """
    from core.list_filters import filter_by_word

    narrowed = filter_by_word(queryset, value, rules, param=param)
    return queryset if narrowed is queryset else excluding_returned(narrowed)


def word_condition(model, rule) -> Q:
    """The documents a status word counts, as a condition: its ``rule``, never one sent back.

    :func:`filter_by_status_word` as a ``Q``, for a summary that counts each
    tab in one aggregate (``Count("id", filter=word_condition(...))``), so a
    tab's count is the number of rows the tab lists. The documents sent back
    are counted apart, under ``sent_back``, with :func:`returned_condition`.
    """
    return rule & ~returned_condition(model)


def stored_status_rules(*statuses, field: str = "status") -> dict:
    """``{STATUS: Q(field=STATUS)}`` for words that are the stored status itself."""
    return {str(status): Q(**{field: status}) for status in statuses}


def filter_by_stored_status(queryset, value, *, field: str = "status", param: str = "status"):
    """:func:`filter_by_status_word` for a list whose words are its stored statuses.

    The words are the field's own choices, so a status the model gains is a
    word the list takes without another edit.
    """
    choices = [code for code, _ in queryset.model._meta.get_field(field).choices]
    return filter_by_status_word(
        queryset, value, stored_status_rules(*choices, field=field), param=param,
    )


def filter_by_approval(queryset, value):
    """``queryset`` narrowed by an ``?approval=`` value; unchanged when none is given.

    ``returned`` keeps only the documents whose latest approval request an
    approver has returned to its sender (the rows reading ``approval_returned``
    true). Any other value is refused (400 on ``approval``).
    """
    if not asks_for_returned(value):
        return queryset
    return queryset.filter(returned_condition(queryset.model))


def filter_by_approval_param(queryset, params):
    """:func:`filter_by_approval` with the value read from a request's query ``params``."""
    return filter_by_approval(queryset, params.get(APPROVAL_PARAM))


def export_filter(document: str, *, source: str = ""):
    """The ``approval`` filter for an Export Centre dataset behind a list that offers Sent back.

    ``document`` is the ``app_label.ModelName`` of the document the list shows
    and the approval request governs. ``source`` is the route from the
    dataset's row to it, blank when the row is the document itself; the
    postings dataset, one row per journal line, passes ``entry``.

    It is published as a choice, ``{"id": "approval", "values":
    ["returned"]}``, so the builder offers it as a "Sent back" box like any
    other choice. A screen export reads the list's own ``approval`` parameter
    into it (:func:`vs_exports.catalogue.resolve_screen`), refusing what the
    list refuses with the list's 400. ``not_returned`` is the export's own
    value for "not sent back", which a screen's translator adds beside any
    status word (:func:`not_returned_spec`), as the list's status filter
    leaves sent-back documents out (:func:`filter_by_status_word`). A stored or
    quick-run filter holding any other value is refused with the list's
    sentence. No value, both values, or an empty list narrows nothing.
    """
    from django.apps import apps

    from vs_exports.catalogue import FILTER_CHOICE, FilterDef, FilterError

    def compiles(spec, scope=None):
        values = spec.get("values") or []
        if not isinstance(values, list):
            raise FilterError("“Approval” expects a list of values.", filter_id=APPROVAL_PARAM)
        others = [value for value in values if value != APPROVAL_NOT_RETURNED]
        try:
            returned = any([asks_for_returned(value) for value in others])
        except ValidationError:
            raise FilterError(APPROVAL_REFUSAL, filter_id=APPROVAL_PARAM)
        not_returned = len(others) < len(values)
        if returned == not_returned:
            return Q()
        condition = returned_condition(apps.get_model(document), path=source)
        return condition if returned else ~condition

    def from_screen(value):
        asks_for_returned(value)
        return {"id": APPROVAL_PARAM, "values": [APPROVAL_RETURNED]}

    return FilterDef(
        APPROVAL_PARAM, "Approval", FILTER_CHOICE,
        source=source,
        choices={APPROVAL_RETURNED: "Sent back", APPROVAL_NOT_RETURNED: "Not sent back"},
        description="Only the documents an approver sent back to whoever sent them.",
        compiles=compiles,
        screen_param=APPROVAL_PARAM,
        from_screen=from_screen,
    )


def not_returned_spec() -> dict:
    """The export filter a screen translator adds beside a status word: not sent back.

    The export twin of :func:`filter_by_status_word`, so a file exported from a
    list filtered to Draft holds the drafts the list shows and not the
    documents sent back, which the list shows under Sent back.
    """
    return {"id": APPROVAL_PARAM, "values": [APPROVAL_NOT_RETURNED]}
