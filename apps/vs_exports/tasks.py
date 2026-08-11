"""Background work for the Export Centre.

Four tasks, each thin: the decisions live in :mod:`vs_exports.services`, so the same
behaviour is reachable from a test, a management command or a worker.

Every task rides the platform's :class:`core.tasks_base.TrackedTask` base, so each run
already appears in View Queues with the actor who triggered it.

Retries: only :class:`~vs_exports.constants.FailureCode.INFRASTRUCTURE` outcomes are
worth another attempt, and the service has already recorded the run as failed by the
time we see one - so the task does not use Celery's ``autoretry_for``. Retrying a
permission or filter failure would fail identically and cost the user three
notifications instead of one.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="vs_exports.run_export")
def run_export_task(run_id: int):
    """Produce the file for one :class:`~vs_exports.models.ExportRun`."""
    from .services import execute_run

    run = execute_run(run_id)
    if run is None:
        return {"run": run_id, "status": "MISSING"}
    return {
        "run": run.reference,
        "status": run.status,
        "rows": run.row_count,
        "omissions": len(run.omissions or []),
    }


@shared_task(name="vs_exports.expire_files")
def expire_export_files_task():
    """Nightly: hard-delete storage for files past their 30-day availability.

    Availability itself is evaluated at read time, so a night this job misses changes
    nothing a user sees - it only delays reclaiming the bytes.
    """
    from .services import expire_files

    purged = expire_files()
    return {"purged": purged}


@shared_task(name="vs_exports.prune_analytics")
def prune_analytics_task():
    """Nightly: drop product telemetry past its retention window.

    Analytics is disposable - that is the whole difference between it and the audit
    trail sitting beside it, which is kept indefinitely and never pruned by anything.
    """
    import datetime

    from django.utils import timezone

    from .analytics import RETENTION_DAYS
    from .models import ExportAnalyticsEvent

    cutoff = timezone.now() - datetime.timedelta(days=RETENTION_DAYS)
    deleted, _ = ExportAnalyticsEvent.objects.filter(occurred_at__lt=cutoff).delete()
    return {"deleted": deleted, "older_than_days": RETENTION_DAYS}


@shared_task(name="vs_exports.dispatch_schedules")
def dispatch_due_schedules_task():
    """Every few minutes: start the schedules whose moment has come.

    Cheap when there is nothing to do - one indexed query on ``next_run_at`` - so it
    can run often enough that a 03:00 schedule fires close to 03:00.
    """
    from .services import dispatch_due_schedules

    return dispatch_due_schedules()
