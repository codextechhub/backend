"""Run lifecycle, downloads and file retention.

The views stay thin; everything that decides *what happens* lives here.

Design points worth knowing before changing anything:

* **Configuration is frozen at trigger time, not at execution time.** ``frozen_config``
  is written when the run row is created, so a definition edited while a run sits in
  the queue does not change what that run produces.
* **Expiry is never a run transition.** :func:`expire_files` purges storage and stamps
  ``purged_at``; the run stays COMPLETED forever. Availability is answered by the file.
* **Downloads are re-authorised against the downloader**, not the owner, and every
  attempt - allowed or refused - is written to :class:`~vs_exports.models.ExportDownload`
  before the bytes move.
"""
from __future__ import annotations

import datetime
import logging
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import F, Q
from django.utils import timezone

from . import analytics, audit
from .catalogue import default_format_options, get_dataset
from .constants import (
    ABANDONED_QUEUED_HOURS,
    ABANDONED_RUNNING_HOURS,
    AuditAction,
    MISSED_WINDOW_GRACE_HOURS,
    PauseReason,
    ScheduleState,
    CONCURRENT_RUN_LIMIT,
    DEFAULT_ROW_CAP,
    DownloadOutcome,
    DownloadRefusal,
    ExportFormat,
    ExportPermission,
    FAILURE_GUIDANCE,
    FailureCode,
    FILE_RETENTION_DAYS,
    FORMAT_MEDIA,
    IDEMPOTENCY_WINDOW_SECONDS,
    RETRYABLE_FAILURE_CODES,
    RunPhase,
    RunStatus,
    RunTrigger,
    SUCCESSFUL_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
)
from .scheduling import should_run_missed
from .engine import (
    Cancelled,
    ExportError,
    may_export_dataset,
    produce,
    resolve_columns,
)
from .models import (
    ExportDownload,
    ExportFile,
    ExportRun,
    ExportSchedule,
)


logger = logging.getLogger(__name__)


class ExportServiceError(Exception):
    """A request the service refuses - surfaced to the caller as a 400."""


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
# Freeze a definition into the configuration a run will use.
def freeze(definition) -> dict:
    """Snapshot everything that decides what the file contains.

    Mandatory on every run: it is the only honest answer to "what produced this file",
    and the only way the UI can show how the definition has drifted since.
    """
    return {
        "definition_id": definition.pk,
        "name": definition.name,
        "dataset_key": definition.dataset_key,
        "entity_id": definition.entity_id,
        "entity_code": definition.entity.code if definition.entity_id else "",
        "columns": list(definition.columns or []),
        "filters": list(definition.filters or []),
        "sort": list(definition.sort or []),
        "format": definition.format,
        "format_options": dict(definition.format_options or {}),
        "values_mode": definition.values_mode,
        "file_name_pattern": definition.file_name_pattern,
        "frozen_at": timezone.now().isoformat(),
    }


# Compare a run's frozen configuration against the definition as it stands now.
def config_drift(run) -> list[dict]:
    """``[{field, then, now}]`` - what changed since this run was produced.

    Powers the run detail's "this differs from the export's current setup in N places".
    Returns an empty list for a quick export, which never had a definition to drift
    from.
    """
    if run.definition_id is None or run.definition is None:
        return []
    now = freeze(run.definition)
    then = run.frozen_config or {}
    watched = (
        "dataset_key", "columns", "filters", "sort", "format", "format_options",
        "values_mode", "file_name_pattern", "name",
    )
    return [
        {"field": key, "then": then.get(key), "now": now.get(key)}
        for key in watched
        if then.get(key) != now.get(key)
    ]


# Drop option keys that do not belong to the chosen format.
def clean_format_options(fmt: str, options: dict | None) -> dict:
    """Keep the options object discriminated by format rather than a flat bag.

    Anything not declared for ``fmt`` in the catalogue is discarded, so switching
    format cannot leave a stale CSV delimiter sitting on an Excel export.
    """
    allowed = default_format_options(fmt)
    merged = dict(allowed)
    merged.update({k: v for k, v in (options or {}).items() if k in allowed})
    return merged


# --------------------------------------------------------------------------- #
# Triggering                                                                  #
# --------------------------------------------------------------------------- #
# Count a tenant's in-flight runs.
def in_flight(tenant) -> int:
    return ExportRun.objects.filter(
        tenant=tenant, status__in=[RunStatus.QUEUED, RunStatus.RUNNING],
    ).count()


