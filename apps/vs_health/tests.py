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

from core.test_utils import TenantAPIClient
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


def _platform_tenant():
    """The one PLATFORM tenant, seeded by vs_tenants migration 0002.

    Being platform staff IS being on this tenant - there is no persona column
    standing in for it any more - so a fixture that wants a CX account names
    the tenant, exactly as production code does.
    """
    from vs_tenants.models import Tenant

    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


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
            comparator=AlertRule.Comparator.GT, threshold=5, duration_sec=0,
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


class SustainedAlertEvaluationTests(TestCase):
    def setUp(self):
        self.svc = MonitoredService.objects.create(
            key="api", name="API · DRF", sort_order=1,
        )
        bucket = timezone.now().replace(second=0, microsecond=0) - timedelta(minutes=1)
        RequestMetric.objects.create(
            bucket_start=bucket,
            route="/v1/i/students/",
            method="GET",
            tenant_id=None,
            request_count=100,
            status_2xx=90,
            status_5xx=10,
            latency_sum_ms=9000,
            latency_max_ms=300,
            latency_hist=_hist_from([90] * 100),
        )
        self.rule = AlertRule.objects.create(
            name="Sustained API errors",
            metric=AlertRule.Metric.ERROR_RATE,
            comparator=AlertRule.Comparator.GT,
            threshold=5,
            duration_sec=300,
            severity=Severity.SEV1,
            target_service=self.svc,
        )

    def test_rule_fires_only_after_full_sustained_duration(self):
        from vs_health.tasks import evaluate_alert_rules_task

        started = timezone.now()
        with patch("vs_health.tasks.timezone.now", return_value=started):
            self.assertEqual(evaluate_alert_rules_task()["fired"], 0)
        with patch(
            "vs_health.tasks.timezone.now",
            return_value=started + timedelta(seconds=299),
        ):
            self.assertEqual(evaluate_alert_rules_task()["fired"], 0)
        with patch(
            "vs_health.tasks.timezone.now",
            return_value=started + timedelta(seconds=300),
        ):
            self.assertEqual(evaluate_alert_rules_task()["fired"], 1)

        self.rule.refresh_from_db()
        self.assertEqual(self.rule.breach_started_at, started)
        self.assertEqual(Alert.objects.filter(rule=self.rule).count(), 1)

    def test_clearing_before_duration_resets_the_clock(self):
        from vs_health.tasks import evaluate_alert_rules_task

        started = timezone.now()
        with patch("vs_health.tasks.timezone.now", return_value=started):
            evaluate_alert_rules_task()
        RequestMetric.objects.all().delete()
        with patch(
            "vs_health.tasks.timezone.now",
            return_value=started + timedelta(seconds=120),
        ):
            self.assertEqual(evaluate_alert_rules_task()["fired"], 0)

        self.rule.refresh_from_db()
        self.assertIsNone(self.rule.breach_started_at)
        self.assertFalse(Alert.objects.filter(rule=self.rule).exists())


