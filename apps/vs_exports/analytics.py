"""Product analytics for the Export Centre — a second pipeline, deliberately separate.

**Why it is not the audit trail.** The two answer different questions and have opposite
requirements, so merging them damages both:

===============  ==========================================  =========================
                 Audit (:mod:`vs_exports.audit`)             Analytics (this module)
===============  ==========================================  =========================
Question         "Who took what data out?"                   "Is this feature working?"
Retention        Forever, beyond file expiry                 :data:`RETENTION_DAYS`
Mutability       Append-only, immutable, validated           Prunable, droppable
Volume           One row per meaningful act                  Many rows per session
Contents         Actor, tenant, object id, IP                Counts and buckets only
Losing a row     A compliance gap                            A rounding error
===============  ==========================================  =========================

Analytics volume would drown the audit trail and dilute its evidentiary value; audit's
immutability and retention rules would make this data expensive and impossible to
correct. Hence two pipelines.

**The privacy rule is enforced, not promised.** The handoff says "no row data, no field
values". :data:`SCHEMA` whitelists the allowed property keys per event and
:func:`record` drops everything else, so a well-meaning caller cannot leak a customer
name into telemetry by adding a keyword argument. Continuous quantities are bucketed
(:func:`bucket_rows`, :func:`bucket_bytes`, :func:`bucket_ms`) so a row count can never
act as a fingerprint for a particular query.

**What is measured here versus computed from the database.** Two of the four headline
metrics are already facts in :class:`~vs_exports.models.ExportRun` — reuse is
``definition_id IS NOT NULL`` and the failure share is a status count — so they are
computed directly rather than duplicated into events that could disagree with the runs
they describe. This module exists for what the database genuinely cannot know: how far
people get through the builder before giving up, and how long a failure takes to
resolve.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


#: How long product telemetry is kept. Analytics is disposable by design; the audit
#: trail it sits beside is not.
RETENTION_DAYS = 180


class Event:
    """The closed set of analytics event names (handoff D10·3)."""

    BUILDER_ENTERED = "builder.entered"
    BUILDER_STEP_VIEWED = "builder.step_viewed"
    BUILDER_STEP_BACK = "builder.step_back"
    BUILDER_ABANDONED = "builder.abandoned"
    BUILDER_COMPLETED = "builder.completed"
    QUICK_EXPORT_USED = "quick_export.used"
    ESTIMATE_VIEWED = "estimate.viewed"
    COLUMNS_CHANGED = "columns.changed"
    VALUES_MODE_CHOSEN = "values_mode.chosen"
    RUN_TRIGGERED = "run.triggered"
    FILE_DOWNLOADED = "file.downloaded"
    FAILURE_VIEWED = "failure.viewed"
    FAILURE_RESOLVED = "failure.resolved"


#: event name → the property keys it may carry. Anything else is dropped by
#: :func:`record`. This is what makes "no row data, no field values" a property of the
#: system rather than a convention people remember to follow.
SCHEMA = {
    Event.BUILDER_ENTERED: {"entry"},                       # wizard | quick | duplicate
    Event.BUILDER_STEP_VIEWED: {"step", "ms_on_step"},
    Event.BUILDER_STEP_BACK: {"from_step", "to_step"},
    Event.BUILDER_ABANDONED: {"last_step"},
    Event.BUILDER_COMPLETED: {"primary_action"},
    Event.QUICK_EXPORT_USED: {"saved_as_definition"},
    Event.ESTIMATE_VIEWED: {"rows_bucket", "size_bucket"},
    Event.COLUMNS_CHANGED: {"count", "from_default"},
    Event.VALUES_MODE_CHOSEN: {"mode"},
    Event.RUN_TRIGGERED: {"trigger", "from_definition"},
    Event.FILE_DOWNLOADED: {"age_days"},
    Event.FAILURE_VIEWED: {"reason_code"},
    Event.FAILURE_RESOLVED: {"ms_to_resolve", "path"},
}

#: Events the browser reports, because only it knows about wizard steps and dwell time.
CLIENT_EVENTS = frozenset({
    Event.BUILDER_ENTERED, Event.BUILDER_STEP_VIEWED, Event.BUILDER_STEP_BACK,
    Event.BUILDER_ABANDONED, Event.BUILDER_COMPLETED, Event.COLUMNS_CHANGED,
    Event.VALUES_MODE_CHOSEN, Event.ESTIMATE_VIEWED,
})


# --------------------------------------------------------------------------- #
# Bucketing                                                                   #
# --------------------------------------------------------------------------- #
#: Row-count buckets. Coarse on purpose: "how big are people's exports" is answerable
#: from these, "which query did Ada run" is not.
_ROW_BUCKETS = ((0, "0"), (100, "1-100"), (1_000, "101-1k"), (10_000, "1k-10k"),
                (100_000, "10k-100k"), (float("inf"), "100k+"))
_SIZE_BUCKETS = ((0, "0"), (102_400, "<100KB"), (1_048_576, "100KB-1MB"),
                 (10_485_760, "1-10MB"), (float("inf"), "10MB+"))
_MS_BUCKETS = ((60_000, "<1m"), (3_600_000, "1m-1h"), (86_400_000, "1h-1d"),
               (604_800_000, "1d-7d"), (float("inf"), "7d+"))


# Bucket a row count.
def bucket_rows(rows) -> str:
    return _bucket(rows, _ROW_BUCKETS)


# Bucket a byte size.
def bucket_bytes(size) -> str:
    return _bucket(size, _SIZE_BUCKETS)


# Bucket a duration in milliseconds.
def bucket_ms(ms) -> str:
    return _bucket(ms, _MS_BUCKETS)


def _bucket(value, table) -> str:
    if value is None:
        return "unknown"
    for ceiling, label in table:
        if value <= ceiling:
            return label
    return table[-1][1]


# --------------------------------------------------------------------------- #
# Recording                                                                   #
# --------------------------------------------------------------------------- #
# Keep only the properties this event is allowed to carry.
def sanitise(name: str, properties: dict | None) -> dict:
    """Drop every key the event's schema does not declare, and every non-scalar value.

    Both halves matter: the key whitelist stops a new field name leaking data, and the
    scalar check stops a whole serialised row arriving under an allowed key.
    """
    allowed = SCHEMA.get(name)
    if not allowed:
        return {}
    clean = {}
    for key, value in (properties or {}).items():
        if key not in allowed:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float, str)) or value is None:
            # Strings are truncated: a bucket label or an enum token is never long, so
            # anything long is a mistake and should not be stored whole.
            clean[key] = value[:64] if isinstance(value, str) else value
    return clean


def record(name, *, tenant, actor=None, properties=None, session_key=""):
    """Write one analytics event. Never raises.

    Telemetry must not be able to fail a user's request: an unknown event name, a
    database hiccup or a serialisation problem is logged and swallowed. That is the
    opposite of the audit trail's contract, and it is deliberate.
    """
    if name not in SCHEMA:
        logger.warning("Unknown export analytics event %r — dropped.", name)
        return None
    try:
        from .models import ExportAnalyticsEvent

        return ExportAnalyticsEvent.objects.create(
            tenant=tenant,
            actor=actor if actor is not None and getattr(actor, "pk", None) else None,
            name=name,
            properties=sanitise(name, properties),
            session_key=(session_key or "")[:64],
        )
    except Exception:                              # pragma: no cover - never blocking
        logger.warning("Export analytics event %r failed to record.", name, exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# The four headline metrics                                                   #
# --------------------------------------------------------------------------- #
def summary(tenant, *, since=None) -> dict:
    """The four numbers the handoff says decide whether this shipped well.

    1. **Reuse** — share of runs started from a saved definition rather than rebuilt.
    2. **Builder abandonment by step** — where people give up.
    3. **Time to resolve a failure** — median from viewing a failure to a good run.
    4. **Unhappy runs** — share ending failed or completed-with-omissions.

    1 and 4 are counted from :class:`~vs_exports.models.ExportRun` because they are
    already facts there; duplicating them into events would only create two numbers
    that can disagree. 2 and 3 come from the events, because nothing else knows them.
    """
    import datetime

    from django.db.models import Count
    from django.utils import timezone

    from .constants import RunStatus
    from .models import ExportAnalyticsEvent, ExportRun

    since = since or (timezone.now() - datetime.timedelta(days=30))
    runs = ExportRun.objects.filter(tenant=tenant, queued_at__gte=since)
    total = runs.count()

    reused = runs.filter(definition__isnull=False).count()
    unhappy = runs.filter(status__in=[
        RunStatus.FAILED, RunStatus.COMPLETED_WITH_OMISSIONS,
    ]).count()

    events = ExportAnalyticsEvent.objects.filter(tenant=tenant, occurred_at__gte=since)
    abandoned = (
        events.filter(name=Event.BUILDER_ABANDONED)
        .values("properties__last_step")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    entered = events.filter(name=Event.BUILDER_ENTERED).count()
    completed = events.filter(name=Event.BUILDER_COMPLETED).count()

    resolutions = list(
        events.filter(name=Event.FAILURE_RESOLVED)
        .values_list("properties__ms_to_resolve", flat=True)
    )
    resolutions = sorted(v for v in resolutions if isinstance(v, (int, float)))

    return {
        "window_start": since,
        "runs": total,
        "reuse": {
            "from_saved_definition": reused,
            "rebuilt": total - reused,
            "share": round(reused / total, 3) if total else None,
        },
        "builder": {
            "entered": entered,
            "completed": completed,
            "completion_share": round(completed / entered, 3) if entered else None,
            "abandoned_by_step": [
                {"step": row["properties__last_step"], "count": row["count"]}
                for row in abandoned
            ],
        },
        "failure_resolution": {
            "resolved": len(resolutions),
            "median_ms": _median(resolutions),
            "median_bucket": bucket_ms(_median(resolutions)),
        },
        "unhappy_runs": {
            "count": unhappy,
            "share": round(unhappy / total, 3) if total else None,
        },
    }


# Median of a pre-sorted list, or None.
def _median(values):
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2
