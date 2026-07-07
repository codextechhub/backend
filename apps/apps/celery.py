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

    # --- vs_finance (dunning) --------------------------------------------
    # Daily: generate the day's overdue reminders and dispatch every PENDING
    # notice through vs_notifications. Idempotent per (invoice, level) and per
    # run date, so a missed window is safe.
    "finance-daily-dunning": {
        "task": "vs_finance.run_daily_dunning",
        "schedule": crontab(hour=6, minute=0),
    },

    # --- vs_health (VIGIL observability) ---------------------------------
    # Synthetic probes, queue snapshots, and alert evaluation. All idempotent
    # and safe to miss in eager environments.
    "health-run-uptime-checks": {
        "task": "vs_health.tasks.run_uptime_checks_task",
        "schedule": crontab(minute="*/5"),
    },
    "health-capture-queue-snapshot": {
        "task": "vs_health.tasks.capture_queue_snapshot_task",
        "schedule": crontab(minute="*"),
    },
    "health-evaluate-alert-rules": {
        "task": "vs_health.tasks.evaluate_alert_rules_task",
        "schedule": crontab(minute="*"),
    },
    "health-rollup-uptime-daily": {
        "task": "vs_health.tasks.rollup_uptime_daily_task",
        "schedule": crontab(minute=15),  # hourly at :15
    },
    "health-prune-metrics": {
        "task": "vs_health.tasks.prune_health_metrics_task",
        "schedule": crontab(hour=3, minute=0),
    },
}