class ServiceScopedAlertTests(TestCase):
    def test_error_rate_uses_only_the_selected_service_routes(self):
        from vs_health.tasks import evaluate_alert_rules_task

        schools = MonitoredService.objects.create(
            key="schools", name="Schools & Onboarding", sort_order=1,
        )
        bucket = timezone.now().replace(second=0, microsecond=0) - timedelta(minutes=1)
        RequestMetric.objects.create(
            bucket_start=bucket,
            route="/v1/i/students/",
            method="GET",
            request_count=100,
            status_2xx=90,
            status_5xx=10,
            latency_hist=_hist_from([90] * 100),
        )
        RequestMetric.objects.create(
            bucket_start=bucket,
            route="/v1/finance/invoices/",
            method="GET",
            request_count=2000,
            status_2xx=2000,
            latency_hist=_hist_from([90] * 2000),
        )
        rule = AlertRule.objects.create(
            name="Schools error rate",
            metric=AlertRule.Metric.ERROR_RATE,
            comparator=AlertRule.Comparator.GT,
            threshold=5,
            duration_sec=0,
            severity=Severity.SEV1,
            target_service=schools,
        )

        self.assertEqual(evaluate_alert_rules_task()["fired"], 1)

        alert = Alert.objects.get(rule=rule)
        self.assertEqual(alert.value, 10.0)

    def test_request_metric_rule_rejects_a_service_without_request_signals(self):
        from vs_health.serializers import AlertRuleSerializer

        postgres = MonitoredService.objects.create(
            key="postgres", name="PostgreSQL", sort_order=2,
        )
        serializer = AlertRuleSerializer(data={
            "name": "Postgres request errors",
            "metric": AlertRule.Metric.ERROR_RATE,
            "comparator": AlertRule.Comparator.GT,
            "threshold": 5,
            "duration_sec": 300,
            "severity": Severity.SEV1,
            "target_service_key": postgres.key,
            "target_queue": "",
            "channel": AlertRule.Channel.EMAIL_AND_IN_APP,
            "is_enabled": True,
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("target_service_key", serializer.errors)


class AlertNotificationDeliveryTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from vs_notifications.services.seed import seed_notification_templates

        self.platform_tenant = _platform_tenant()
        self.operator = get_user_model().objects.create_user(
            tenant=self.platform_tenant,
            email="on-call@codexng.com",
            first_name="Ada",
            last_name="Okoye",
            status=get_user_model().Status.ACTIVE,
        )
        seed_notification_templates()

        self.svc = MonitoredService.objects.create(
            key="api", name="API · DRF", sort_order=1,
        )
        bucket = timezone.now().replace(second=0, microsecond=0) - timedelta(minutes=1)
        RequestMetric.objects.create(
            bucket_start=bucket,
            route="/v1/i/students/",
            method="GET",
            request_count=100,
            status_2xx=90,
            status_5xx=10,
            latency_hist=_hist_from([90] * 100),
        )
        self.rule = AlertRule.objects.create(
            name="API error rate",
            metric=AlertRule.Metric.ERROR_RATE,
            comparator=AlertRule.Comparator.GT,
            threshold=5,
            duration_sec=0,
            severity=Severity.SEV1,
            target_service=self.svc,
        )

    def test_firing_alert_creates_email_and_in_app_delivery_records(self):
        from vs_health.tasks import evaluate_alert_rules_task
        from vs_notifications.constants import ChannelChoices, NotificationStatus
        from vs_notifications.models import Notification

        with patch(
            "vs_rbac.evaluator.resolve_users_with_permission",
            return_value=[self.operator],
        ), patch(
            "vs_notifications.tasks.deliver_email_notification.delay",
        ) as delay, self.captureOnCommitCallbacks(execute=True):
            result = evaluate_alert_rules_task()
            repeated = evaluate_alert_rules_task()

        rows = Notification.all_objects.filter(
            recipient=self.operator,
            event_type__key="health.alert_fired",
        )
        self.assertEqual(result["notification_records"], 2)
        self.assertEqual(repeated["notification_records"], 0)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            set(rows.values_list("channel", flat=True)),
            {ChannelChoices.EMAIL, ChannelChoices.IN_APP},
        )
        self.assertEqual(
            rows.get(channel=ChannelChoices.IN_APP).status,
            NotificationStatus.SENT,
        )
        self.assertEqual(
            rows.get(channel=ChannelChoices.EMAIL).status,
            NotificationStatus.PENDING,
        )
        delay.assert_called_once()


class IncidentCodeTests(TestCase):
    def test_omitted_codes_are_uuid_backed_and_unique(self):
        first = Incident.objects.create(title="First incident")
        second = Incident.objects.create(title="Second incident")

        self.assertRegex(first.code, r"^INC-[0-9A-F]{16}$")
        self.assertRegex(second.code, r"^INC-[0-9A-F]{16}$")
        self.assertNotEqual(first.code, second.code)


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
            comparator=AlertRule.Comparator.GT, threshold=800, duration_sec=0,
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
            comparator=AlertRule.Comparator.GT, threshold=800, duration_sec=0,
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
            comparator=AlertRule.Comparator.GT, threshold=800, duration_sec=0,
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

        latency_rule = AlertRule.objects.get(name="p95 latency SLO")
        self.assertEqual(latency_rule.threshold, 800)
        self.assertEqual(
            latency_rule.channel,
            AlertRule.Channel.EMAIL_AND_IN_APP,
        )
        self.assertEqual(AlertRule.objects.get(name="API error rate").threshold, 5)


class RBACGatingTests(APITestCase):
    def test_overview_requires_authentication(self):
        resp = self.client.get(reverse("health-overview"))
        self.assertEqual(resp.status_code, 401)

    def test_overview_authenticated_returns_envelope(self):
        from unittest.mock import patch
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create_user(tenant=_platform_tenant(), 
            email="sre@codexng.com", first_name="S", last_name="RE",
            status=User.Status.ACTIVE,
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


class HealthTenantAnalyticsFilterTests(APITestCase):
    """The auth tenant assertion and analytics row filter stay independent."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from vs_tenants.models import Tenant

        User = get_user_model()
        self.user = User.objects.create_user(
            tenant=_platform_tenant(),
            email="health-scope@codexng.com",
            first_name="Health",
            last_name="Operator",
            status=User.Status.ACTIVE,
        )
        self.client = TenantAPIClient(user=self.user)
        self.alpha = Tenant.objects.create(
            name="Alpha Organization",
            slug="alpha-organization",
            kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        self.beta = Tenant.objects.create(
            name="Beta Organization",
            slug="beta-organization",
            kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        now = timezone.now().replace(second=0, microsecond=0)
        RequestMetric.objects.create(
            bucket_start=now,
            route="/v1/alpha/",
            method="GET",
            tenant=self.alpha,
            request_count=40,
            status_2xx=40,
            latency_sum_ms=4000,
            latency_hist=_hist_from([100] * 40),
        )
        RequestMetric.objects.create(
            bucket_start=now,
            route="/v1/beta/",
            method="GET",
            tenant=self.beta,
            request_count=80,
            status_2xx=80,
            latency_sum_ms=8000,
            latency_hist=_hist_from([100] * 80),
        )

    def _get(self, name, params):
        with patch("vs_rbac.permissions.has_permission", return_value=True):
            return self.client.get(reverse(name), params)

    def test_overview_filters_by_slug_while_authentication_uses_own_slug(self):
        response = self._get(
            "health-overview",
            {"range": "1h", "for_tenant": self.alpha.slug},
        )

        self.assertEqual(response.status_code, 200, response.content)
        series = response.json()["data"]["request_series"]
        self.assertEqual(sum(point["requests"] for point in series), 40)

    def test_api_endpoints_filter_accepts_numeric_tenant_id(self):
        response = self._get(
            "health-api-endpoints",
            {"range": "1h", "for_tenant": str(self.beta.pk)},
        )

        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]["endpoints"]
        self.assertEqual([row["route"] for row in rows], ["/v1/beta/"])

    def test_unknown_filter_is_rejected_instead_of_returning_global_data(self):
        response = self._get(
            "health-api-endpoints",
            {"range": "1h", "for_tenant": "does-not-exist"},
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("for_tenant", response.json()["error"]["detail"])

    def test_authentication_assertion_is_not_used_as_the_analytics_filter(self):
        response = self._get("health-api-endpoints", {"range": "1h"})

        self.assertEqual(response.status_code, 200, response.content)
        routes = {row["route"] for row in response.json()["data"]["endpoints"]}
        self.assertEqual(routes, {"/v1/alpha/", "/v1/beta/"})
