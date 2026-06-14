"""Idempotent seeding for vs_health.

Creates the service registry, uptime checks, alert rules, SLOs, a couple of
historical incidents, the RBAC permissions, and backfills synthetic history
(daily uptime rollups + a recent slice of request metrics) so the dashboards
render before real traffic and probes have accrued.

Run via ``python manage.py seed_health``. Re-running only fills gaps.
"""
from __future__ import annotations

import random
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .constants import HISTOGRAM_SIZE, LATENCY_BUCKETS_MS, PERM_VIEW, PERM_MANAGE


PROBE_BASE = getattr(settings, "HEALTH_PROBE_BASE_URL", "https://api.codexvision.io")
SSL_DOMAIN = getattr(settings, "HEALTH_SSL_DOMAIN", "api.codexvision.io")

SERVICES = [
    ("web", "Web Frontend", "Edge", "Tier 1", "internal", 10),
    ("api", "API · DRF", "Core", "Tier 1", "internal", 20),
    ("auth", "Auth / JWT", "Core", "Tier 1", "internal", 30),
    ("admissions", "Admissions", "Modules", "Tier 2", "internal", 40),
    ("billing", "Billing & Fees", "Modules", "Tier 2", "internal", 50),
    ("reports", "Report Engine", "Modules", "Tier 3", "internal", 60),
    ("celery", "Celery Workers", "Async", "Tier 2", "internal", 70),
    ("postgres", "PostgreSQL", "Data", "Tier 1", "datastore", 80),
    ("redis", "Redis", "Data", "Tier 1", "datastore", 90),
    ("smtp", "Zoho SMTP", "External", "Ext", "external", 100),
    ("payments", "Payment Gateway", "External", "Ext", "external", 110),
    ("dns", "DNS / SSL", "External", "Ext", "external", 120),
]

# Representative routes for the synthetic request-metric backfill.
SEED_ROUTES = [
    ("/v1/i/students/", "GET"),
    ("/v1/user/auth/login/", "POST"),
    ("/v1/finance/invoices/", "GET"),
    ("/v1/payments/initialize/", "POST"),
    ("/v1/import/students/", "POST"),
    ("/v1/finance/reports/term-sheet/", "GET"),
]


def _log(stdout, msg):
    if stdout:
        stdout.write(msg)


def seed_permissions(stdout=None):
    from vs_rbac.models import PermissionModule, PermissionResource, PermissionAction, Permission

    module, _ = PermissionModule.objects.get_or_create(
        name="platform", defaults={"description": "Platform-wide capabilities."})
    resource, _ = PermissionResource.objects.get_or_create(
        module=module, name="health",
        defaults={"description": "VIGIL observability."})
    for action_name, desc in [("view", "View observability data"),
                              ("manage", "Manage incidents, alerts and deployments")]:
        PermissionAction.objects.get_or_create(name=action_name, defaults={"description": desc})

    for key, action, sens in [(PERM_VIEW, "view", "NORMAL"), (PERM_MANAGE, "manage", "SENSITIVE")]:
        if not Permission.objects.filter(key=key).exists():
            Permission.objects.create(
                module=module, resource=resource,
                action=PermissionAction.objects.get(name=action),
                description=f"VIGIL: {action}",
                sensitivity_level=sens,
            )
    _log(stdout, f"  permissions: {PERM_VIEW}, {PERM_MANAGE}")


def seed_services(stdout=None):
    from .models import MonitoredService
    for key, name, group, tier, kind, order in SERVICES:
        MonitoredService.objects.get_or_create(
            key=key,
            defaults={"name": name, "group": group, "tier": tier,
                      "kind": kind, "sort_order": order},
        )
    _log(stdout, f"  services: {MonitoredService.objects.count()}")


def seed_checks(stdout=None):
    from .models import MonitoredService, UptimeCheck, CheckType
    svc = {s.key: s for s in MonitoredService.objects.all()}

    def mk(service_key, name, check_type, target="", expected=None, interval=300):
        s = svc.get(service_key)
        if not s:
            return
        UptimeCheck.objects.get_or_create(
            service=s, name=name,
            defaults={"check_type": check_type, "target": target,
                      "expected": expected or {}, "interval_sec": interval},
        )

    mk("web", "Web frontend", CheckType.HTTP, getattr(settings, "FRONTEND_BASE_URL", PROBE_BASE),
       {"status": 200, "warn_ms": 800})
    mk("api", "API health", CheckType.HTTP, f"{PROBE_BASE}/v1/", {"status": 200, "warn_ms": 600})
    mk("auth", "Auth endpoint", CheckType.HTTP, f"{PROBE_BASE}/v1/user/", {"warn_ms": 600})
    mk("postgres", "Postgres SELECT 1", CheckType.POSTGRES, expected={"warn_ms": 100})
    mk("redis", "Redis ping", CheckType.REDIS, expected={"warn_ms": 50})
    mk("dns", "SSL certificate", CheckType.SSL, SSL_DOMAIN, {"warn_days": 14, "critical_days": 5}, 3600)
    mk("payments", "Payments gateway", CheckType.HTTP, f"{PROBE_BASE}/v1/payments/", {"warn_ms": 900})
    _log(stdout, f"  uptime checks: {UptimeCheck.objects.count()}")


