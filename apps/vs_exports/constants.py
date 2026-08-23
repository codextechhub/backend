"""Closed vocabularies for the Export Centre.

The design handoff (Deliverable 10) makes several of these sets *normative* - the UI
renders exactly these tokens and never invents its own. In particular:

* :class:`RunStatus` is the closed set ``queued · running · completed ·
  completed_with_omissions · failed · cancelled``. ``expired`` is deliberately **not**
  a run status: expiry is a property of the *file*, derived at read time, so history is
  never overwritten to represent it (see :class:`vs_exports.models.ExportFile`).
* :class:`FailureCode` is the machine reason code carried by every failure. The API
  returns the code, a user-safe message and a support reference; the UI maps the code
  to a recommended action and never shows a traceback.
* :class:`OmissionCode` explains a ``completed_with_omissions`` run. The reason list is
  structured because the UI renders it - it must never have to infer it from prose.
"""
from __future__ import annotations

from django.db import models


# --------------------------------------------------------------------------- #
# Runs                                                                        #
# --------------------------------------------------------------------------- #
class RunStatus(models.TextChoices):
    """Lifecycle of one export run. Terminal states never change afterwards."""

    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    COMPLETED = "COMPLETED", "Completed"
    COMPLETED_WITH_OMISSIONS = "COMPLETED_WITH_OMISSIONS", "Completed with omissions"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"


#: Statuses after which a run is finished and must not be mutated again.
TERMINAL_RUN_STATUSES = frozenset({
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_OMISSIONS,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
})

#: Statuses that produced a downloadable file.
SUCCESSFUL_RUN_STATUSES = frozenset({
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_OMISSIONS,
})


class RunTrigger(models.TextChoices):
    """What caused a run - shown verbatim in the Files list."""

    MANUAL = "MANUAL", "Manual"
    SCHEDULED = "SCHEDULED", "Scheduled"
    QUICK = "QUICK", "Quick export"
    RETRY = "RETRY", "Retry"
    API = "API", "API"


class RunPhase(models.TextChoices):
    """Coarse progress phase, so an indeterminate bar can still name what it is doing."""

    QUEUED = "QUEUED", "Waiting for a worker"
    COUNTING = "COUNTING", "Counting rows"
    READING = "READING", "Reading rows"
    BUILDING = "BUILDING", "Building file"
    DONE = "DONE", "Done"


class FailureCode(models.TextChoices):
    """Machine reason codes. Every failure maps to exactly one.

    ``retryable`` behaviour lives in :data:`RETRYABLE_FAILURE_CODES` - data and
    permission failures never auto-retry, because retrying them changes nothing.
    """

    DATASET_WITHDRAWN = "DATASET_WITHDRAWN", "The dataset is no longer published"
    DATASET_FORBIDDEN = "DATASET_FORBIDDEN", "No longer allowed to export this dataset"
    ENTITY_FORBIDDEN = "ENTITY_FORBIDDEN", "No longer allowed to use this entity"
    FILTER_INVALID = "FILTER_INVALID", "A filter this export uses no longer exists"
    REQUIRED_FILTER_MISSING = "REQUIRED_FILTER_MISSING", "A required filter is not set"
    NO_COLUMNS = "NO_COLUMNS", "Every selected column has become unavailable"
    ROW_CAP_EXCEEDED = "ROW_CAP_EXCEEDED", "The result is larger than the row cap"
    #: Historical only. A wide date range no longer fails a run - it is advised
    #: on in the estimate (``WIDE_DATE_RANGE``) and the row cap is the real
    #: ceiling. Kept because runs recorded before that change still carry it and
    #: their detail screens must keep rendering.
    DATE_SPAN_EXCEEDED = "DATE_SPAN_EXCEEDED", "The date range is wider than the dataset allows"
    OWNER_INACTIVE = "OWNER_INACTIVE", "The owner of this export is no longer active"
    INFRASTRUCTURE = "INFRASTRUCTURE", "A temporary system problem"
    UNKNOWN = "UNKNOWN", "An unexpected problem"