def queue_position(run) -> int | None:
    """How many of this tenant's runs are ahead of ``run``, or ``None`` if it started.

    The design asks for this so a wait of more than 30 seconds can be *explained*
    ("your export is 4th in the queue") rather than met with silence - silence is what
    makes people run the same export twice.
    """
    if run.status != RunStatus.QUEUED:
        return None
    ahead = ExportRun.objects.filter(
        tenant_id=run.tenant_id, status=RunStatus.QUEUED, queued_at__lt=run.queued_at,
    ).count()
    # Runs already executing occupy a worker, so they are genuinely ahead too.
    ahead += ExportRun.objects.filter(
        tenant_id=run.tenant_id, status=RunStatus.RUNNING,
    ).count()
    return ahead + 1


# Apply the guards every trigger path shares.
def _accept_run(tenant, client_key):
    """Idempotency and the fair-share cap, in one place for every trigger path.

    Returns the existing run when ``client_key`` repeats inside the window - that is
    the difference between a double-click and a duplicate export - and raises
    :class:`ExportServiceError` when the tenant is at its concurrency cap, so one
    500k-row export cannot starve everyone else's queue.
    """
    if client_key:
        window_start = timezone.now() - datetime.timedelta(seconds=IDEMPOTENCY_WINDOW_SECONDS)
        existing = ExportRun.objects.filter(
            tenant=tenant, client_key=client_key, queued_at__gte=window_start,
        ).order_by("-queued_at").first()
        if existing is not None:
            return existing

    if in_flight(tenant) >= CONCURRENT_RUN_LIMIT:
        raise ExportServiceError(
            f"{CONCURRENT_RUN_LIMIT} exports are already running for your organisation. "
            f"This one will be accepted as soon as one of them finishes."
        )
    return None


def trigger_run(*, definition, actor, trigger=RunTrigger.MANUAL, client_key="",
                config=None, schedule=None, queue=True):
    """Create (or return) a run for ``definition`` and hand it to the queue."""
    tenant = definition.tenant

    existing = _accept_run(tenant, client_key)
    if existing is not None:
        return existing, False

    if definition.is_draft:
        raise ExportServiceError(
            "This export is still a draft. Finish it - it needs a dataset, columns and "
            "any required filters - before it can run."
        )

    run = ExportRun.objects.create(
        tenant=tenant,
        entity=definition.entity,
        definition=definition,
        schedule=schedule,
        frozen_config=config or freeze(definition),
        trigger=trigger,
        requested_by=actor,
        client_key=client_key or "",
    )
    analytics.record(
        analytics.Event.RUN_TRIGGERED, tenant=tenant, actor=actor,
        properties={"trigger": trigger, "from_definition": run.definition_id is not None},
    )
    audit.record(
        AuditAction.RUN_STARTED, actor=actor, tenant=tenant, obj=run,
        label=run.reference,
        metadata={"trigger": trigger, "dataset": run.frozen_config.get("dataset_key")},
    )
    if queue:
        enqueue(run, actor)
    return run, True


def trigger_quick_run(*, config, entity, tenant, actor, client_key="", queue=True):
    """Run a configuration that was never saved - the Quick export path.

    Started from a module list screen with its filters already applied, so there is no
    recipe to reuse and ``definition`` stays null. Everything downstream already copes:
    the run's own ``frozen_config`` is the authority on what it produced, and a run with
    no definition is visible and downloadable only to the person who asked for it.
    """
    existing = _accept_run(tenant, client_key)
    if existing is not None:
        return existing, False

    frozen = {
        "definition_id": None,
        "name": config.get("name") or "Quick export",
        "dataset_key": config["dataset_key"],
        "entity_id": entity.pk if entity is not None else None,
        "entity_code": entity.code if entity is not None else "",
        "columns": list(config.get("columns") or []),
        "filters": list(config.get("filters") or []),
        "sort": list(config.get("sort") or []),
        "format": config.get("format") or ExportFormat.XLSX,
        "format_options": clean_format_options(
            config.get("format") or ExportFormat.XLSX, config.get("format_options"),
        ),
        "values_mode": config.get("values_mode"),
        "file_name_pattern": "",
        "frozen_at": timezone.now().isoformat(),
    }
    run = ExportRun.objects.create(
        tenant=tenant,
        entity=entity,
        definition=None,
        frozen_config=frozen,
        trigger=RunTrigger.QUICK,
        requested_by=actor,
        client_key=client_key or "",
    )
    analytics.record(
        analytics.Event.RUN_TRIGGERED, tenant=tenant, actor=actor,
        properties={"trigger": RunTrigger.QUICK, "from_definition": False},
    )
    analytics.record(
        analytics.Event.QUICK_EXPORT_USED, tenant=tenant, actor=actor,
        # False here by definition: a quick export that got saved would have come
        # through trigger_run with a definition behind it.
        properties={"saved_as_definition": False},
    )
    audit.record(
        AuditAction.RUN_STARTED, actor=actor, tenant=tenant, obj=run,
        label=run.reference,
        metadata={"trigger": RunTrigger.QUICK, "dataset": frozen["dataset_key"]},
    )
    if queue:
        enqueue(run, actor)
    return run, True


