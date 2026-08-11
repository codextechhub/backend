"""Support datasets published to the Export Centre.

Registered from :meth:`vs_tickets.apps.VsTicketsConfig.ready`. Tenant-scoped: a
support ticket belongs to the organisation that raised it.

The ticket body is deliberately *not* offered as a column. Descriptions are free text
that people paste account numbers and screenshots of payslips into, and a catalogue
that offers it invites exactly that data out of the building in a spreadsheet.
"""
from __future__ import annotations

from vs_exports.catalogue import (
    FILTER_CHOICE,
    FILTER_DATE_RANGE,
    FILTER_TEXT,
    KIND_CHOICE,
    KIND_DATETIME,
    KIND_TEXT,
    Dataset,
    DatasetScope,
    Field,
    FilterDef,
    choice_labels,
    register,
)


# Build the tenant-scoped base queryset for support tickets.
def _tickets(scope):
    from .models import Ticket

    return Ticket.all_objects.filter(tenant=scope.tenant)


_CATEGORY = choice_labels("vs_tickets.constants.TicketCategory")
_PRIORITY = choice_labels("vs_tickets.constants.TicketPriority")
_STATUS = choice_labels("vs_tickets.constants.TicketStatus")


# Register every support dataset. Called once from AppConfig.ready().
def register_datasets():
    register(Dataset(
        key="support.tickets",
        module="Support",
        name="Support tickets",
        description=(
            "Tickets raised by this organisation, with category, priority and how long "
            "each took to resolve. The ticket body is not exportable."
        ),
        base=_tickets,
        scope=DatasetScope.TENANT,
        permission="tickets.ticket.view",
        row_cap=200_000,
        default_columns=("ticket_number", "title", "status", "priority", "created_at"),
        fields=(
            Field("ticket_number", "Ticket", "Ticket", KIND_TEXT, locked=True),
            Field("title", "Title", "Ticket", KIND_TEXT),
            Field("category", "Category", "Ticket", KIND_CHOICE, choices=_CATEGORY),
            Field("priority", "Priority", "Ticket", KIND_CHOICE, choices=_PRIORITY),
            Field("status", "Status", "Ticket", KIND_CHOICE, choices=_STATUS),
            Field("source", "Raised through", "Ticket", KIND_TEXT),
            Field("created_at", "Raised", "Timeline", KIND_DATETIME),
            Field("resolved_at", "Resolved", "Timeline", KIND_DATETIME),
            Field("closed_at", "Closed", "Timeline", KIND_DATETIME),
            Field("requester_email", "Raised by", "People", KIND_TEXT,
                  source="requester__email", sensitive=True,
                  description="Restricted: identifies the person who asked for help."),
            Field("assignee_email", "Assigned to", "People", KIND_TEXT,
                  source="assignee__email"),
        ),
        filters=(
            FilterDef("created_at", "Raised", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_STATUS),
            FilterDef("priority", "Priority", FILTER_CHOICE, choices=_PRIORITY),
            FilterDef("category", "Category", FILTER_CHOICE, choices=_CATEGORY),
            FilterDef("title", "Title", FILTER_TEXT),
        ),
    ))


# --------------------------------------------------------------------------- #
# Screen bindings                                                             #
# --------------------------------------------------------------------------- #
# Translate the ticket list screen's filters into export filters.
def _translate_tickets(params):
    """``/v1/support/tickets/`` → filter specs for ``support.tickets``.

    The screen's ``created_from``/``created_to`` pair becomes one date-range filter,
    which also satisfies the dataset's required window - so a ticket export started
    from a date-filtered screen matches the screen exactly.
    """
    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    for param, filter_id in (("status", "status"), ("priority", "priority"),
                             ("category", "category")):
        if value := params.get(param):
            filters.append({"id": filter_id, "values": [value]})

    start, end = params.get("created_from"), params.get("created_to")
    if start or end:
        window = {"id": "created_at"}
        if start:
            window["start"] = start
        if end:
            window["end"] = end
        filters.append(window)

    if value := params.get("q"):
        filters.append({"id": "title", "value": value})
    for param, reason in (
        ("assignee", "The ticket export does not filter by assignee yet."),
        ("requester", "The ticket export does not filter by who raised it."),
        ("assigned_to_me", "This is a per-person view with no export equivalent."),
        ("school", "The ticket export does not filter by school."),
    ):
        if (value := params.get(param)) is not None:
            unmapped.append(Unmapped(param, value, reason))
    return filters, unmapped


# Register the support screens. Called once from AppConfig.ready().
def register_screens():
    from vs_exports.catalogue import ScreenBinding, register_screen

    register_screen(ScreenBinding(
        key="support.tickets",
        handles=(
            "status", "priority", "category", "created_from", "created_to",
            "q", "assignee", "requester", "assigned_to_me", "school",
        ),
        label="Support - Tickets",
        dataset_key="support.tickets",
        translate=_translate_tickets,
    ))
