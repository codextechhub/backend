"""
TrackedTask - the platform-wide Celery base class (wired via
``Celery(task_cls="core.tasks_base:TrackedTask")``), so EVERY task is
automatically tracked in :class:`core.models.BackgroundJob`.

Attribution: callers attach the owner by passing reserved kwargs to
``.delay()`` / ``.apply_async()`` - they are stripped before the task runs,
so task signatures stay untouched::

    execute_import_batch_task.delay(
        import_batch_id=str(batch.id),
        _job_owner_id=str(request.user.id),
        _job_tenant_id=request.user.tenant_id,
        _job_label=f"Import: {batch.file_name}",
        _job_kind="import",
    )

``_job_owner_id`` is the ACTOR who triggered the work - never the subject the
work is *about*. An invitation email to Jane, queued by admin Ada, is owned by
Ada: she triggered it and she sees its timing and outcome in View Queues. If a
task opts into a completion alert, that alert also goes to Ada. Passing the
subject there hands a stranger someone else's queue row and any enabled result
alert. Omit ``_job_tenant_id`` unless it must differ from the owner's tenant;
it is derived from the owner otherwise.

Tasks queued without these kwargs (beat schedules, internal fan-out) are
recorded as system rows (owner=None) when they start.

Every attributed task remains visible in View Queues. Completion notifications
are reserved for user-facing result jobs such as imports. Routine delivery and
system plumbing stay silent by default. Pass ``_job_notify=True`` only when a
new task produces a result the owner is waiting to use, or ``False`` when a
domain-specific notification already reports the outcome.

Tracking is best-effort by design: any database problem while writing the
job row is logged and swallowed so the underlying task is never blocked.
"""
from __future__ import annotations

import logging
import uuid

from celery import Task

logger = logging.getLogger(__name__)

_JOB_KWARGS = (
    "_job_owner_id", "_job_tenant_id", "_job_label",
    "_job_kind", "_job_notify",
)

# These categories produce a user-visible result that is not already announced
# by its owning module. Export jobs deliberately opt out at their call site
# because export.run_completed and export.run_failed carry the file-specific
# result and action link.
_DEFAULT_NOTIFY_KINDS = frozenset({"import"})


def _resolve_job_tenant_id(meta=None):
    meta = meta or {}
    if meta.get("_job_tenant_id"):
        return meta["_job_tenant_id"]
    if meta.get("_job_owner_id"):
        from vs_user.models import User
        return User.objects.only("tenant_id").get(pk=meta["_job_owner_id"]).tenant_id
    from vs_tenants.models import Tenant
    return Tenant.objects.only("id").get(slug="codex").pk


def _short_kind(task_name: str) -> str:
    if "import" in task_name:
        return "import"
    if "email" in task_name or "notification" in task_name:
        return "email"
    return "system"


def _should_notify_owner(kind: str, explicit_notify) -> bool:
    """Resolve bell importance without making every Celery detail user-facing."""
    if explicit_notify is not None:
        return bool(explicit_notify)
    return kind in _DEFAULT_NOTIFY_KINDS


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

            kind = meta["_job_kind"] or _short_kind(self.name or "")

            BackgroundJob.objects.get_or_create(
                celery_task_id=task_id,
                defaults=dict(
                    owner_id=meta["_job_owner_id"] or None,
                    tenant_id=_resolve_job_tenant_id(meta),
                    label=meta["_job_label"] or "",
                    kind=kind,
                    task_name=self.name or "",
                    status=BackgroundJob.Status.QUEUED,
                    notify_owner=_should_notify_owner(kind, meta["_job_notify"]),
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
                    tenant_id=_resolve_job_tenant_id(),
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
        if not job.owner_id or not job.label or not job.notify_owner:
            return
        try:
            from vs_notifications.notify import send_notification

            key = "task.completed" if succeeded else "task.failed"
            send_notification(
                event_key=key,
                context={
                    "label": job.label,
                    "error": "" if succeeded else job.error[:300],
                },
                recipients=[job.owner],
                tenant=job.tenant,
            )
        except Exception:  # pragma: no cover
            # Best-effort: any failure (including UnknownEventTypeError when the
            # event registry is unseeded) is swallowed so it never fails the job.
            logger.warning("BackgroundJob notification failed for job %s", job.pk, exc_info=True)