# Hand a run to the worker.
def enqueue(run, actor=None):
    """Queue the Celery task and link the resulting BackgroundJob row.

    The job is attributed to the *actor* who asked for it, never to the export's
    subject - that is the platform convention for View Queues and its completion
    notification.
    """
    from core.models import BackgroundJob
    from .tasks import run_export_task

    owner = actor or run.requested_by
    async_result = run_export_task.delay(
        run_id=run.pk,
        _job_owner_id=str(owner.pk) if owner else None,
        _job_tenant_id=run.tenant_id,
        _job_label=f"Export: {run.frozen_config.get('name') or run.reference}",
        _job_kind="export",
        # The export module sends one richer result notification with the run
        # link, row count, omissions and email delivery. Do not add a second
        # generic "background task completed" bell for the same outcome.
        _job_notify=False,
    )
    job = BackgroundJob.objects.filter(celery_task_id=async_result.id).first()
    if job is not None:
        run.background_job = job
        run.save(update_fields=["background_job", "updated_at"])
    return async_result


def retry_run(run, actor):
    """Queue a fresh attempt of a failed run, carrying the attempt count forward.

    Only genuinely retryable failures are offered a retry: re-running a permission or
    filter failure would fail again in exactly the same way, so the UI points at the
    fix instead.
    """
    if run.status != RunStatus.FAILED:
        raise ExportServiceError("Only a failed run can be retried.")
    if run.definition is None:
        raise ExportServiceError(
            "This was a quick export, so there is no saved recipe to retry. Build it "
            "again from the module screen."
        )
    # The rule this function's docstring has always claimed, now enforced. A
    # filter, permission or row-cap failure fails again identically, so retrying
    # it costs the user a second wait and a second notification and changes
    # nothing - the guidance for the code is the actual next step.
    if run.failure_code and run.failure_code not in RETRYABLE_FAILURE_CODES:
        raise ExportServiceError(
            f"{run.failure_message or 'This run failed.'} "
            f"{FAILURE_GUIDANCE.get(run.failure_code, '')}".strip()
        )
    new_run = ExportRun.objects.create(
        tenant=run.tenant,
        entity=run.entity,
        definition=run.definition,
        frozen_config=freeze(run.definition),
        trigger=RunTrigger.RETRY,
        requested_by=actor,
        attempt=run.attempt + 1,
    )
    enqueue(new_run, actor)
    return new_run


def request_cancel(run, actor):
    """Ask a queued or running export to stop.

    Cancellation is cooperative: the flag is set here and the worker notices between
    chunks. A queued run that no worker has picked up yet is finalised immediately, so
    the UI does not show a permanently "cancelling" row.
    """
    if run.is_terminal:
        raise ExportServiceError(f"This run already finished as {run.get_status_display()}.")
    run.cancel_requested = True
    fields = ["cancel_requested", "updated_at"]
    if run.status == RunStatus.QUEUED:
        run.status = RunStatus.CANCELLED
        run.phase = RunPhase.DONE
        run.ended_at = timezone.now()
        fields += ["status", "phase", "ended_at"]
    run.save(update_fields=fields)
    return run


