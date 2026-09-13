"""Where a notification points, and what reading that destination clears.

:data:`RECORD_DESTINATIONS` is the single source of truth for a notification
that is about one record. Each entry names the event keys it covers, the
metadata key holding the record id, and the route template. Both directions are
derived from that one table: :func:`notification_action_url` builds the link a
reader follows, and :func:`notification_route_q` reverses a path back into the
filter that clears it. Deriving both is the point. Two independent allowlists
drift, and a destination with no matching clear rule leaves a bell entry unread
forever after the reader has already opened the very page it pointed at.

Two kinds of destination live here, and they are deliberately not symmetrical.

A *record* destination points at one row, and is an acknowledgement rule.
Opening that row clears the notifications that pointed at it.

A *list* destination points at a module index. It is somewhere to go when a
notification names no record, and it acknowledges nothing, because arriving at
an index is no evidence that anything on it was read. Someone holding a
requisition awaiting her approval, a delivery notice and a vendor quote who
opens the procurement index to look up a stationery order has read none of the
three; a rule that cleared them on arrival would take the approval off her bell
before she ever saw it.

A record family may also carry an id and no route, for a record whose
notifications clear when it is read through the API while the frontend has no
page to deep-link to.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import reduce
from operator import or_
from typing import Mapping

from django.db.models import Q


class RecordFamily:
    """The record types a notification can be about.

    A read endpoint names a family rather than a route, so it clears the right
    notifications whether or not the frontend has a page for them, and so the
    two sides cannot fall out of step over a renamed URL.
    """

    TICKET = "ticket"
    WORKFLOW_INSTANCE = "workflow_instance"
    EXPORT_RUN = "export_run"
    IMPORT_JOB = "import_job"
    HEALTH_INCIDENT = "health_incident"
    TODO_TASK = "todo_task"


@dataclass(frozen=True)
class EventKeys:
    """The event keys one destination covers.

    ``prefix`` claims a whole family, ``keys`` names individual events, and
    ``excluded`` carves out the keys a sibling destination claims instead. The
    carve-out is what lets one record send its approver and its submitter to
    different pages while both still clear when the record is read.
    """

    prefix: str = ""
    keys: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    def matches(self, event_key: str) -> bool:
        """Whether *event_key* belongs to this destination."""
        if event_key in self.excluded:
            return False
        if self.keys and event_key in self.keys:
            return True
        return bool(self.prefix) and event_key.startswith(self.prefix)

    def as_q(self) -> Q:
        """The same match as a queryset condition on ``event_type__key``."""
        query = Q(event_type__key__startswith=self.prefix) if self.prefix else Q()
        if self.keys:
            named = Q(event_type__key__in=self.keys)
            query = query | named if self.prefix else named
        if self.excluded:
            query &= ~Q(event_type__key__in=self.excluded)
        return query


@dataclass(frozen=True)
class RecordDestination:
    """One notification family that points at a single record.

    ``family`` is the record, not the page. Two entries share a family when the
    same row is read in two places, and reading the row clears both.

    ``variant_key`` covers the family whose event key names an outcome instead
    of a subject: ``task.completed`` says a background job finished and nothing
    about what the job was, so the route comes from the job's own kind. A kind
    absent from ``variant_routes`` has no record page, and its notification is
    left unlinked rather than pointed at a list the reader would then have to
    search.
    """

    family: str
    keys: EventKeys
    id_key: str
    route: str = ""
    variant_key: str = ""
    variant_routes: Mapping[str, str] = field(default_factory=dict)

    def url(self, metadata) -> str:
        """The destination for a notification in this family, or ``""``."""
        metadata = metadata or {}
        record_id = metadata.get(self.id_key)
        if record_id in (None, ""):
            return ""
        template = self.route
        if self.variant_key:
            variant = metadata.get(self.variant_key) or ""
            template = self.variant_routes.get(variant, "")
        return template.format(id=record_id) if template else ""

    def route_templates(self) -> tuple[str, ...]:
        """Every distinct template this destination can produce."""
        if self.route:
            return (self.route,)
        return tuple(dict.fromkeys(self.variant_routes.values()))


@dataclass(frozen=True)
class ListDestination:
    """A module index, offered when a notification names no record.

    Destination only. Reaching an index says nothing about which of its rows
    was read, so no acknowledgement rule is derived from these.
    """

    route: str
    prefixes: tuple[str, ...]


_WORKFLOW_APPROVAL_KEYS = ("workflow.stage_activated", "workflow.escalated")

RECORD_DESTINATIONS = (
    RecordDestination(
        family=RecordFamily.TICKET,
        keys=EventKeys(prefix="ticket."),
        id_key="ticket_id",
        route="/support/tickets/{id}",
    ),
    RecordDestination(
        family=RecordFamily.WORKFLOW_INSTANCE,
        keys=EventKeys(keys=_WORKFLOW_APPROVAL_KEYS),
        id_key="workflow_instance_id",
        route="/workflow/approvals/{id}",
    ),
    RecordDestination(
        family=RecordFamily.WORKFLOW_INSTANCE,
        keys=EventKeys(prefix="workflow.", excluded=_WORKFLOW_APPROVAL_KEYS),
        id_key="workflow_instance_id",
        route="/workflow/my-submissions/{id}",
    ),
    # A failed export has to land on the run that explains itself, not on a
    # list the reader then has to search. Falls back to the files list below
    # when the run id is absent.
    RecordDestination(
        family=RecordFamily.EXPORT_RUN,
        keys=EventKeys(prefix="export."),
        id_key="export_run_id",
        route="/export/runs/{id}",
    ),
    RecordDestination(
        family=RecordFamily.IMPORT_JOB,
        keys=EventKeys(prefix="task."),
        id_key="job_target_id",
        variant_key="job_kind",
        variant_routes={
            "import": "/data-imports/batches/{id}/view",
            "import_rollback": "/data-imports/batches/{id}/view",
        },
    ),
    # An incident is read through the API and has no page of its own, so this
    # family carries the clear rule without a link to offer.
    RecordDestination(
        family=RecordFamily.HEALTH_INCIDENT,
        keys=EventKeys(prefix="health."),
        id_key="incident_id",
    ),
    # A review request names one task, but the console lists tasks and has no
    # page for a single one, so the link below falls through to that list while
    # the clear rule stays tied to the task itself.
    RecordDestination(
        family=RecordFamily.TODO_TASK,
        keys=EventKeys(prefix="todo."),
        id_key="todo_task_id",
    ),
)

LIST_DESTINATIONS = (
    ListDestination(route="/data-imports/batches", prefixes=("import.",)),
    ListDestination(route="/export/files", prefixes=("export.",)),
    ListDestination(route="/team-management", prefixes=("user.", "team.")),
    ListDestination(route="/me/security", prefixes=("security.",)),
    # Billing events are addressed to a customer's email rather than to a user,
    # so dispatch writes no in-app row for one and this rule is inert until one
    # of them is sent to staff. It names the family that exists all the same:
    # a rule written against a key no event uses is a rule nobody can test.
    ListDestination(route="/finance", prefixes=("billing.", "payments.")),
    ListDestination(route="/procurement", prefixes=("procurement.",)),
    ListDestination(route="/tasks", prefixes=("todo.",)),
)


def _route_pattern(template: str) -> re.Pattern:
    """Turn a route template into the pattern that reads its id back out."""
    return re.compile(
        r"(?P<id>[^/]+)".join(re.escape(part) for part in template.split("{id}"))
    )


# Every record route paired with the destination it came from, so the reverse
# direction is generated rather than restated.
_REVERSE_RULES = tuple(
    (_route_pattern(template), destination)
    for destination in RECORD_DESTINATIONS
    for template in destination.route_templates()
)


def notification_action_url(notification):
    """Resolve a notification to an allowlisted frontend destination."""
    event_key = notification.event_type.key
    metadata = notification.metadata or {}
    for destination in RECORD_DESTINATIONS:
        if destination.keys.matches(event_key) and (url := destination.url(metadata)):
            return url
    for destination in LIST_DESTINATIONS:
        if event_key.startswith(destination.prefixes):
            return destination.route
    return ""


def _metadata_value_q(field_path, raw_value):
    """Match an id stored as either JSON text or a JSON number."""
    query = Q(**{field_path: raw_value})
    if raw_value.isdigit():
        query |= Q(**{field_path: int(raw_value)})
    return query


def destinations_for_family(family) -> tuple[RecordDestination, ...]:
    """Every destination about one record type."""
    found = tuple(item for item in RECORD_DESTINATIONS if item.family == family)
    if not found:
        raise KeyError(f"No notification record family named {family!r}.")
    return found


def record_filter_q(destination, value) -> Q:
    """The filter for one destination: its event family and one record id."""
    return destination.keys.as_q() & _metadata_value_q(
        f"metadata__{destination.id_key}", str(value)
    )


def family_filter_q(family, value) -> Q:
    """The filter for every notification in *family* about one record."""
    return reduce(
        or_,
        (record_filter_q(item, value) for item in destinations_for_family(family)),
    )


def notification_route_q(path):
    """Return the event filter for a record route, or None if there is none.

    A module index returns None on purpose: it is a destination and not an
    acknowledgement, so landing on one clears nothing.
    """
    path = path.rstrip("/") or "/"
    for pattern, destination in _REVERSE_RULES:
        if match := pattern.fullmatch(path):
            return record_filter_q(destination, match.group("id"))
    return None
