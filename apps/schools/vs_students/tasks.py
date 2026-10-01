"""The module's periodic jobs, as Celery beat runs them.

Thin, exactly like ``vs_onboarding.tasks``: the decisions live in the
services, so the same behaviour is reachable from a test and from the worker.
Scheduling is beat, in ``apps/apps/celery.py``. Every job here is idempotent,
so an environment with no beat scheduler simply never runs them and a missed
window costs a late return rather than a wrong one.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger("vs_students")


@shared_task(name="vs_students.return_ended_suspensions")
def return_ended_suspensions_task():
    """Daily: put back every pupil whose suspension has reached its end date.

    Each return is dated the day the suspension was set to end, so a worker
    that was down for three days records the pupil as back on the day they
    were due rather than on the day the job caught up.
    """
    from .services.suspension_return import return_suspended_students

    result = return_suspended_students()
    if result["skipped"] or result["failed"]:
        logger.warning(
            "return_ended_suspensions: returned %s pupil(s), skipped %s, "
            "failed %s.", result["returned"], result["skipped"],
            result["failed"],
        )
    return result