# --------------------------------------------------------------------------- #
# Execution                                                                   #
# --------------------------------------------------------------------------- #
def execute_run(run_id: int):
    """Produce the file for one run. Called by the Celery task; safe to call directly.

    Terminal runs are ignored rather than re-executed, so a duplicate task delivery
    cannot overwrite a finished run's history.
    """
    run = ExportRun.objects.select_related(
        "definition", "entity", "tenant", "requested_by",
    ).filter(pk=run_id).first()
    if run is None or run.status in TERMINAL_RUN_STATUSES:
        return run

    if run.cancel_requested:
        return _finish_cancelled(run)

    run.status = RunStatus.RUNNING
    run.phase = RunPhase.COUNTING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "phase", "started_at", "updated_at"])

    owner = run.definition.owner if run.definition_id else run.requested_by
    if owner is None or getattr(owner, "status", "ACTIVE") != "ACTIVE":
        return _finish_failed(
            run, FailureCode.OWNER_INACTIVE,
            "The owner of this export is no longer active, so it cannot run as them. "
            "An administrator must reassign it.",
        )

    # Progress is written straight to the row: the UI polls, it does not subscribe.
    def _progress(phase, done, total):
        ExportRun.objects.filter(pk=run.pk).update(
            phase=phase, rows_done=done or 0, rows_total=total,
        )

    def _cancelled():
        return ExportRun.objects.filter(pk=run.pk, cancel_requested=True).exists()

    # Everything that decides whether a file exists is inside one guard, so there is
    # no window in which the run can be left RUNNING with a file already written. A
    # run that reaches this function ALWAYS leaves it terminal.
    try:
        body, headers, field_ids, row_count, omissions = produce(
            owner, run.frozen_config, run.scope_context(), run.tenant,
            progress=_progress, is_cancelled=_cancelled,
        )
        file = _store_file(run, body, field_ids, row_count)
    except Cancelled:
        return _finish_cancelled(run)
    except ExportError as exc:
        return _finish_failed(run, exc.code, exc.message)
    except Exception as exc:                       # pragma: no cover - defensive
        return _finish_failed(
            run, FailureCode.INFRASTRUCTURE,
            "This run stopped because of a temporary system problem. Try again; if it "
            "keeps happening, contact support with the reference above.",
            detail=str(exc),
        )

    run.status = (
        RunStatus.COMPLETED_WITH_OMISSIONS if omissions else RunStatus.COMPLETED
    )
    run.phase = RunPhase.DONE
    run.row_count = row_count
    run.rows_done = row_count
    run.rows_total = row_count
    run.omissions = [o.as_dict() for o in omissions]
    run.ended_at = timezone.now()
    run.save(update_fields=[
        "status", "phase", "row_count", "rows_done", "rows_total", "omissions",
        "ended_at", "updated_at",
    ])

    _after_completion(run, owner, file, row_count, omissions)
    return run


# Everything that happens once the run is already finished and saved.
#
# The file exists and the row is terminal before any of this runs, so none of it may
# change the outcome - and none of it may undo the outcome by raising either. A failed
# audit write, a notification the mailer refused, a schedule that could not be advanced:
# each is worth logging and none is worth telling the user their export did not happen.
def _after_completion(run, owner, file, row_count, omissions):
    try:
        _record_sensitive(run, owner)
        audit.record(
            AuditAction.RUN_COMPLETED, actor=run.requested_by, tenant=run.tenant, obj=run,
            label=run.reference,
            metadata={"rows": row_count, "bytes": file.size_bytes, "file": file.name},
        )
        if omissions:
            audit.record(
                AuditAction.RUN_OMITTED_FIELDS, actor=run.requested_by, tenant=run.tenant,
                obj=run, label=run.reference, severity="WARNING",
                metadata={"omissions": run.omissions},
            )
        _record_failure_resolved(run)
        _advance_schedule(run, succeeded=True)
        _notify(run, omissions=bool(omissions))
    except Exception:                              # pragma: no cover - defensive
        logger.exception(
            "Export run %s completed but its follow-up bookkeeping failed", run.reference,
        )


# Close the loop on a failure this run has just put right.
def _record_failure_resolved(run):
    """Measure how long a definition stayed broken, if this run is what fixed it.

    "Median time from failure viewed to failure resolved" is one of the four metrics
    the handoff names, and it is the only one that needs both ends stitched together:
    the failure is a run row, the fix is a *later* run row, and nothing but this
    comparison knows they are the same story. Derived server-side rather than trusted
    from the client, so a user who closes the tab is still counted.
    """
    if run.definition_id is None:
        return
    previous = (
        ExportRun.objects
        .filter(definition_id=run.definition_id, status=RunStatus.FAILED)
        .exclude(pk=run.pk)
        .order_by("-queued_at")
        .first()
    )
    if previous is None or previous.ended_at is None:
        return
    # Only the failure immediately before this run counts: an older failure that was
    # already followed by a success is a story that closed long ago.
    intervening = ExportRun.objects.filter(
        definition_id=run.definition_id,
        status__in=list(SUCCESSFUL_RUN_STATUSES),
        queued_at__gt=previous.queued_at,
    ).exclude(pk=run.pk).exists()
    if intervening:
        return
    elapsed_ms = int((timezone.now() - previous.ended_at).total_seconds() * 1000)
    analytics.record(
        analytics.Event.FAILURE_RESOLVED, tenant=run.tenant, actor=run.requested_by,
        properties={
            "ms_to_resolve": elapsed_ms,
            # Which route the user took back to a good file.
            "path": "retry" if run.trigger == RunTrigger.RETRY else "edit_and_run",
        },
    )


