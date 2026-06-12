"""
TrackedTask — the platform-wide Celery base class (wired via
``Celery(task_cls="core.tasks_base:TrackedTask")``), so EVERY task is
automatically tracked in :class:`core.models.BackgroundJob`.

Attribution: callers attach the owner by passing reserved kwargs to
``.delay()`` / ``.apply_async()`` — they are stripped before the task runs,
so task signatures stay untouched::

    execute_import_batch_task.delay(
        import_batch_id=str(batch.id),
        _job_owner_id=str(request.user.id),
        _job_school_id=request.user.school_id,
        _job_label=f"Import: {batch.file_name}",
        _job_kind="import",
    )

Tasks queued without these kwargs (beat schedules, internal fan-out) are
recorded as system rows (owner=None) when they start.

On completion the owner gets an in-app notification (best-effort — a
notification failure never fails the task).

Tracking is best-effort by design: any database problem while writing the
job row is logged and swallowed so the underlying task is never blocked.
"""
from __future__ import annotations

import logging
import uuid

from celery import Task

logger = logging.getLogger(__name__)

_JOB_KWARGS = ("_job_owner_id", "_job_school_id", "_job_label", "_job_kind")


def _short_kind(task_name: str) -> str:
    if "import" in task_name:
        return "import"
    if "email" in task_name or "notification" in task_name:
        return "email"
    return "system"


class TrackedTask(Task):

    # ------------------------------------------------------------------ #
    # Queue time                                                         #
    # ------------------------------------------------------------------ #
    def apply_async(self, args=None, kwargs=None, task_id=None, **options):
        kwargs = dict(kwargs or {})
        meta = {key: kwargs.pop(key, None) for key in _JOB_KWARGS}
        task_id = task_id or str(uuid.uuid4())

        if meta["_job_owner_id"] or meta["_job_label"]:
            self._record_queued(task_id, meta)

        return super().apply_async(args=args, kwargs=kwargs, task_id=task_id, **options)

    def _record_queued(self, task_id, meta):
        try:
            from core.models import BackgroundJob

            BackgroundJob.objects.get_or_create(
                celery_task_id=task_id,
                defaults=dict(
                    owner_id=meta["_job_owner_id"] or None,
                    school_id=meta["_job_school_id"] or None,
                    label=meta["_job_label"] or "",
                    kind=meta["_job_kind"] or _short_kind(self.name or ""),
                    task_name=self.name or "",
                    status=BackgroundJob.Status.QUEUED,
                ),
            )
        except Exception:  # pragma: no cover - tracking must never block queuing
            logger.warning("BackgroundJob queue-record failed for %s", task_id, exc_info=True)

    # ------------------------------------------------------------------ #
    # Run time                                                           #
    # ------------------------------------------------------------------ #
    def before_start(self, task_id, args, kwargs):
        try:
            from django.utils import timezone

            from core.models import BackgroundJob

            job, _ = BackgroundJob.objects.get_or_create(
                celery_task_id=task_id,
                defaults=dict(
                    task_name=self.name or "",
                    kind=_short_kind(self.name or ""),
                ),
            )
            job.status = BackgroundJob.Status.RUNNING
            job.started_at = timezone.now()
            job.worker = str(getattr(self.request, "hostname", "") or "")
            job.save(update_fields=["status", "started_at", "worker"])
        except Exception:  # pragma: no cover
            logger.warning("BackgroundJob start-record failed for %s", task_id, exc_info=True)
        super().before_start(task_id, args, kwargs)

    def __call__(self, *args, **kwargs):
        try:
            return super().__call__(*args, **kwargs)
        except Exception as exc:
            # Eager mode with propagation re-raises BEFORE on_failure fires,
            # so record the failure here. _finish is terminal-state guarded,
            # so the worker path (where on_failure also runs) won't double-write.
            request = self.request
            if request is not None and getattr(request, "is_eager", False):
                import traceback as tb
                self._finish(
                    request.id, succeeded=False,
                    error=str(exc), traceback_text=tb.format_exc(),
                )
            raise

    def on_success(self, retval, task_id, args, kwargs):
        self._finish(task_id, succeeded=True, retval=retval)
        super().on_success(retval, task_id, args, kwargs)

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        self._finish(
            task_id, succeeded=False,
            error=str(exc), traceback_text=str(einfo) if einfo else "",
        )
        super().on_failure(exc, task_id, args, kwargs, einfo)

    def _finish(self, task_id, *, succeeded, retval=None, error="", traceback_text=""):
        try:
            from django.utils import timezone

            from core.models import BackgroundJob

            job = BackgroundJob.objects.filter(celery_task_id=task_id).first()
            if job is None:
                return
            terminal = (BackgroundJob.Status.SUCCEEDED, BackgroundJob.Status.FAILED)
            if job.status in terminal:
                return
            job.status = (
                BackgroundJob.Status.SUCCEEDED if succeeded else BackgroundJob.Status.FAILED
            )
            job.finished_at = timezone.now()
            if succeeded:
                job.progress = 100
                if isinstance(retval, (dict, list, str, int, float, bool)):
                    job.result = retval
            else:
                job.error = error[:2000]
                job.traceback = traceback_text[:10000]
            job.save(update_fields=[
                "status", "finished_at", "progress", "result", "error", "traceback",
            ])
            self._notify_owner(job, succeeded)
        except Exception:  # pragma: no cover
            logger.warning("BackgroundJob finish-record failed for %s", task_id, exc_info=True)

    # ------------------------------------------------------------------ #
    # Completion notification (in-app, best-effort)                      #
    # ------------------------------------------------------------------ #
    def _notify_owner(self, job, succeeded):
        if not job.owner_id or not job.label:
            return
        try:
            from vs_notifications.constants import ChannelChoices, NotificationStatus
            from vs_notifications.models import Notification, NotificationEventType

            key = "task.completed" if succeeded else "task.failed"
            event, _ = NotificationEventType.objects.get_or_create(
                key=key,
                defaults=dict(
                    label="Background task completed" if succeeded else "Background task failed",
                    source_module="core",
                ),
            )
            outcome = "finished successfully" if succeeded else "FAILED"
            Notification.objects.create(
                school=job.school,
                recipient_id=job.owner_id,
                event_type=event,
                channel=ChannelChoices.IN_APP,
                subject=f"{job.label} — {outcome}",
                body=(
                    f"Your background task '{job.label}' {outcome}."
                    + ("" if succeeded else f" Error: {job.error[:300]}")
                ),
                status=NotificationStatus.SENT,
            )
        except Exception:  # pragma: no cover
            logger.warning("BackgroundJob notification failed for job %s", job.pk, exc_info=True)
