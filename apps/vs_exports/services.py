"""Run lifecycle, downloads and deliveries.

The views stay thin; everything that decides *what happens* lives here.

Design points worth knowing before changing anything:

* **Configuration is frozen at trigger time, not at execution time.** ``frozen_config``
  is written when the run row is created, so a definition edited while a run sits in
  the queue does not change what that run produces.
* **Expiry is never a run transition.** :func:`expire_files` purges storage and stamps
  ``purged_at``; the run stays COMPLETED forever. Availability is answered by the file.
* **Downloads are re-authorised against the downloader**, not the owner, and every
  attempt — allowed or refused — is written to :class:`~vs_exports.models.ExportDownload`
  before the bytes move.
"""
from __future__ import annotations

import datetime
import logging
import secrets
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import F
from django.utils import timezone

from . import analytics, audit
from .catalogue import default_format_options, get_dataset
from .constants import (
    AuditAction,
    CONCURRENT_RUN_LIMIT,
    DEFAULT_ROW_CAP,
    DeliveryState,
    Destination,
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
from .engine import (
    Cancelled,
    ExportError,
    may_export_dataset,
    produce,
    resolve_columns,
)
from .models import (
    ExportDelivery,
    ExportDownload,
    ExportFile,
    ExportRun,
)


logger = logging.getLogger(__name__)


class ExportServiceError(Exception):
    """A request the service refuses — surfaced to the caller as a 400."""


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
    """``[{field, then, now}]`` — what changed since this run was produced.

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
    ("your export is 4th in the queue") rather than met with silence — silence is what
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

    Returns the existing run when ``client_key`` repeats inside the window — that is
    the difference between a double-click and a duplicate export — and raises
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
                config=None, queue=True):
    """Create (or return) a run for ``definition`` and hand it to the queue."""
    tenant = definition.tenant

    existing = _accept_run(tenant, client_key)
    if existing is not None:
        return existing, False

    if definition.is_draft:
        raise ExportServiceError(
            "This export is still a draft. Finish it — it needs a dataset, columns and "
            "any required filters — before it can run."
        )

    run = ExportRun.objects.create(
        tenant=tenant,
        entity=definition.entity,
        definition=definition,
        frozen_config=config or freeze(definition),
        trigger=trigger,
        requested_by=actor,
        client_key=client_key or "",
    )
    _open_deliveries(run)
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
    """Run a configuration that was never saved — the Quick export path.

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
    subject — that is the platform convention for View Queues and its completion
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
    # nothing — the guidance for the code is the actual next step.
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

    try:
        body, headers, field_ids, row_count, omissions = produce(
            owner, run.frozen_config, run.scope_context(), run.tenant,
            progress=_progress, is_cancelled=_cancelled,
        )
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

    file = _store_file(run, body, field_ids, row_count)
    _record_sensitive(run, owner)

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
    _dispatch_deliveries(run, file)
    _notify(run, omissions=bool(omissions))
    return run


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
    # Any pending delivery is skipped rather than left pending forever — a run with no
    # file has nothing to deliver, and the UI must be able to say so.
    ExportDelivery.objects.filter(run=run, state=DeliveryState.PENDING).update(
        state=DeliveryState.SKIPPED, failure_reason="No file was produced.",
    )
    _notify(run, failed=True)
    return run


# Finalise a cancelled run.
def _finish_cancelled(run):
    """No partial file is ever kept — nothing was stored before this point."""
    run.status = RunStatus.CANCELLED
    run.phase = RunPhase.DONE
    run.ended_at = timezone.now()
    run.save(update_fields=["status", "phase", "ended_at", "updated_at"])
    return run


# Tell the owner what happened.
def _notify(run, *, failed=False, omissions=False):
    """Notify the owner on completion, failure and omissions.

    Which channels actually fire is the event type's business, not this function's —
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
        else "Some columns were left out — open the run to see which." if omissions
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
            # Lets the bell deep-link to THIS run rather than the Files list —
            # a failure notice is only useful next to the thing that failed.
            metadata={"export_run_id": run.pk},
        )
    except Exception:                              # pragma: no cover - best effort
        # A notification failure must never turn a produced file into a failed run.
        logger.warning("Export notification failed for run %s", run.pk, exc_info=True)


# --------------------------------------------------------------------------- #
# Deliveries                                                                  #
# --------------------------------------------------------------------------- #
# Open one pending delivery per recipient at trigger time.
def _open_deliveries(run):
    """Create the PENDING delivery rows this run will try to send.

    Written at trigger time, not on completion, so a run that fails still shows *who
    was going to receive it* and that they did not — a run can succeed while a
    delivery fails, and the UI can only say which if the rows exist either way.

    Recipients are the people the export is shared with. v1 delivers to signed-in
    platform users only: an external address cannot be re-authorised at download time,
    and the whole point of a link over an attachment is that it is re-checked.
    """
    definition = run.definition
    if definition is None or not definition.email_recipients:
        return
    ExportDelivery.objects.bulk_create([
        ExportDelivery(
            run=run,
            destination=Destination.EMAIL_LINK,
            recipient_user=share.user,
            recipient_email=share.user.email,
            token=secrets.token_urlsafe(32),
        )
        for share in definition.shares.select_related("user")
    ])


# Send the secure links for a completed run.
def _dispatch_deliveries(run, file):
    """Send each pending delivery, or skip it when there is nothing worth sending.

    Recipients receive a link to the authenticated download endpoint — never the file
    itself — so access is judged against the recipient at the moment they click, not at
    the moment we send. One failed send never fails the run or the other recipients.
    """
    pending = list(
        ExportDelivery.objects.filter(run=run, state=DeliveryState.PENDING)
        .select_related("recipient_user")
    )
    if not pending:
        return
    if not file.row_count:
        # An empty file is worse than no email: the recipient opens it, finds nothing,
        # and has no way to tell "no matches" from "something broke".
        ExportDelivery.objects.filter(run=run, state=DeliveryState.PENDING).update(
            state=DeliveryState.SKIPPED,
            failure_reason="No rows matched, so there was nothing worth sending.",
        )
        return

    from vs_notifications.notify import send_notification

    link = download_url(file)
    for delivery in pending:
        delivery.attempted_at = timezone.now()
        try:
            send_notification(
                event_key="export.file_shared",
                context={
                    "label": f"Export: {file.name}",
                    "export_name": run.frozen_config.get("name") or run.reference,
                    "file_name": file.name,
                    "rows": file.row_count,
                    "available_until": file.available_until.strftime("%d %b %Y"),
                    "link": link,
                    "reference": run.reference,
                },
                recipients=[delivery.recipient_user] if delivery.recipient_user_id else [],
                tenant=run.tenant,
            )
            delivery.state = DeliveryState.SENT
        except Exception as exc:                   # pragma: no cover - best effort
            # The file exists and the run succeeded; only this delivery did not.
            delivery.state = DeliveryState.FAILED
            delivery.failure_reason = str(exc)[:300]
            logger.warning("Export delivery %s failed", delivery.pk, exc_info=True)
        delivery.save(update_fields=[
            "state", "attempted_at", "failure_reason", "updated_at",
        ])
        if delivery.state == DeliveryState.SENT:
            audit.record(
                AuditAction.LINK_SENT, actor=run.requested_by, tenant=run.tenant,
                obj=delivery,
                label=delivery.recipient_email or str(delivery.recipient_user_id),
                metadata={"run": run.reference, "file": file.name},
            )


# Build the link a recipient follows to reach the file.
def download_url(file) -> str:
    """Absolute URL of the authenticated download endpoint for ``file``.

    Deliberately not a signed or public URL: it resolves to the same endpoint the
    Export Centre uses, so the recipient must sign in and still hold access to the
    run's entity and dataset. A leaked link is worth nothing on its own.
    """
    base = str(getattr(settings, "FRONTEND_BASE_URL", "")).rstrip("/")
    return f"{base}/exports/files/{file.pk}"


def revoke_delivery(delivery, actor):
    """Kill a secure link. The file itself is untouched; only this route closes."""
    delivery.state = DeliveryState.REVOKED
    delivery.save(update_fields=["state", "updated_at"])
    audit.record(
        AuditAction.LINK_REVOKED, actor=actor, tenant=delivery.run.tenant, obj=delivery,
        label=delivery.recipient_email or str(delivery.recipient_user_id),
        severity="WARNING",
    )
    return delivery


# --------------------------------------------------------------------------- #
# Downloads                                                                   #
# --------------------------------------------------------------------------- #
def authorise_download(file, user, tenant):
    """Decide whether ``user`` may take ``file`` now. Returns ``(ok, refusal_reason)``.

    Checked against the run's *frozen* entity and dataset — not the definition's
    current ones — because the file contains what it contains, and access to it must be
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
    trail as an allowed one — which is exactly the question a compliance review asks.
    """
    record = ExportDownload.objects.create(
        file=file, user=user, outcome=outcome, refusal_reason=reason or "", ip_address=ip or "",
    )
    if outcome == DownloadOutcome.ALLOWED:
        # F() so two people downloading at once cannot lose a count.
        ExportFile.objects.filter(pk=file.pk).update(download_count=F("download_count") + 1)
        # How old files are when people fetch them tells us whether 30 days is the
        # right retention window — the one product question the run rows cannot answer.
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
        try:
            default_storage.delete(file.storage_name)
        except Exception:                          # pragma: no cover - storage best effort
            pass
        file.purged_at = now
        file.save(update_fields=["purged_at", "updated_at"])
        audit.record(
            AuditAction.FILE_EXPIRED, tenant=file.run.tenant, obj=file, label=file.name,
            metadata={"run": file.run.reference},
        )
        purged += 1
    return purged


# --------------------------------------------------------------------------- #
# Capabilities                                                                #
# --------------------------------------------------------------------------- #
def capabilities(user, tenant):
    """What this user may do, as flags — so the UI can disable with a reason.

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