# Persist the produced bytes and open the availability window.
def _store_file(run, body, field_ids, row_count) -> ExportFile:
    config = run.frozen_config
    fmt = config.get("format") or ExportFormat.XLSX
    extension = FORMAT_MEDIA.get(fmt, ("bin", "application/octet-stream"))[0]

    stem = run.definition.render_file_name(run_id=run.reference) if run.definition_id else (
        f"{config.get('dataset_key', 'export').replace('.', '-')}-"
        f"{timezone.localtime():%Y-%m-%d}"
    )
    name = f"{stem}.{extension}"
    # Storage keys are opaque and unguessable: the download endpoint is the only way
    # in, so a leaked key must not be a second door.
    storage_name = default_storage.save(
        f"exports/{run.tenant_id}/{uuid.uuid4().hex}.{extension}", ContentFile(body),
    )
    return ExportFile.objects.create(
        run=run,
        name=name,
        format=fmt,
        storage_name=storage_name,
        size_bytes=len(body),
        row_count=row_count,
        columns_produced=field_ids,
        available_until=ExportFile.default_expiry(),
    )


# Record the sensitive-field audit event for a completed run.
def _record_sensitive(run, owner):
    dataset = get_dataset(run.frozen_config.get("dataset_key"))
    if dataset is None:
        return
    fields, _ = resolve_columns(owner, dataset, run.frozen_config.get("columns"), run.tenant)
    audit.record_sensitive_fields(run, [f for f in fields if f.sensitive], actor=owner)


# Finalise a run that failed.
def _finish_failed(run, code, message, *, detail=""):
    run.status = RunStatus.FAILED
    run.phase = RunPhase.DONE
    run.failure_code = code
    run.failure_message = message
    run.ended_at = timezone.now()
    run.save(update_fields=[
        "status", "phase", "failure_code", "failure_message", "ended_at", "updated_at",
    ])
    audit.record(
        AuditAction.RUN_FAILED, actor=run.requested_by, tenant=run.tenant, obj=run,
        label=run.reference, severity="CRITICAL", status="FAILED",
        metadata={"code": code, "detail": detail[:500]},
    )
    _advance_schedule(run, succeeded=False, detail=message)
    _notify(run, failed=True)
    return run


# Roll a schedule forward after one of its runs finished.
def _advance_schedule(run, *, succeeded, detail=""):
    """Move the schedule on, and pause it if this run was the third failure in a row.

    Kept out of the finalisers themselves so both terminal paths share one rule, and
    so a run triggered by hand never touches a schedule it did not come from.
    """
    if run.schedule_id is None:
        return
    schedule = run.schedule
    if succeeded:
        schedule.register_success()
    else:
        schedule.register_failure(detail)
        if schedule.state == ScheduleState.PAUSED:
            audit.record(
                AuditAction.SCHEDULE_PAUSED, tenant=run.tenant, obj=schedule,
                label=schedule.definition.name, severity="WARNING",
                metadata={"reason": schedule.pause_reason, "run": run.reference},
            )
    schedule.last_run = run
    schedule.save(update_fields=["last_run", "updated_at"])
    # A paused schedule keeps its stale next_run_at so resuming can recompute it.
    if schedule.state == ScheduleState.ACTIVE:
        schedule.reschedule()


# Finalise a cancelled run.
def _finish_cancelled(run):
    """No partial file is ever kept - nothing was stored before this point."""
    run.status = RunStatus.CANCELLED
    run.phase = RunPhase.DONE
    run.ended_at = timezone.now()
    run.save(update_fields=["status", "phase", "ended_at", "updated_at"])
    return run


