"""Audit datasets published to the Export Centre.

Registered from :meth:`vs_audit.apps.VsAuditConfig.ready`. Tenant-scoped: an audit
event belongs to the organisation, not to a set of books, which is exactly the case
that made ``ExportDefinition.entity`` nullable.

The permission is ``platform.audit.export`` rather than ``platform.audit.view``:
reading the console and taking the trail out of the building are different decisions,
and the seed already marks the export key restricted.
"""
from __future__ import annotations

from vs_exports.catalogue import (
    FILTER_CHOICE,
    FILTER_DATE_RANGE,
    FILTER_SEARCH,
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


def _audit_events(scope):
    """The tenant-scoped base queryset an audit export reads.

    The boundary is ``vs_audit.scoping.tenant_event_predicate``, the same one
    the Event Explorer and every other audit surface reads, and never a second
    copy spelled ``filter(tenant=scope.tenant)``. That copy is narrower: it
    matches the column alone and misses rows that carry their owner's pk in
    ``metadata['tenant_id']`` instead. Bright Star's audit officer would see
    her old password resets on the screen, export that same view, and not find
    them in the file.

    Only the boundary is shared, not the console's widening for PLATFORM
    callers. An export always covers the caller's own organisation, which is
    what ``_translate_events`` tells a platform reviewer who narrowed the
    screen with ``tenant_slug``, so this reads the predicate directly and never
    ``audit_scope_predicate``. Codex is a tenant like any other here and
    recovers its own rows the same way every school does.
    """
    from .models import AuditEvent
    from .scoping import tenant_event_predicate

    return AuditEvent.objects.filter(tenant_event_predicate(scope.tenant))


_SEVERITY = choice_labels("vs_audit.models.AuditSeverity")
_STATUS = choice_labels("vs_audit.models.AuditStatus")
_MODULE = choice_labels("vs_audit.models.AuditModuleKey")
_ACTION = choice_labels("vs_audit.models.AuditActionType")


# Register every audit dataset. Called once from AppConfig.ready().
def register_datasets():
    register(Dataset(
        key="audit.events",
        module="Audit",
        name="Audit events",
        description=(
            "Every audited action across the platform, one row per event. Covers the "
            "whole organisation - audit is not kept per set of books. This is the "
            "dataset a compliance review starts from."
        ),
        base=_audit_events,
        scope=DatasetScope.TENANT,
        permission="platform.audit.export",
        row_cap=500_000,
        default_columns=("event_at", "module_key", "action_type", "actor_label", "summary"),
        fields=(
            Field("event_id", "Event", "Event", KIND_TEXT, source="id", locked=True),
            Field("event_at", "When", "Event", KIND_DATETIME),
            Field("module_key", "Module", "Event", KIND_CHOICE, choices=_MODULE),
            Field("action_type", "Action", "Event", KIND_CHOICE, choices=_ACTION),
            Field("severity", "Severity", "Event", KIND_CHOICE, choices=_SEVERITY),
            Field("status", "Outcome", "Event", KIND_CHOICE, choices=_STATUS),
            Field("summary", "Summary", "Event", KIND_TEXT),
            Field("actor_type", "Actor type", "Actor", KIND_TEXT),
            Field("actor_label", "Actor", "Actor", KIND_TEXT),
            Field("entity_type", "Object type", "Target", KIND_TEXT),
            Field("entity_label", "Object", "Target", KIND_TEXT),
            Field("actor_email", "Actor email", "Actor", KIND_TEXT,
                  source="actor_user__email", sensitive=True,
                  description="Restricted: identifies the person behind the action."),
        ),
        filters=(
            FilterDef("event_at", "When", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True,
                      description="Required - an unbounded audit export is never what "
                                  "anyone actually wants."),
            FilterDef("module_key", "Module", FILTER_CHOICE, choices=_MODULE),
            FilterDef("action_type", "Action", FILTER_CHOICE, choices=_ACTION),
            FilterDef("severity", "Severity", FILTER_CHOICE, choices=_SEVERITY),
            FilterDef("status", "Outcome", FILTER_CHOICE, choices=_STATUS),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("summary", "Summary"), ("actor_label", "Actor"),
                ("entity_label", "Object"),
            ), description="Matches any one of these, the way console search does."),
            FilterDef("entity_type", "Object type", FILTER_TEXT),
            FilterDef("entity_label", "Object", FILTER_TEXT),
        ),
    ))


# --------------------------------------------------------------------------- #
# Screen bindings                                                             #
# --------------------------------------------------------------------------- #
# Translate the audit console's filters into export filters.
def _translate_events(params):
    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    for param in ("module_key", "severity", "status"):
        if value := params.get(param):
            filters.append({"id": param, "values": [value]})
    if value := params.get("action_type"):
        filters.append({"id": "action_type", "values": [value]})
    if value := params.get("entity_type"):
        filters.append({"id": "entity_type", "value": value})

    # The console spells its window either way round depending on the screen.
    window = {}
    if value := (params.get("start") or params.get("date_from")):
        window["start"] = value
    if value := (params.get("end") or params.get("date_to")):
        window["end"] = value
    if window:
        filters.append({"id": "event_at", **window})
    if value := params.get("search"):
        filters.append({"id": "search", "value": value})
    if value := params.get("actor"):
        unmapped.append(Unmapped(
            "actor", value,
            "The audit export does not filter by actor yet; the file covers everyone "
            "the other filters allow.",
        ))
    if value := params.get("tenant_slug"):
        # The screen and the file do not mean the same thing by "tenant", and
        # saying so is what Unmapped is for: the screen is not tenant-scoped, the
        # export is, and the export's boundary is not editable by a filter.
        unmapped.append(Unmapped(
            "tenant_slug", value,
            "An audit export always covers your own organisation, so it cannot be "
            "narrowed to another tenant the way the screen can.",
        ))
    return filters, unmapped


# Register the audit screens. Called once from AppConfig.ready().
def register_screens():
    from vs_exports.catalogue import ScreenBinding, register_screen

    register_screen(ScreenBinding(
        key="audit.events",
        handles=(
            "module_key", "severity", "status", "action_type", "entity_type",
            "start", "end", "date_from", "date_to", "search", "actor",
            "tenant_slug",
        ),
        label="Audit - Activity",
        dataset_key="audit.events",
        translate=_translate_events,
        # An audit console defaults to the recent past; 90 days matches the console.
        default_window_days=90,
    ))
