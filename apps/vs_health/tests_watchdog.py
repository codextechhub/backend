"""A stopped worker is noticed by something that is not the worker.

Every health alarm about background work runs as a task on the one worker, so
when it stops they all stop and the console keeps showing HEALTHY. These tests
pin the reader that runs in the web process instead: a stale heartbeat marks
Celery CRITICAL and opens one incident, however often it is checked, and a fresh
heartbeat resolves it.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from vs_health import watchdog
from vs_health.constants import HealthStatus
from vs_health.models import Incident, MonitoredService, QueueSnapshot


class WorkerHeartbeatTests(TestCase):
    def setUp(self):
        self.celery, _ = MonitoredService.objects.update_or_create(
            key="celery", defaults={"name": "Celery workers", "is_active": True,
                                    "current_status": HealthStatus.HEALTHY},
        )
        notify = patch.object(watchdog, "_notify_operators", return_value=1)
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    def _beat(self, *, minutes_ago):
        QueueSnapshot.objects.create(
            queue_name="celery", captured_at=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def _open_incidents(self):
        return Incident.objects.filter(fault_key=watchdog.WORKER_HEARTBEAT_FAULT_KEY).exclude(
            status=Incident.Status.RESOLVED)

    def test_no_heartbeat_ever_raises_nothing(self):
        QueueSnapshot.objects.all().delete()
        result = watchdog.check_worker_heartbeat()
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(self._open_incidents().exists())

    def test_a_fresh_heartbeat_is_healthy(self):
        self._beat(minutes_ago=1)
        result = watchdog.check_worker_heartbeat()
        self.assertEqual(result["status"], "healthy")
        self.assertFalse(self._open_incidents().exists())

    def test_a_stale_heartbeat_marks_celery_critical_and_opens_one_incident(self):
        self._beat(minutes_ago=12)

        first = watchdog.check_worker_heartbeat()
        second = watchdog.check_worker_heartbeat()  # Another web process, or the next pass.

        self.assertEqual(first["status"], "stale")
        self.assertIsNotNone(first["incident"])
        self.assertIsNone(second["incident"])
        self.assertEqual(self._open_incidents().count(), 1)
        self.celery.refresh_from_db()
        self.assertEqual(self.celery.current_status, HealthStatus.CRITICAL)
        self.assertEqual(self.notify.call_count, 1)  # Operators are told once.

    def test_the_incident_resolves_when_the_heartbeat_returns(self):
        self._beat(minutes_ago=12)
        watchdog.check_worker_heartbeat()
        self._beat(minutes_ago=0)

        result = watchdog.check_worker_heartbeat()

        self.assertEqual(result["status"], "healthy")
        self.assertFalse(self._open_incidents().exists())
        self._beat(minutes_ago=12)
        QueueSnapshot.objects.filter(captured_at__gt=timezone.now() - timedelta(minutes=5)).delete()
        self.assertIsNotNone(watchdog.check_worker_heartbeat()["incident"])  # A new stoppage opens anew.

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_an_eager_environment_is_not_checked(self):
        self._beat(minutes_ago=60)
        self.assertIsNone(watchdog.check_worker_heartbeat_throttled())
        self.assertFalse(self._open_incidents().exists())

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_the_web_process_check_runs_at_most_once_a_minute(self):
        self._beat(minutes_ago=60)
        watchdog._last_check = 0.0
        self.addCleanup(setattr, watchdog, "_last_check", 0.0)

        self.assertEqual(watchdog.check_worker_heartbeat_throttled()["status"], "stale")
        self.assertIsNone(watchdog.check_worker_heartbeat_throttled())