# Tell the owner what happened.
def _notify(run, *, failed=False, omissions=False):
    """Notify the owner on completion, failure and omissions.

    Which channels actually fire is the event type's business, not this function's -
    both keys support in-app and email, and a tenant can switch the email channel off
    per event. Here we only decide *which* event and what it says.
    """
    from vs_notifications.notify import send_notification

    recipient = run.definition.owner if run.definition_id else run.requested_by
    if recipient is None:
        return
    key = "export.run_failed" if failed else "export.run_completed"
    label = run.frozen_config.get("name") or run.reference
    detail = (
        f"{run.failure_message} {run.failure_guidance}".strip() if failed
        else "Some columns were left out - open the run to see which." if omissions
        else ""
    )
    try:
        send_notification(
            event_key=key,
            context={
                "label": f"Export: {label}",
                "export_name": label,
                "reference": run.reference,
                "error": detail,
                "rows": run.row_count or 0,
            },
            recipients=[recipient],
            tenant=run.tenant,
            # Lets the bell deep-link to THIS run rather than the Files list -
            # a failure notice is only useful next to the thing that failed.
            metadata={"export_run_id": run.pk},
        )
    except Exception:                              # pragma: no cover - best effort
        # A notification failure must never turn a produced file into a failed run.
        logger.warning("Export notification failed for run %s", run.pk, exc_info=True)


# --------------------------------------------------------------------------- #
# Deliveries                                                                  #
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Downloads                                                                   #
# --------------------------------------------------------------------------- #
def authorise_download(file, user, tenant):
    """Decide whether ``user`` may take ``file`` now. Returns ``(ok, refusal_reason)``.

    Checked against the run's *frozen* entity and dataset - not the definition's
    current ones - because the file contains what it contains, and access to it must be
    judged on that. Expiry and purge are checked first: they are properties of the
    file, so they apply to the owner too.
    """
    if file.is_purged:
        return False, DownloadRefusal.PURGED
    if file.is_expired:
        return False, DownloadRefusal.EXPIRED

    run = file.run
    if run.tenant_id != getattr(tenant, "pk", tenant):
        return False, DownloadRefusal.NO_ENTITY_ACCESS

    # Entity access is only a question for a run that had one. A tenant-scoped run is
    # already bounded by the tenant check above.
    if run.entity_id is not None:
        from vs_finance.models import LedgerEntity

        if not LedgerEntity.objects.filter(pk=run.entity_id, tenant=tenant).exists():
            return False, DownloadRefusal.NO_ENTITY_ACCESS

    dataset = get_dataset(run.frozen_config.get("dataset_key"))
    if dataset is None or not may_export_dataset(user, dataset, tenant):
        return False, DownloadRefusal.NO_DATASET_ACCESS

    definition = run.definition
    if definition is not None:
        is_owner = definition.owner_id == user.pk
        is_requester = run.requested_by_id == user.pk
        is_shared = definition.shares.filter(user=user).exists()
        if not (is_owner or is_requester or is_shared):
            return False, DownloadRefusal.NOT_SHARED
    elif run.requested_by_id != user.pk:
        return False, DownloadRefusal.NOT_SHARED

    return True, ""


def log_download(file, user, *, outcome, reason="", ip=""):
    """Write the download record and keep the file's counter in step.

    Every attempt is logged before the bytes move, so a refused attempt leaves the same
    trail as an allowed one - which is exactly the question a compliance review asks.
    """
    record = ExportDownload.objects.create(
        file=file, user=user, outcome=outcome, refusal_reason=reason or "", ip_address=ip or "",
    )
    if outcome == DownloadOutcome.ALLOWED:
        # F() so two people downloading at once cannot lose a count.
        ExportFile.objects.filter(pk=file.pk).update(download_count=F("download_count") + 1)
        # How old files are when people fetch them tells us whether 30 days is the
        # right retention window - the one product question the run rows cannot answer.
        analytics.record(
            analytics.Event.FILE_DOWNLOADED, tenant=file.run.tenant, actor=user,
            properties={"age_days": (timezone.now() - file.created_at).days},
        )
    audit.record(
        AuditAction.FILE_DOWNLOADED if outcome == DownloadOutcome.ALLOWED
        else AuditAction.FILE_DOWNLOAD_REFUSED,
        actor=user, tenant=file.run.tenant, obj=file, label=file.name,
        severity="INFO" if outcome == DownloadOutcome.ALLOWED else "WARNING",
        # A refused download is DENIED, not FAILED: nothing broke, access was declined.
        status="SUCCESS" if outcome == DownloadOutcome.ALLOWED else "DENIED",
        metadata={"run": file.run.reference, "reason": reason},
    )
    return record


# --------------------------------------------------------------------------- #
# Expiry                                                                      #
# --------------------------------------------------------------------------- #
def expire_files(*, now=None) -> int:
    """Hard-delete storage for files past their availability. Returns the count.

    The run record and its audit trail survive indefinitely: this marks the file
    purged, it does not rewrite history. Deliberately idempotent, so a missed night is
    caught up by the next one without double-counting.
    """
    now = now or timezone.now()
    stale = ExportFile.objects.filter(available_until__lte=now, purged_at__isnull=True)
    purged = 0
    for file in stale.select_related("run", "run__tenant").iterator():
        _purge_file(file, now=now, reason="expired")
        purged += 1
    return purged