#: Only transient infrastructure faults auto-retry (3 attempts, invisible to the user).
RETRYABLE_FAILURE_CODES = frozenset({FailureCode.INFRASTRUCTURE, FailureCode.UNKNOWN})

#: code → the action the UI should recommend. Never a stack trace, always a next step.
FAILURE_GUIDANCE = {
    FailureCode.DATASET_WITHDRAWN: (
        "Your administrators withdrew this dataset. Pick a different dataset, or ask "
        "them to publish it again."
    ),
    FailureCode.DATASET_FORBIDDEN: (
        "Your access to this dataset was removed. Request access, then run the export "
        "again."
    ),
    FailureCode.ENTITY_FORBIDDEN: (
        "Your access to this entity was removed. Request access to the entity, then "
        "run the export again."
    ),
    FailureCode.FILTER_INVALID: (
        "Edit the export and replace the filter that no longer exists, then run it "
        "again."
    ),
    FailureCode.REQUIRED_FILTER_MISSING: (
        "Edit the export and set the filter this dataset requires."
    ),
    FailureCode.NO_COLUMNS: (
        "Every column you chose is now restricted. Request access to the fields, or "
        "edit the export to use columns you can read."
    ),
    FailureCode.ROW_CAP_EXCEEDED: (
        "Narrow the filters - a shorter date range is usually enough - and run it again."
    ),
    FailureCode.DATE_SPAN_EXCEEDED: (
        "Shorten the date range to the maximum this dataset allows, then run it again."
    ),
    FailureCode.OWNER_INACTIVE: (
        "An administrator must reassign this export to an active owner."
    ),
    FailureCode.INFRASTRUCTURE: (
        "This was a temporary system problem. Try again; if it keeps happening, contact "
        "support with the reference above."
    ),
    FailureCode.UNKNOWN: (
        "Contact support with the reference above so we can look at this run."
    ),
}


class OmissionCode(models.TextChoices):
    """Why part of a ``completed_with_omissions`` result was left out."""

    FIELD_FORBIDDEN = "FIELD_FORBIDDEN", "Column left out - no access to the field"
    FIELD_WITHDRAWN = "FIELD_WITHDRAWN", "Column left out - the field no longer exists"
    ROW_CAP_HIT = "ROW_CAP_HIT", "Rows left out - the row cap was reached"


# --------------------------------------------------------------------------- #
# Schedules                                                                   #
# --------------------------------------------------------------------------- #
class Recurrence(models.TextChoices):
    """How often a schedule fires. ONCE is a future date, not a repeat."""

    ONCE = "ONCE", "Once, at a future date"
    DAILY = "DAILY", "Daily"
    WEEKLY = "WEEKLY", "Weekly"
    MONTHLY = "MONTHLY", "Monthly"
    QUARTERLY = "QUARTERLY", "Quarterly"


class ScheduleState(models.TextChoices):
    """PAUSED means something went wrong and a person must act. FINISHED means the
    series simply ran out - a ONCE schedule that fired, or one past its end date.
    Keeping them apart is what stops a completed schedule sitting in the paused list
    looking like a fault."""

    ACTIVE = "ACTIVE", "Active"
    PAUSED = "PAUSED", "Paused"
    FINISHED = "FINISHED", "Finished"


class PauseReason(models.TextChoices):
    BY_PERSON = "BY_PERSON", "Paused by a person"
    CONSECUTIVE_FAILURES = "CONSECUTIVE_FAILURES", "Paused after repeated failures"
    OWNER_INACTIVE = "OWNER_INACTIVE", "Paused because the owner was deactivated"


#: A schedule pauses itself after this many consecutive failed runs, so a broken
#: export stops filling the Files list with identical failures.
MAX_CONSECUTIVE_FAILURES = 3

#: A window missed through an outage still runs if recovery is inside this many hours.
#: Beyond it the window is skipped and reported - six stale nightly files help nobody.
MISSED_WINDOW_GRACE_HOURS = 6


