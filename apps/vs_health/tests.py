"""Tests for the Health module (vs_health).

Covers the analytics math (histogram→percentile, golden signals), the alert
evaluation fire/resolve lifecycle with auto-incidents, the daily rollup, the
collector flush, and RBAC gating on the API.
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from vs_health import collectors, services
from vs_health.constants import HISTOGRAM_SIZE, LATENCY_BUCKETS_MS
from vs_health.models import (
    MonitoredService,
    RequestMetric,
    UptimeCheck,
    UptimeCheckResult,
    UptimeDailyRollup,
    AlertRule,
    Alert,
    Incident,
    CheckType,
    Severity,
)


def _hist_from(latencies):
    h = [0] * HISTOGRAM_SIZE
    for lat in latencies:
        idx = next((i for i, u in enumerate(LATENCY_BUCKETS_MS) if lat <= u), HISTOGRAM_SIZE - 1)
        h[idx] += 1
    return h


class PercentileMathTests(TestCase):
    def test_percentile_monotonic_and_bounded(self):
        hist = _hist_from(list(range(1, 1001)))  # 1..1000 ms uniform
        p50 = services.percentile_from_hist(hist, 50)
        p95 = services.percentile_from_hist(hist, 95)
        p99 = services.percentile_from_hist(hist, 99)
        self.assertLess(p50, p95)
        self.assertLessEqual(p95, p99)
        # p50 of a uniform 1..1000 distribution sits near the middle bucket.
        self.assertGreater(p50, 200)
        self.assertLess(p50, 800)

    def test_empty_histogram_is_zero(self):
        self.assertEqual(services.percentile_from_hist([0] * HISTOGRAM_SIZE, 95), 0.0)

    def test_merge_hist_sums_elementwise(self):
        a = _hist_from([10, 10])
        b = _hist_from([10])
        merged = services.merge_hist([a, b])
        self.assertEqual(sum(merged), 3)


class GoldenSignalsTests(TestCase):
    def setUp(self):
        now = timezone.now().replace(second=0, microsecond=0)
        for m in range(10):
            RequestMetric.objects.create(
                bucket_start=now - timedelta(minutes=m),
                route="/v1/i/students/", method="GET", tenant_id=None,
                request_count=100, status_2xx=98, status_5xx=2,
                latency_sum_ms=9000, latency_max_ms=300,
                latency_hist=_hist_from([90] * 100),
            )

    def test_golden_signals_shape_and_values(self):
        tr = services.parse_range("1h")
        kpis = services.golden_signals(tr)
        self.assertIn("latency", kpis)
        self.assertIn("traffic", kpis)
        self.assertIn("errors", kpis)
        self.assertIn("saturation", kpis)
        # error rate ~ 2%
        self.assertAlmostEqual(kpis["errors"]["value"], 2.0, delta=0.5)
        self.assertGreater(kpis["traffic"]["value"], 0)

    def test_request_series_returns_points(self):
        tr = services.parse_range("1h")
        series = services.request_series(tr)
        self.assertTrue(series)
        self.assertIn("p95", series[0])
        self.assertIn("error_rate", series[0])


class CollectorFlushTests(TestCase):
    def setUp(self):
        # The collector buffer is process-global and the request middleware
        # feeds it during the whole suite — drain it so this test sees only
        # its own records.
        collectors._drain()

    def test_record_then_flush_upserts_and_merges(self):
        collectors.record(route="/v1/x/", method="GET", status_code=200, latency_ms=42, throttled=False)
        collectors.record(route="/v1/x/", method="GET", status_code=500, latency_ms=120, throttled=False)
        touched = collectors.flush()
        self.assertEqual(touched, 1)
        row = RequestMetric.objects.get(route="/v1/x/", method="GET")
        self.assertEqual(row.request_count, 2)
        self.assertEqual(row.status_5xx, 1)
        # A second batch into the same bucket must merge, not duplicate.
        collectors.record(route="/v1/x/", method="GET", status_code=200, latency_ms=30)
        collectors.flush()
        row.refresh_from_db()
        self.assertEqual(row.request_count, 3)
        self.assertEqual(RequestMetric.objects.filter(route="/v1/x/").count(), 1)


class AlertEvaluationTests(TestCase):
    def setUp(self):
        self.svc = MonitoredService.objects.create(key="api", name="API · DRF", sort_order=1)
        now = timezone.now().replace(second=0, microsecond=0)
        # 50% error rate over the recent window — well past a 5% threshold.
        RequestMetric.objects.create(
            bucket_start=now, route="/v1/i/", method="GET", tenant_id=None,
            request_count=100, status_2xx=50, status_5xx=50,
            latency_sum_ms=9000, latency_max_ms=300, latency_hist=_hist_from([90] * 100),
        )
        self.rule = AlertRule.objects.create(
            name="API error rate", metric=AlertRule.Metric.ERROR_RATE,
            comparator=AlertRule.Comparator.GT, threshold=5, duration_sec=300,
            severity=Severity.SEV1, target_service=self.svc,
        )

    def test_breach_fires_alert_and_opens_auto_incident(self):
        from vs_health.tasks import evaluate_alert_rules_task
        result = evaluate_alert_rules_task()
        self.assertEqual(result["fired"], 1)
        alert = Alert.objects.get(rule=self.rule)
        self.assertEqual(alert.status, Alert.Status.FIRING)
        self.assertIsNotNone(alert.incident)
        self.assertEqual(alert.incident.source, Incident.Source.AUTO)
        self.assertTrue(alert.incident.timeline.exists())

    def test_recovery_resolves_alert_and_incident(self):
        from vs_health.tasks import evaluate_alert_rules_task
        evaluate_alert_rules_task()
        # Clear the breach: wipe metrics so error rate computes to 0.
        RequestMetric.objects.all().delete()
        result = evaluate_alert_rules_task()
        self.assertEqual(result["resolved"], 1)
        alert = Alert.objects.get(rule=self.rule)
        self.assertEqual(alert.status, Alert.Status.RESOLVED)
        alert.incident.refresh_from_db()
        self.assertEqual(alert.incident.status, Incident.Status.RESOLVED)


class DailyRollupTests(TestCase):
    def test_rollup_computes_uptime_from_results(self):
        from vs_health.tasks import rollup_uptime_daily_task
        svc = MonitoredService.objects.create(key="redis", name="Redis", sort_order=1)
        check = UptimeCheck.objects.create(service=svc, name="ping", check_type=CheckType.REDIS)
        now = timezone.now()
        for i in range(10):
            UptimeCheckResult.objects.create(
                uptime_check=check, service=svc,
                status="critical" if i < 2 else "healthy",
                response_ms=20, checked_at=now,
            )
        rollup_uptime_daily_task()
        roll = UptimeDailyRollup.objects.get(service=svc, day=now.date())
        self.assertEqual(roll.total_checks, 10)
        self.assertEqual(roll.failed_checks, 2)
        self.assertAlmostEqual(float(roll.uptime_pct), 80.0, delta=0.01)


class RBACGatingTests(APITestCase):
    def test_overview_requires_authentication(self):
        resp = self.client.get(reverse("health-overview"))
        self.assertEqual(resp.status_code, 401)

    def test_overview_authenticated_returns_envelope(self):
        from unittest.mock import patch
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create_user(
            email="sre@codexng.com", first_name="S", last_name="RE",
            user_type=User.UserType.CX_STAFF, status=User.Status.ACTIVE,
        )
        self.client.force_authenticate(user=user)
        # Grant the platform.health.view permission for this request.
        with patch("vs_rbac.permissions.has_permission", return_value=True):
            resp = self.client.get(reverse("health-overview"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        for key in ("posture", "kpis", "services", "request_series", "queues"):
            self.assertIn(key, body["data"])