# Delete one file's bytes and mark the row purged. History is never rewritten.
def _purge_file(file, *, now, reason):
    try:
        default_storage.delete(file.storage_name)
    except Exception:                              # pragma: no cover - storage best effort
        pass
    file.purged_at = now
    file.save(update_fields=["purged_at", "updated_at"])
    audit.record(
        AuditAction.FILE_EXPIRED, tenant=file.run.tenant, obj=file, label=file.name,
        metadata={"run": file.run.reference, "reason": reason},
    )


def sweep_abandoned_runs(*, now=None) -> dict:
    """Finish runs whose worker never reported back. Returns what it closed.

    The safety net for the one way a run can still be stranded: the process holding it
    dies, so none of :func:`execute_run`'s own finalisers ever run. Everything else
    about the export is already durable - the row, the frozen config, the audit trail -
    and this is what stops that row spinning forever.

    Two ways a swept run ends, and the difference matters to the person waiting:

    * asked to be cancelled - it ends CANCELLED, silently. They already know; a failure
      notice for something they themselves stopped is noise.
    * anything else - it ends FAILED with :attr:`FailureCode.INFRASTRUCTURE`, which is
      retryable, carries "try again" guidance, and notifies the owner. A run that died
      is not a run that never happened.

    Any file the dead worker had already stored is purged rather than handed over. The
    bytes are complete (``_store_file`` writes in one go), but the *run* never recorded
    what is in them - no row count of its own, no omission list - and offering a file
    this app cannot describe is precisely the silence the Export Centre exists to
    prevent. Failing the run instead costs one more click and produces a file that
    comes with its own account of itself.

    Idempotent: a swept run is terminal, so the next pass does not see it.
    """
    now = now or timezone.now()
    running_cutoff = now - datetime.timedelta(hours=ABANDONED_RUNNING_HOURS)
    queued_cutoff = now - datetime.timedelta(hours=ABANDONED_QUEUED_HOURS)

    stranded = ExportRun.objects.filter(
        Q(status=RunStatus.RUNNING, started_at__lt=running_cutoff)
        # A RUNNING row always has started_at (execute_run writes both in one save), but
        # the fallback keeps the net from having a hole in it: no non-terminal row can
        # be invisible to this query forever, whatever wrote it.
        | Q(status=RunStatus.RUNNING, started_at__isnull=True, queued_at__lt=running_cutoff)
        | Q(status=RunStatus.QUEUED, queued_at__lt=queued_cutoff)
    ).select_related("definition", "schedule", "tenant", "requested_by")

    closed = {"failed": 0, "cancelled": 0, "files_purged": 0}
    for run in stranded.iterator():
        file = ExportFile.objects.filter(run=run, purged_at__isnull=True).first()
        if file is not None:
            _purge_file(file, now=now, reason="run_abandoned")
            closed["files_purged"] += 1

        if run.cancel_requested:
            _finish_cancelled(run)
            closed["cancelled"] += 1
            continue

        _finish_failed(
            run, FailureCode.INFRASTRUCTURE,
            "This run stopped without finishing - whatever was producing it never "
            "reported back.",
            detail=f"swept after {run.status.lower()} since "
                   f"{(run.started_at or run.queued_at).isoformat()}",
        )
        closed["failed"] += 1
    logger.info("Export sweeper closed %s", closed)
    return closed


# --------------------------------------------------------------------------- #
# Capabilities                                                                #
# --------------------------------------------------------------------------- #
def capabilities(user, tenant):
    """What this user may do, as flags - so the UI can disable with a reason.

    The handoff is emphatic about this: capability flags rather than booleans buried in
    errors are what make the permission experience humane. Failing at submit is the
    behaviour this endpoint exists to prevent.
    """
    from vs_finance.models import LedgerEntity
    from vs_rbac.evaluator import has_permission
    from vs_rbac.permissions import is_vision_super_admin

    def _can(key):
        return is_vision_super_admin(user) or has_permission(user, key, tenant=tenant)

    entities = LedgerEntity.objects.filter(tenant=tenant, is_active=True)
    return {
        "can_create": _can(ExportPermission.DEFINITION_CREATE),
        "can_run": _can(ExportPermission.RUN_CREATE),
        "can_share": _can(ExportPermission.DEFINITION_SHARE),
        "can_export_sensitive": _can(ExportPermission.SENSITIVE_EXPORT),
        "can_view_activity": _can(ExportPermission.ACTIVITY_VIEW),
        "allowed_entities": [
            {"id": e.pk, "code": e.code, "name": e.name} for e in entities
        ],
        "row_cap": DEFAULT_ROW_CAP,
        "concurrent_run_limit": CONCURRENT_RUN_LIMIT,
        "in_flight": in_flight(tenant),
        "retention_days": FILE_RETENTION_DAYS,
    }


