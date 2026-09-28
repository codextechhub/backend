"""Noticing, from outside the worker, that the background worker has stopped.

Every alarm this platform raises about background work runs as a Celery task:
the queue snapshot that marks Celery CRITICAL when no worker answers, the alert
rules, the payments unbooked-money digest, the payment recovery sweep. They all
run on the one worker, with beat embedded in it. When that worker stops, every
one of them stops together, and the last status on screen stays HEALTHY. An
alarm that needs the thing it watches cannot report that thing's death.

The heartbeat is the queue snapshot. ``capture_queue_snapshot_task`` runs on
beat every minute and writes a ``QueueSnapshot`` row each time, so the newest
row's ``captured_at`` is when beat and a worker were last both alive. No second
heartbeat is written: that row already is one.

The reader runs where Celery does not: the web process. The request-metrics
flusher (:mod:`vs_health.collectors`) is a daemon thread in each web process
that wakes every half minute, and it calls :func:`check_worker_heartbeat_throttled`
after each flush. So the check keeps running while the API serves requests, and
the API serves requests whether or not the worker is up (webhooks, parents
paying, the health console itself).

When the newest heartbeat is older than :data:`WORKER_HEARTBEAT_STALE_AFTER`:

* the ``celery`` monitored service is set CRITICAL, which the Command Center's
  posture and service grid read directly;
* one incident is opened through :func:`vs_health.faults.report_configuration_fault`,
  keyed so that every web process and every pass finds the same open incident
  instead of opening another;
* platform operators holding ``platform.health.update`` are told once, when
  the incident opens, through the same ``health.alert_fired`` notification a
  firing alert rule sends. The in-app notice is written straight away; the email
  leg is itself queued work, so it goes out only once a worker is back.

When the heartbeat is fresh again, the open incident is resolved, so the next
stoppage opens a new one. The snapshot task restores the service's status
itself on its first run.

No snapshot at all means no worker has ever run against this database (a
development machine with eager Celery), which is not an outage, so nothing is
raised. Environments that run Celery eagerly are skipped for the same reason.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .constants import HealthStatus

logger = logging.getLogger(__name__)

#: Newest-heartbeat age past which the worker counts as stopped. The heartbeat is
#: written every minute, so five minutes is several missed beats, not one slow one.
WORKER_HEARTBEAT_STALE_AFTER = timedelta(minutes=5)

#: The one open incident a stopped worker is filed under.
WORKER_HEARTBEAT_FAULT_KEY = "celery.worker-heartbeat-stale"

#: Least time between two checks in one web process.
CHECK_INTERVAL_SECONDS = 60

_last_check = 0.0
_check_lock = threading.Lock()


def last_heartbeat():
    """The instant a worker last wrote a queue snapshot, or ``None`` if none ever has."""
    from .models import QueueSnapshot

    return (
        QueueSnapshot.objects.order_by("-captured_at")
        .values_list("captured_at", flat=True)
        .first()
    )


def check_worker_heartbeat(*, now=None) -> dict:
    """Raise, or clear, the stopped-worker alarm from the newest heartbeat's age.

    Returns ``{"status", "last_heartbeat", "age_seconds", "incident"}``, where
    ``status`` is ``unknown`` (no heartbeat ever), ``healthy`` or ``stale``, and
    ``incident`` is the code of an incident opened by this call, else ``None``.
    """
    from .faults import report_configuration_fault
    from .models import MonitoredService, Severity

    now = now or timezone.now()
    beat = last_heartbeat()
    if beat is None:
        return {"status": "unknown", "last_heartbeat": None, "age_seconds": None,
                "incident": None}

    age = now - beat
    result = {"last_heartbeat": beat.isoformat(), "age_seconds": int(age.total_seconds()),
              "incident": None}
    if age <= WORKER_HEARTBEAT_STALE_AFTER:
        _resolve_open_incidents(now)
        return {**result, "status": "healthy"}

    celery_service = MonitoredService.objects.filter(key="celery", is_active=True).first()
    if celery_service is not None:
        celery_service.set_status(HealthStatus.CRITICAL)

    minutes = int(age.total_seconds() // 60)
    incident = report_configuration_fault(
        fault_key=WORKER_HEARTBEAT_FAULT_KEY,
        title="Background worker has stopped",
        summary=(
            f"No worker heartbeat for {minutes} minutes (last at {beat.isoformat()}). "
            "Scheduled and queued work is not running: webhook bookings, the payment "
            "recovery sweep, dunning and every health alarm that runs as a task. "
            "Restart the worker service and check its logs."
        ),
        severity=Severity.SEV1,
        who="Worker heartbeat",
    )
    if incident is not None:
        if celery_service is not None:
            incident.services.add(celery_service)
        _notify_operators(incident, minutes, celery_service)
        result["incident"] = incident.code
    return {**result, "status": "stale"}


def check_worker_heartbeat_throttled() -> dict | None:
    """Run :func:`check_worker_heartbeat` at most once a minute in this process.

    Called by the web process's metrics flusher. Returns ``None`` when skipped:
    too soon since the last check, or Celery runs eagerly here and no worker is
    expected.
    """
    global _last_check
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return None
    with _check_lock:
        if time.monotonic() - _last_check < CHECK_INTERVAL_SECONDS:
            return None
        _last_check = time.monotonic()
    return check_worker_heartbeat()


def _resolve_open_incidents(now) -> None:
    """Resolve the stopped-worker incident once the heartbeat is back."""
    from .models import Incident

    for incident in (
        Incident.objects.filter(fault_key=WORKER_HEARTBEAT_FAULT_KEY)
        .exclude(status=Incident.Status.RESOLVED)
    ):
        incident.status = Incident.Status.RESOLVED
        incident.resolved_at = now
        incident.save(update_fields=["status", "resolved_at", "updated_at"])
        incident.add_event(
            kind="resolved", who="Worker heartbeat",
            text="The worker heartbeat is back.",
        )


def _notify_operators(incident, minutes, celery_service) -> int:
    """Tell platform operators the worker stopped. Never raises.

    Uses the ``health.alert_fired`` event so the message arrives where every other
    platform health alarm does, phrased as an observed value against a limit.
    """
    try:
        from vs_rbac.evaluator import resolve_users_with_permission
        from vs_notifications.notify import send_notification
        from vs_tenants.models import Tenant

        from .constants import PERM_UPDATE

        platform = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        recipients = list(resolve_users_with_permission(
            tenant=platform, branch=None, permission_key=PERM_UPDATE,
        ))
        if not recipients:
            logger.error("Worker heartbeat incident %s has no recipients.", incident.code)
            return 0
        sent = send_notification(
            event_key="health.alert_fired",
            context={
                "incident_code": incident.code,
                "rule_name": "Background worker heartbeat age (minutes)",
                "severity_label": incident.get_severity_display(),
                "service_name": celery_service.name if celery_service else "Background worker",
                "observed_value": minutes,
                "comparator": ">",
                "threshold": int(WORKER_HEARTBEAT_STALE_AFTER.total_seconds() // 60),
                "fired_at": incident.started_at.isoformat(),
            },
            recipients=recipients,
            tenant=platform,
            metadata={"incident_id": str(incident.id), "incident_code": incident.code},
        )
        return len(sent)
    except Exception:  # noqa: BLE001 - the incident must survive a delivery failure
        logger.exception("Worker heartbeat incident %s: notification failed.", incident.code)
        return 0
