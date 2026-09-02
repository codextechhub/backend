# =============================================================================
# core / checks.py
#
# One Django system check, registered from CoreConfig.ready().
#
# WHY IT EXISTS
# -------------
# ``CELERY_TASK_ALWAYS_EAGER`` makes ``.delay()`` run its task inline, in the
# calling process. That is the right default for local development and for a
# deployment whose worker is not up yet, and it is deliberately what
# ``apps/settings/staging.py`` falls back to.
#
# What it does NOT do is run ``beat``. Eager mode has no scheduler, so in that
# configuration **no periodic task fires at all** - not once, not late, never.
# ``apps/celery.py`` currently schedules, among others:
#
#   finance-daily-dunning              overdue fee reminders
#   payments-dispatch-undispatched-payouts   the recovery sweep for a payout
#                                      that was approved but never sent
#   payments-unbooked-digest / -surge  the alarms for gateway money that
#                                      failed to book
#   mark-stuck-import-jobs             the thing that notices a dead import
#
# None of that is loud when it does not happen. A school simply is not chased
# for its fees, and nobody is told that nobody was chased. The comment in
# apps/celery.py reasons that the tasks are idempotent "so a missed window is
# safe", which is true of missing *one* window and is not the situation this
# check exists to catch: the scheduler having never run at all.
#
# WHY A WARNING AND NOT AN ERROR
# ------------------------------
# An Error makes every management command exit non-zero, including the
# ``migrate`` that runs during deploy. Shipping that would take a system which
# is currently serving traffic in this state and refuse to deploy it, which is a
# worse failure than the one being reported. It is a Warning so it is visible on
# every command in production without being a gate.
#
# **Once the worker and broker are live, change ``CheckWarning`` to
# ``CheckError`` below.** At that point the condition genuinely is a
# misconfiguration rather than a known transitional state, and a deploy that
# reintroduces it should fail.
#
# WHY IT IS GUARDED ON DEBUG
# --------------------------
# Eager mode is correct in development, so firing there would train everyone to
# ignore the message - which is exactly how this went unnoticed for months in
# the first place. It speaks only where it is a problem.
# =============================================================================
from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning as CheckWarning, register

# Stable id so a deployment that has accepted the gap can silence exactly this
# one via SILENCED_SYSTEM_CHECKS.
CHECK_ID = "core.W001"


@register()
def check_scheduled_tasks_can_run(app_configs=None, **kwargs):
    """Warn when a production deployment can never run a scheduled task.

    Reads settings only - no database, no broker connection - so it is safe on
    every management command, including one run against an unreachable database.
    """
    if settings.DEBUG:
        # Eager mode is the right answer locally; saying so every time would
        # only teach people to scroll past it.
        return []

    if not getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return []

    scheduled = sorted((getattr(settings, "CELERY_BEAT_SCHEDULE", None) or {}))
    if not scheduled:
        # Read the schedule off the app when it is set there rather than in
        # settings, which is where this project puts it.
        try:
            from apps.celery import app as celery_app

            scheduled = sorted(celery_app.conf.beat_schedule or {})
        except Exception:  # pragma: no cover - never let a check crash a command
            scheduled = []

    named = ", ".join(scheduled[:4])
    tail = f" and {len(scheduled) - 4} more" if len(scheduled) > 4 else ""

    return [
        CheckWarning(
            "Celery is in eager mode with DEBUG off, so no scheduled task can run.",
            hint=(
                "CELERY_TASK_ALWAYS_EAGER makes .delay() run inline and provides "
                "no beat scheduler, so every periodic task is inert in this "
                f"deployment ({len(scheduled)} are configured"
                + (f": {named}{tail}" if scheduled else "")
                + "). Overdue-fee dunning, the undispatched-payout sweep and the "
                "unbooked-gateway-money alarms are among them, and none of them "
                "reports its own absence. Start the worker service "
                "(celery -A apps worker -B), set REDIS_URL on both the web "
                "service and the worker, and set CELERY_EAGER=false on the web "
                "service. Silence with SILENCED_SYSTEM_CHECKS = ['core.W001'] "
                "only if this deployment genuinely has no background work."
            ),
            id=CHECK_ID,
        )
    ]