# --------------------------------------------------------------------------- #
# Schedules                                                                   #
# --------------------------------------------------------------------------- #
def dispatch_due_schedules(*, now=None) -> dict:
    """Trigger every active schedule whose moment has come. Returns a small summary.

    Four things happen here and nowhere else:

    * a window missed through an outage runs once on recovery inside the grace
      period, and is otherwise skipped and rolled forward rather than caught up;
    * a schedule whose owner has been deactivated pauses instead of running as them,
      because an unattended run must never read more than its owner could;
    * a tenant at its concurrency cap is left alone with its ``next_run_at`` intact,
      so the next tick retries rather than losing the window;
    * every started run is attributed to the definition's owner, not to whoever
      created the schedule.
    """
    now = now or timezone.now()
    due = ExportSchedule.objects.select_related(
        "definition", "definition__owner", "definition__entity", "definition__tenant",
    ).filter(state=ScheduleState.ACTIVE, next_run_at__lte=now)

    started, skipped, paused, deferred = 0, 0, 0, 0
    for schedule in due:
        if not should_run_missed(schedule.next_run_at, now=now):
            skipped += 1
            logger.info(
                "Export schedule %s missed its %s window by more than %sh; skipping.",
                schedule.pk, schedule.next_run_at, MISSED_WINDOW_GRACE_HOURS,
            )
            schedule.reschedule(after=now)
            continue

        definition = schedule.definition
        owner = definition.owner
        if owner is None or getattr(owner, "status", "ACTIVE") != "ACTIVE":
            schedule.state = ScheduleState.PAUSED
            schedule.pause_reason = PauseReason.OWNER_INACTIVE
            schedule.pause_detail = (
                "The owner of this export is no longer active. An administrator must "
                "reassign it before it can run again."
            )
            schedule.save(update_fields=[
                "state", "pause_reason", "pause_detail", "updated_at",
            ])
            audit.record(
                AuditAction.SCHEDULE_PAUSED, tenant=definition.tenant, obj=schedule,
                label=definition.name, severity="WARNING",
                metadata={"reason": PauseReason.OWNER_INACTIVE},
            )
            paused += 1
            continue

        try:
            trigger_run(
                definition=definition, actor=owner,
                trigger=RunTrigger.SCHEDULED, schedule=schedule,
            )
        except ExportServiceError:
            # At the cap. Leave next_run_at where it is so the next tick tries again.
            deferred += 1
            continue
        started += 1
        schedule.refresh_from_db()
        if schedule.state == ScheduleState.ACTIVE:
            schedule.reschedule(after=now)

    return {
        "started": started, "skipped": skipped,
        "paused": paused, "deferred": deferred,
    }


def pause_schedule(schedule, actor, *, detail=""):
    """Stop a schedule until somebody resumes it."""
    schedule.state = ScheduleState.PAUSED
    schedule.pause_reason = PauseReason.BY_PERSON
    schedule.pause_detail = detail[:300]
    schedule.save(update_fields=[
        "state", "pause_reason", "pause_detail", "updated_at",
    ])
    audit.record(
        AuditAction.SCHEDULE_PAUSED, actor=actor, tenant=schedule.definition.tenant,
        obj=schedule, label=schedule.definition.name,
        metadata={"reason": PauseReason.BY_PERSON, "detail": detail[:300]},
    )
    return schedule


def resume_schedule(schedule, actor):
    """Clear the pause, reset the failure counter and recompute the next occurrence.

    Resetting the counter matters: without it a schedule paused by three failures
    would pause again on its very next failure, however long it had run cleanly in
    between.
    """
    schedule.state = ScheduleState.ACTIVE
    schedule.pause_reason = ""
    schedule.pause_detail = ""
    schedule.consecutive_failures = 0
    schedule.save(update_fields=[
        "state", "pause_reason", "pause_detail", "consecutive_failures", "updated_at",
    ])
    schedule.reschedule()
    audit.record(
        AuditAction.SCHEDULE_RESUMED, actor=actor, tenant=schedule.definition.tenant,
        obj=schedule, label=schedule.definition.name,
    )
    return schedule
