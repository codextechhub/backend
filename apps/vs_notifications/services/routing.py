from __future__ import annotations

import re

from django.db.models import Q


_TICKET_ROUTE = re.compile(r"^/support/tickets/(?P<id>[^/]+)$")
_IMPORT_BATCH_ROUTE = re.compile(r"^/data-imports/batches/(?P<id>[^/]+)/view$")
_WORKFLOW_APPROVAL_ROUTE = re.compile(r"^/workflow/approvals/(?P<id>[^/]+)$")
_WORKFLOW_SUBMISSION_ROUTE = re.compile(r"^/workflow/my-submissions/(?P<id>[^/]+)$")
_WORKFLOW_APPROVAL_EVENTS = ("workflow.stage_activated", "workflow.escalated")

# Where a finished background job is read, by its ``BackgroundJob.kind``. A
# kind absent here has no record page of its own, and its notification is left
# unlinked rather than pointed at a list the reader would then have to search.
_JOB_KIND_ROUTES = {
    "import": "/data-imports/batches/{target}/view",
    "import_rollback": "/data-imports/batches/{target}/view",
}

_PREFIX_ROUTES = {
    "/data-imports/batches": ("import.",),
    "/export/files": ("export.",),
    "/team-management": ("user.", "team."),
    "/me/security": ("security.",),
    "/finance": ("finance.", "payments."),
    "/procurement": ("procurement.",),
}


def notification_action_url(notification):
    """Resolve a notification to an allowlisted frontend destination."""
    event_key = notification.event_type.key
    metadata = notification.metadata or {}
    if event_key.startswith("ticket.") and metadata.get("ticket_id"):
        return f"/support/tickets/{metadata['ticket_id']}"
    if event_key.startswith("workflow.") and metadata.get("workflow_instance_id"):
        instance_id = metadata["workflow_instance_id"]
        if event_key in _WORKFLOW_APPROVAL_EVENTS:
            return f"/workflow/approvals/{instance_id}"
        return f"/workflow/my-submissions/{instance_id}"
    # A failure notification has to land on the run that explains itself, not on
    # a list the reader then has to search. Falls back to the Files list below
    # when the run id is absent.
    if event_key.startswith("export.") and metadata.get("export_run_id"):
        return f"/export/runs/{metadata['export_run_id']}"
    # The generic background-job bell. Its event key names the outcome and not
    # the subject, so the destination comes from the job's own kind and target
    # rather than from the key the way every rule above works.
    if event_key.startswith("task."):
        template = _JOB_KIND_ROUTES.get(metadata.get("job_kind") or "")
        target = metadata.get("job_target_id") or ""
        if template and target:
            return template.format(target=target)
        return ""
    for route, prefixes in _PREFIX_ROUTES.items():
        if event_key.startswith(prefixes):
            return route
    return ""


def _metadata_value_q(field, raw_value):
    query = Q(**{field: raw_value})
    if raw_value.isdigit():
        query |= Q(**{field: int(raw_value)})
    return query


def notification_route_q(path):
    """Return the indexed/scoped event filter for an allowlisted route."""
    path = path.rstrip("/") or "/"
    if match := _TICKET_ROUTE.fullmatch(path):
        return Q(event_type__key__startswith="ticket.") & _metadata_value_q(
            "metadata__ticket_id", match.group("id")
        )
    if match := _WORKFLOW_APPROVAL_ROUTE.fullmatch(path):
        return Q(event_type__key__in=_WORKFLOW_APPROVAL_EVENTS) & _metadata_value_q(
            "metadata__workflow_instance_id", match.group("id")
        )
    if match := _WORKFLOW_SUBMISSION_ROUTE.fullmatch(path):
        return (
            Q(event_type__key__startswith="workflow.")
            & ~Q(event_type__key__in=_WORKFLOW_APPROVAL_EVENTS)
            & _metadata_value_q("metadata__workflow_instance_id", match.group("id"))
        )
    if match := _IMPORT_BATCH_ROUTE.fullmatch(path):
        return Q(event_type__key__startswith="task.") & _metadata_value_q(
            "metadata__job_target_id", match.group("id")
        )
    if prefixes := _PREFIX_ROUTES.get(path):
        query = Q()
        for prefix in prefixes:
            query |= Q(event_type__key__startswith=prefix)
        return query
    return None
