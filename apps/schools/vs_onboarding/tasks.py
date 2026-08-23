"""The module's three periodic jobs, as Celery beat runs them.

Each is thin, exactly like ``vs_exports.tasks``: the decisions live in the
services, so the same behaviour is reachable from a test, from the management
command an operator runs by hand, and from the worker. The commands are not a
second implementation of anything - all three call the function below them.

Scheduling is beat, in ``apps/apps/celery.py``, because that is what this repo
already does for recurring work (dunning, unbooked-money alerts, export expiry,
health probes). Every job here is idempotent, so an environment with no beat
scheduler simply never runs them and a missed window costs nothing.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger("vs_onboarding")


@shared_task(name="vs_onboarding.expire_stale_onboarding")
def expire_stale_onboarding_task():
    """Daily: expire schools out of time, then warn those close to it.

    One task rather than two, because the order matters: a school past ninety
    days must be suspended, not warned about a deadline it has already missed.
    """
    from .services.lifecycle import run_sweep

    result = run_sweep()
    if result["expired_count"]:
        logger.warning(
            "expire_stale_onboarding: suspended %s school(s).",
            result["expired_count"],
        )
    return {
        "expired": result["expired_count"],
        "warned": result["warned_count"],
        "without_school_profile": result["without_school_profile"],
    }


@shared_task(name="vs_onboarding.report_stale_onboarding")
def report_stale_onboarding_task():
    """Fortnightly: tell platform operators which onboardings are going stale."""
    from .services.lifecycle import stale_onboarding_report

    result = stale_onboarding_report()
    return {
        "ageing": len(result["ageing"]),
        "expired": len(result["expired"]),
        "dispatched": result["dispatched"],
    }


@shared_task(name="vs_onboarding.purge_go_live_history")
def purge_go_live_history_task():
    """Weekly: drop go-live history past a year, keeping each activation.

    The window is a year but the job runs weekly, because the cutoff rolls: a
    yearly run would leave rows up to a year past their retention date.
    """
    from .services.retention import purge_go_live_history

    result = purge_go_live_history()
    return {"removed": result["removed"], "kept": result["kept_activating"]}