# --------------------------------------------------------------------------- #
# Definitions and files                                                       #
# --------------------------------------------------------------------------- #
class DatasetScope(models.TextChoices):
    """What boundary a dataset's rows live inside - a property of the *dataset*.

    The Export Centre is a platform feature, not a Finance one. Finance, Payments and
    Procurement rows belong to a :class:`~vs_finance.models.LedgerEntity` (a set of
    books), and mixing two entities into one file would be an accounting error. Audit
    events, users and configuration have no entity at all - they belong to the tenant.

    Declaring the boundary on the dataset is what lets one Export Centre serve both
    without the platform layer assuming everything is finance.
    """

    ENTITY = "ENTITY", "One ledger entity"
    TENANT = "TENANT", "The whole tenant"


class ExportFormat(models.TextChoices):
    """Formats v1.0 produces. PDF and JSON are explicitly post-v1."""

    CSV = "csv", "CSV (.csv)"
    XLSX = "xlsx", "Excel (.xlsx)"


#: format → (file extension, MIME content type).
FORMAT_MEDIA = {
    ExportFormat.CSV: ("csv", "text/csv"),
    ExportFormat.XLSX: (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
}


class ValuesMode(models.TextChoices):
    """Whether cells are rendered for a person or for another system to import."""

    PEOPLE = "people", "For people to read"
    SYSTEM = "system", "For another system to import"


class Sharing(models.TextChoices):
    PRIVATE = "PRIVATE", "Private"
    SHARED = "SHARED", "Shared"




class DownloadOutcome(models.TextChoices):
    ALLOWED = "ALLOWED", "Allowed"
    REFUSED = "REFUSED", "Refused"


class DownloadRefusal(models.TextChoices):
    """Why a download was refused - logged on every refused attempt."""

    EXPIRED = "EXPIRED", "The file passed its availability date"
    PURGED = "PURGED", "The file has been deleted from storage"
    NO_ENTITY_ACCESS = "NO_ENTITY_ACCESS", "No access to the run's entity"
    NO_DATASET_ACCESS = "NO_DATASET_ACCESS", "No access to the run's dataset"
    NOT_SHARED = "NOT_SHARED", "The export is not shared with this user"


# --------------------------------------------------------------------------- #
# Platform limits                                                             #
# --------------------------------------------------------------------------- #
#: Days a produced file stays downloadable, counted from run completion.
FILE_RETENTION_DAYS = 30

#: Hard ceiling on rows in one export, applied on top of the dataset's own cap.
DEFAULT_ROW_CAP = 500_000

#: Above this, the review step warns (but never blocks).
ROW_WARNING_THRESHOLD = 250_000

#: Runs a single tenant may have in flight at once - the fair-share rule.
CONCURRENT_RUN_LIMIT = 3

#: How long a run may sit in each non-terminal state before the sweeper decides its
#: worker is never coming back. Nothing in the process can strand a run any more -
#: :func:`vs_exports.services.execute_run` always leaves the row terminal - but a
#: worker killed mid-run never reaches that code at all, and an unswept run is worse
#: than a failed one: it spins forever on the Files screen with nothing to cancel or
#: retry, and it counts against :data:`CONCURRENT_RUN_LIMIT`, so three of them stop
#: the whole tenant exporting.
#:
#: Both numbers are safety nets, not deadlines. They sit far beyond how long an
#: export of :data:`DEFAULT_ROW_CAP` rows takes, because failing a run that was only
#: slow costs a user their file, while leaving a dead one an hour longer costs
#: nothing. QUEUED gets the longer window: a run waiting behind others is doing
#: exactly what it should, and only a queue with no worker at all stays there.
ABANDONED_RUNNING_HOURS = 2
ABANDONED_QUEUED_HOURS = 6

#: A repeated run request carrying the same client key inside this window is the
#: same run, not a second one.
IDEMPOTENCY_WINDOW_SECONDS = 60

#: A quick export whose ESTIMATE is at or below this runs inline and hands the
#: file straight back, instead of queueing and making the user go and fetch it.
#: Small files are the common case from a filtered list screen, and a round trip
#: through the queue for a 40 KB spreadsheet is all cost and no benefit.
#:
#: The ceiling exists because an inline run occupies a web worker for its whole
#: duration - that is the resource being protected, which is why the server
#: re-estimates and enforces this itself rather than trusting the caller's claim
#: that the file will be small.
SYNC_EXPORT_MAX_BYTES = 4 * 1024 * 1024

#: Rows returned by the preview endpoint.
PREVIEW_ROWS = 10

#: Counting stops being exact above this many rows; the estimate becomes bucketed
#: rather than making the builder wait on a full table scan.
EXACT_COUNT_LIMIT = 100_000


# --------------------------------------------------------------------------- #
# Permissions and audit                                                       #
# --------------------------------------------------------------------------- #
#: Module bucket in the audit trail. Must be a registered
#: :class:`vs_audit.models.AuditModuleKey` - the vocabulary is validated on save, and
#: an unregistered key is silently swallowed, losing the event.
MODULE_KEY = "EXPORTS"


class ExportPermission:
    """RBAC keys enforced by the views (seeded by ``seed_exports_permissions``)."""

    CATALOGUE_VIEW = "exports.catalogue.view"
    DEFINITION_VIEW = "exports.definition.view"
    DEFINITION_CREATE = "exports.definition.create"
    DEFINITION_UPDATE = "exports.definition.update"
    DEFINITION_DELETE = "exports.definition.delete"
    DEFINITION_SHARE = "exports.definition.share"
    RUN_VIEW = "exports.run.view"
    RUN_CREATE = "exports.run.create"
    RUN_CANCEL = "exports.run.cancel"
    FILE_DOWNLOAD = "exports.file.download"
    SCHEDULE_VIEW = "exports.schedule.view"
    SCHEDULE_CREATE = "exports.schedule.create"
    SCHEDULE_MANAGE = "exports.schedule.manage"
    #: Required *in addition* to the dataset's own key to include a sensitive field.
    SENSITIVE_EXPORT = "exports.sensitive_field.export"
    #: Admin-only: read other people's export activity. Reading it is itself audited.
    ACTIVITY_VIEW = "exports.activity.view"


class AuditAction:
    """Compliance audit events. Immutable and retained beyond file expiry.

    Each name is the design's event (``export.definition.created`` and friends); each
    value is the token registered in :class:`vs_audit.models.AuditActionType`, because
    that vocabulary is closed and validated when the event is saved. Adding an event
    here means adding it there too - otherwise ``emit_audit_event`` swallows the
    validation error and the event is simply never recorded.
    """

    DEFINITION_CREATED = "EXPORT_DEFINITION_CREATED"
    DEFINITION_UPDATED = "EXPORT_DEFINITION_UPDATED"
    DEFINITION_DELETED = "EXPORT_DEFINITION_DELETED"
    DEFINITION_SHARED = "EXPORT_DEFINITION_SHARED"
    SENSITIVE_FIELD_INCLUDED = "EXPORT_SENSITIVE_FIELD_INCLUDED"
    SCHEDULE_CREATED = "EXPORT_SCHEDULE_CREATED"
    SCHEDULE_PAUSED = "EXPORT_SCHEDULE_PAUSED"
    SCHEDULE_RESUMED = "EXPORT_SCHEDULE_RESUMED"
    RUN_STARTED = "EXPORT_REQUESTED"
    RUN_COMPLETED = "EXPORT_COMPLETED"
    RUN_OMITTED_FIELDS = "EXPORT_RUN_OMITTED_FIELDS"
    RUN_FAILED = "EXPORT_FAILED"
    FILE_DOWNLOADED = "EXPORT_FILE_DOWNLOADED"
    FILE_DOWNLOAD_REFUSED = "EXPORT_FILE_DOWNLOAD_REFUSED"
    FILE_EXPIRED = "EXPORT_FILE_EXPIRED"
    ADMIN_VIEWED_ACTIVITY = "EXPORT_ADMIN_VIEWED_ACTIVITY"
