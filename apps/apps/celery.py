import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apps.settings.local")

# task_cls makes TrackedTask the base of EVERY task (including @shared_task),
# so each run is recorded in core.BackgroundJob - the user-facing queue.
app = Celery("apps", task_cls="core.tasks_base:TrackedTask")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Periodic housekeeping. Runs only where a beat scheduler exists -
# the worker service starts with ``celery -A apps worker -B``. Environments
# in eager mode (local dev, staging until the worker is live) simply never
# execute these; the tasks are all idempotent, so a missed window is safe.
app.conf.beat_schedule = {
    "dispatch-rfq-reminders": {
        "task": "vs_procurement.dispatch_rfq_reminders",
        "schedule": 60 * 60,
    },
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

    # --- vs_payments (unbooked gateway money) -----------------------------
    # A booking that fails is already recorded and shown on Needs Attention, but a
    # screen is somewhere you look, not something that tells you. These two do the
    # telling. Both are stateless: the digest reports whatever is outstanding now,
    # and the surge counts only its own window, so a missed run costs a late notice
    # and nothing else.
    "payments-unbooked-digest": {
        "task": "vs_payments.alert_unbooked_receipts",
        "schedule": crontab(hour=7, minute=0),
    },
    # Hourly, matching the window the alarm looks back over.
    "payments-unbooked-surge": {
        "task": "vs_payments.alert_unbooked_surge",
        "schedule": crontab(minute=5),
    },

    # --- vs_exports (Export Centre) --------------------------------------
    # Nightly: hard-delete storage for produced files past their 30-day
    # availability and mark them purged. Availability itself is derived at read
    # time, so a missed night only delays reclaiming bytes - it never changes
    # what a user sees. Idempotent.
    # Every five minutes: start any schedule whose moment has come. The window is
    # the worst-case lateness a scheduled export can suffer, so it is deliberately
    # tighter than the nightly housekeeping below.
    "exports-dispatch-schedules": {
        "task": "vs_exports.dispatch_schedules",
        "schedule": crontab(minute="*/5"),
    },
    # Every half hour: close runs whose worker died mid-flight. Nothing inside the
    # process can strand a run any more, but a killed worker leaves one RUNNING for
    # good - spinning on the Files screen, and counting against the tenant's
    # three-in-flight limit until nobody can export at all. Frequent because it is one
    # indexed query, and because the run it closes is one somebody is waiting on.
    "exports-sweep-abandoned-runs": {
        "task": "vs_exports.sweep_abandoned_runs",
        "schedule": crontab(minute="*/30"),
    },
    "exports-expire-files": {
        "task": "vs_exports.expire_files",
        "schedule": crontab(hour=3, minute=30),
    },
    # Nightly: prune product analytics past its retention window. Deliberately not
    # applied to the audit trail, which is kept indefinitely.
    "exports-prune-analytics": {
        "task": "vs_exports.prune_analytics",
        "schedule": crontab(hour=3, minute=45),
    },
    "guides-prune-analytics": {
        "task": "vs_tickets.prune_guide_analytics",
        "schedule": crontab(hour=3, minute=50),
    },

    # --- vs_onboarding (abandoned onboarding, go-live history) ------------
    # Daily: suspend schools that have been PENDING for 90 days, then warn the
    # ones that have reached 76 (expiry first, so nobody is warned about a
    # deadline they have already passed). Measured from Tenant.pending_since,
    # so a reinstated school gets its window and its warning back rather than
    # being expired again the next morning. Idempotent in both steps.
    "onboarding-expire-stale": {
        "task": "vs_onboarding.expire_stale_onboarding",
        "schedule": crontab(hour=4, minute=0),
    },
    # Every two weeks: the stale-onboarding list for platform operators. Beat
    # has no fortnightly primitive, so this is the 1st and the 15th, which is
    # the same cadence to within a day and never drifts.
    "onboarding-report-stale": {
        "task": "vs_onboarding.report_stale_onboarding",
        "schedule": crontab(hour=7, minute=30, day_of_month="1,15"),
    },
    # Weekly: go-live request history past a year. The retention window is a
    # year, but the cutoff rolls daily, so running it weekly keeps the tail
    # short instead of letting rows sit up to a year past their date.
    "onboarding-purge-go-live-history": {
        "task": "vs_onboarding.purge_go_live_history",
        "schedule": crontab(hour=4, minute=30, day_of_week=0),
    },

    # --- vs_health (platform health) -------------------------------------
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