def seed_alert_rules(stdout=None):
    from .models import MonitoredService, AlertRule
    svc = {s.key: s for s in MonitoredService.objects.all()}
    M, C, S = AlertRule.Metric, AlertRule.Comparator, None
    from .models import Severity

    rules = [
        ("API error rate", M.ERROR_RATE, C.GT, 5, 300, Severity.SEV1, "api", "", "PagerDuty", True),
        ("p95 latency SLO", M.P95_LATENCY, C.GT, 400, 600, Severity.SEV2, None, "", "Slack #sre", True),
        ("Notifications backlog", M.QUEUE_DEPTH, C.GT, 2000, 0, Severity.SEV2, None, "notifications", "Zoho Cliq", True),
        ("SSL expiry", M.SSL_DAYS_LEFT, C.LT, 14, 0, Severity.SEV3, "dns", "", "Email", True),
        ("API uptime SLO", M.UPTIME_PCT, C.LT, 99.5, 0, Severity.SEV2, "api", "", "PagerDuty", True),
    ]
    for name, metric, comp, thresh, dur, sev, skey, queue, channel, on in rules:
        AlertRule.objects.get_or_create(
            name=name,
            defaults={"metric": metric, "comparator": comp, "threshold": thresh,
                      "duration_sec": dur, "severity": sev,
                      "target_service": svc.get(skey) if skey else None,
                      "target_queue": queue, "channel": channel, "is_enabled": on},
        )
    _log(stdout, f"  alert rules: {AlertRule.objects.count()}")


def seed_slos(stdout=None):
    from .models import MonitoredService, SLO
    svc = {s.key: s for s in MonitoredService.objects.all()}
    targets = [("api", 99.9, 30), ("auth", 99.95, 30), ("payments", 99.5, 30), ("reports", 99.0, 30)]
    for key, target, window in targets:
        s = svc.get(key)
        if s:
            SLO.objects.get_or_create(service=s, name="Availability",
                                      defaults={"target_pct": target, "window_days": window})
    _log(stdout, f"  slos: {SLO.objects.count()}")


def seed_incidents(stdout=None):
    from .models import Incident, MonitoredService
    if Incident.objects.filter(code="INC-2036").exists():
        return
    t = timezone.now()
    inc = Incident.objects.create(
        code="INC-2036", title="Slow term-sheet report generation",
        severity=3, status=Incident.Status.RESOLVED, source=Incident.Source.MANUAL,
        owner_label="D. Bello", team="Reports",
        summary="PDF compilation N+1 query. Fixed by prefetch + caching.",
        started_at=t - timedelta(days=2),
        resolved_at=t - timedelta(days=2) + timedelta(minutes=47),
    )
    reports = MonitoredService.objects.filter(key="reports").first()
    if reports:
        inc.services.add(reports)
    inc.add_event(kind="opened", who="Alertmanager", text="p95 on /reports/term-sheet/ > 5s.")
    inc.add_event(kind="resolved", who="D. Bello", text="Deployed fix in v4.18.2. p95 back to 240ms.")
    _log(stdout, "  incidents: sample history created")


def seed_uptime_history(stdout=None, days: int = 90):
    from .models import MonitoredService, UptimeDailyRollup
    from .constants import HealthStatus
    today = timezone.now().date()
    written = 0
    for svc in MonitoredService.objects.all():
        for offset in range(days):
            day = today - timedelta(days=offset)
            if UptimeDailyRollup.objects.filter(service=svc, day=day).exists():
                continue
            # Mostly healthy with occasional dips for realism.
            roll = random.random()
            if roll < 0.03:
                uptime, status = round(random.uniform(97.0, 99.4), 4), HealthStatus.WARNING
            elif roll < 0.01:
                uptime, status = round(random.uniform(95.0, 98.5), 4), HealthStatus.CRITICAL
            else:
                uptime, status = round(random.uniform(99.9, 100.0), 4), HealthStatus.HEALTHY
            UptimeDailyRollup.objects.create(
                service=svc, day=day, uptime_pct=uptime, worst_status=status,
                total_checks=288, failed_checks=int((100 - uptime) / 100 * 288),
                avg_response_ms=round(random.uniform(60, 240), 1),
            )
            written += 1
    _log(stdout, f"  uptime daily rollups: +{written}")


def seed_request_metrics(stdout=None, minutes: int = 90):
    """Backfill a recent slice of request metrics so KPIs/series have data."""
    from .models import RequestMetric
    now = timezone.now().replace(second=0, microsecond=0)
    created = 0
    for m in range(minutes):
        bucket = now - timedelta(minutes=m)
        for route, method in SEED_ROUTES:
            if RequestMetric.objects.filter(bucket_start=bucket, route=route,
                                            method=method, school_id=None).exists():
                continue
            count = random.randint(20, 400)
            errors = max(0, int(count * random.uniform(0, 0.02)))
            hist = [0] * HISTOGRAM_SIZE
            sum_ms = 0.0
            max_ms = 0.0
            for _ in range(count):
                lat = random.lognormvariate(4.6, 0.5)  # ~100ms median, long tail
                sum_ms += lat
                max_ms = max(max_ms, lat)
                idx = next((i for i, u in enumerate(LATENCY_BUCKETS_MS) if lat <= u), HISTOGRAM_SIZE - 1)
                hist[idx] += 1
            RequestMetric.objects.create(
                bucket_start=bucket, route=route, method=method, school_id=None,
                request_count=count, status_2xx=count - errors, status_5xx=errors,
                latency_sum_ms=round(sum_ms, 1), latency_max_ms=round(max_ms, 1),
                latency_hist=hist,
            )
            created += 1
    _log(stdout, f"  request metrics: +{created} rows")


def run(stdout=None):
    _log(stdout, "Seeding vs_health (VIGIL)…")
    seed_permissions(stdout)
    seed_services(stdout)
    seed_checks(stdout)
    seed_alert_rules(stdout)
    seed_slos(stdout)
    seed_incidents(stdout)
    seed_uptime_history(stdout)
    seed_request_metrics(stdout)
    _log(stdout, "Done.")
