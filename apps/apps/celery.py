import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apps.settings.local")

# task_cls makes TrackedTask the base of EVERY task (including @shared_task),
# so each run is recorded in core.BackgroundJob — the user-facing queue.
app = Celery("apps", task_cls="core.tasks_base:TrackedTask")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Periodic housekeeping. Runs only where a beat scheduler exists —
# the worker service starts with ``celery -A apps worker -B``. Environments
# in eager mode (local dev, staging until the worker is live) simply never
# execute these; the tasks are all idempotent, so a missed window is safe.
app.conf.beat_schedule = {
    "dispatch-pending-import-notifications": {
        "task": "vs_import_data.tasks.dispatch_pending_import_notifications_task",
        "schedule": crontab(minute="*/5"),
    },
    "retry-failed-import-notifications": {
        "task": "vs_import_data.tasks.retry_failed_import_notifications_task",
        "schedule": crontab(minute="*/15"),
    },
    "mark-stuck-import-jobs": {
        "task": "vs_import_data.tasks.mark_stuck_import_jobs_task",
        "schedule": crontab(minute="*/30"),
    },
    "cleanup-old-import-batches": {
        "task": "vs_import_data.tasks.cleanup_old_import_batches_task",
        "schedule": crontab(hour=2, minute=0),
    },
    "prune-background-jobs": {
        "task": "core.tasks.prune_background_jobs_task",
        "schedule": crontab(hour=2, minute=30),
    },
}
