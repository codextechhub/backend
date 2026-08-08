"""Tests for the Health module (vs_health).

Covers the analytics math (histogram→percentile, golden signals), the alert
evaluation fire/resolve lifecycle with auto-incidents, the daily rollup, the
collector flush, and RBAC gating on the API.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from vs_health import collectors, services
from vs_health.constants import HISTOGRAM_SIZE, LATENCY_BUCKETS_MS, MIN_P95_SAMPLE, HealthStatus
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
        # feeds it during the whole suite - drain it so this test sees only
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
        # 50% error rate over the recent window - well past a 5% threshold.
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


class SmallSampleGuardTests(TestCase):
    """A handful of requests must never drive a status or open an incident.

    Production traffic is ~1.7 req/min, so a 15-minute window holds a couple of
    dozen requests and one slow report used to flip p95 past the SLO.
    """

    def _metric(self, *, requests, latencies, route="/v1/finance/invoices/", errors=0):
        # One bucket back, so the row is unambiguously inside every window under
        # test (a bucket stamped exactly "now" can land on the exclusive end).
        bucket = timezone.now().replace(second=0, microsecond=0) - timedelta(minutes=1)
        return RequestMetric.objects.create(
            bucket_start=bucket, route=route, method="GET", tenant_id=None,
            request_count=requests, status_2xx=requests - errors, status_5xx=errors,
            latency_sum_ms=sum(latencies), latency_max_ms=max(latencies),
            latency_hist=_hist_from(latencies),
        )

    def test_below_floor_window_leaves_module_status_unknown(self):
        from vs_health.tasks import refresh_module_service_statuses

        # Starts CRITICAL (the state one slow request used to pin it in) so the
        # assertion proves the guard actively demotes to UNKNOWN rather than
        # matching the model's UNKNOWN default.
        svc = MonitoredService.objects.create(
            key="billing", name="Billing & Fees", sort_order=1,
            current_status=HealthStatus.CRITICAL,
        )
        # 9 fast requests + 1 very slow one: p95 is 5000ms, but n=10 < 30.
        self._metric(requests=10, latencies=[80] * 9 + [5000])

        refresh_module_service_statuses()

        svc.refresh_from_db()
        self.assertEqual(svc.current_status, HealthStatus.UNKNOWN)

    def test_zero_traffic_window_leaves_module_status_unknown(self):
        """No traffic is no signal - a previously green module must not stay green."""
        from vs_health.tasks import refresh_module_service_statuses

        svc = MonitoredService.objects.create(
            key="billing", name="Billing & Fees", sort_order=1,
            current_status=HealthStatus.HEALTHY,
        )

        refresh_module_service_statuses()

        svc.refresh_from_db()
        self.assertEqual(svc.current_status, HealthStatus.UNKNOWN)

    def test_above_floor_slow_window_still_reports_critical(self):
        from vs_health.tasks import refresh_module_service_statuses

        svc = MonitoredService.objects.create(key="billing", name="Billing & Fees", sort_order=1)
        self._metric(requests=60, latencies=[3000] * 60)

        refresh_module_service_statuses()

        svc.refresh_from_db()
        self.assertEqual(svc.current_status, HealthStatus.CRITICAL)

    def test_above_floor_normal_window_is_healthy_at_retuned_thresholds(self):
        """500ms p95 was WARNING under the old 400ms band; it is normal here."""
        from vs_health.tasks import refresh_module_service_statuses

        svc = MonitoredService.objects.create(key="billing", name="Billing & Fees", sort_order=1)
        self._metric(requests=60, latencies=[450] * 60)

        refresh_module_service_statuses()

        svc.refresh_from_db()
        self.assertEqual(svc.current_status, HealthStatus.HEALTHY)

    def test_below_floor_window_does_not_breach_p95_rule(self):
        from vs_health.tasks import evaluate_alert_rules_task

        rule = AlertRule.objects.create(
            name="p95 latency SLO", metric=AlertRule.Metric.P95_LATENCY,
            comparator=AlertRule.Comparator.GT, threshold=800, duration_sec=600,
            severity=Severity.SEV2,
        )
        self._metric(requests=10, latencies=[80] * 9 + [5000])

        result = evaluate_alert_rules_task()

        self.assertEqual(result["fired"], 0)
        self.assertFalse(Alert.objects.filter(rule=rule).exists())
        self.assertFalse(Incident.objects.exists())

    def test_below_floor_window_does_not_breach_error_rate_rule(self):
        """One 500 out of five requests is 20% - noise, not a 5% SLO breach."""
        from vs_health.tasks import evaluate_alert_rules_task

        AlertRule.objects.create(
            name="API error rate", metric=AlertRule.Metric.ERROR_RATE,
            comparator=AlertRule.Comparator.GT, threshold=5, duration_sec=300,
            severity=Severity.SEV1,
        )
        self._metric(requests=5, latencies=[80] * 5, errors=1)

        self.assertEqual(evaluate_alert_rules_task()["fired"], 0)

    def test_above_floor_slow_window_breaches_at_new_threshold(self):
        from vs_health.tasks import evaluate_alert_rules_task

        rule = AlertRule.objects.create(
            name="p95 latency SLO", metric=AlertRule.Metric.P95_LATENCY,
            comparator=AlertRule.Comparator.GT, threshold=800, duration_sec=600,
            severity=Severity.SEV2,
        )
        self._metric(requests=60, latencies=[2000] * 60)

        result = evaluate_alert_rules_task()

        self.assertEqual(result["fired"], 1)
        alert = Alert.objects.get(rule=rule)
        self.assertEqual(alert.status, Alert.Status.FIRING)
        self.assertGreater(alert.value, 800)

    def test_windowed_p95_of_440ms_no_longer_breaches(self):
        """The exact incident that kept reopening: 440ms p95 on ample traffic."""
        from vs_health.tasks import evaluate_alert_rules_task

        AlertRule.objects.create(
            name="p95 latency SLO", metric=AlertRule.Metric.P95_LATENCY,
            comparator=AlertRule.Comparator.GT, threshold=800, duration_sec=600,
            severity=Severity.SEV2,
        )
        self._metric(requests=60, latencies=[420] * 60)

        self.assertEqual(evaluate_alert_rules_task()["fired"], 0)

    def test_open_alert_resolves_once_window_falls_below_floor(self):
        """Traffic drying up must not pin an auto-incident open forever."""
        from vs_health.tasks import evaluate_alert_rules_task

        self._metric(requests=60, latencies=[2000] * 60)
        AlertRule.objects.create(
            name="p95 latency SLO", metric=AlertRule.Metric.P95_LATENCY,
            comparator=AlertRule.Comparator.GT, threshold=800, duration_sec=600,
            severity=Severity.SEV2,
        )
        evaluate_alert_rules_task()
        RequestMetric.objects.all().delete()

        self.assertEqual(evaluate_alert_rules_task()["resolved"], 1)
        incident = Incident.objects.get()
        self.assertEqual(incident.status, Incident.Status.RESOLVED)

    def test_endpoint_status_withheld_below_floor(self):
        self._metric(requests=10, latencies=[80] * 9 + [5000])
        tr = services.parse_range("1h")

        rows = services.endpoint_stats(tr)

        self.assertEqual(rows[0]["status"], HealthStatus.UNKNOWN)
        # The percentile itself stays visible - the number is informational.
        self.assertGreater(rows[0]["p95"], 0)

    def test_window_status_boundary_is_the_shared_floor(self):
        self.assertEqual(
            services.window_status(MIN_P95_SAMPLE - 1, 0.0, 5000), HealthStatus.UNKNOWN)
        self.assertEqual(
            services.window_status(MIN_P95_SAMPLE, 0.0, 5000), HealthStatus.CRITICAL)
        self.assertEqual(services.window_status(0, 0.0, 0.0), HealthStatus.UNKNOWN)

    def test_latency_status_bands_use_retuned_thresholds(self):
        self.assertEqual(services._status_for_latency(440), HealthStatus.HEALTHY)
        self.assertEqual(services._status_for_latency(799), HealthStatus.HEALTHY)
        self.assertEqual(services._status_for_latency(800), HealthStatus.WARNING)
        self.assertEqual(services._status_for_latency(1499), HealthStatus.WARNING)
        self.assertEqual(services._status_for_latency(1500), HealthStatus.CRITICAL)


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


class HealthSeedTests(TestCase):
    @patch("vs_health.seed.SSL_DOMAIN", "api.codexng.com")
    def test_seed_repairs_stale_ssl_monitor_target(self):
        from vs_health.seed import seed_checks

        svc = MonitoredService.objects.create(
            key="dns", name="DNS / SSL", kind="external", sort_order=1
        )
        check = UptimeCheck.objects.create(
            service=svc,
            name="SSL certificate",
            check_type=CheckType.SSL,
            target="api.codexvision.io",
        )

        seed_checks()

        check.refresh_from_db()
        self.assertEqual(check.target, "api.codexng.com")
        self.assertEqual(check.expected["critical_days"], 5)
        self.assertEqual(check.interval_sec, 3600)

    def test_reseed_repairs_stale_alert_rule_threshold(self):
        """The deployed rule was created at 400ms; re-seeding must retune it."""
        from vs_health.seed import seed_alert_rules

        rule = AlertRule.objects.create(
            name="p95 latency SLO", metric=AlertRule.Metric.P95_LATENCY,
            comparator=AlertRule.Comparator.GT, threshold=400, duration_sec=600,
            severity=Severity.SEV2,
        )

        seed_alert_rules()

        rule.refresh_from_db()
        self.assertEqual(rule.threshold, 800)
        # No duplicate rule was created alongside the repaired one.
        self.assertEqual(AlertRule.objects.filter(name="p95 latency SLO").count(), 1)

    def test_reseed_preserves_operator_disabled_rules(self):
        from vs_health.seed import seed_alert_rules

        rule = AlertRule.objects.create(
            name="p95 latency SLO", metric=AlertRule.Metric.P95_LATENCY,
            comparator=AlertRule.Comparator.GT, threshold=400, duration_sec=600,
            severity=Severity.SEV2, is_enabled=False,
        )

        seed_alert_rules()

        rule.refresh_from_db()
        self.assertFalse(rule.is_enabled)
        self.assertEqual(rule.threshold, 800)

    def test_seed_creates_default_rules_at_tuned_thresholds(self):
        from vs_health.seed import seed_alert_rules

        seed_alert_rules()

        self.assertEqual(AlertRule.objects.get(name="p95 latency SLO").threshold, 800)
        self.assertEqual(AlertRule.objects.get(name="API error rate").threshold, 5)


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
