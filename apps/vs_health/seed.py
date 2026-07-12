"""Idempotent seeding for vs_health — CONFIGURATION ONLY.

Creates the service registry, uptime checks, alert rules, SLO targets, and
the RBAC permissions. It never writes telemetry: every measurement on the
health screens (request metrics, uptime rollups, queue snapshots, incidents,
alerts) comes exclusively from the live collectors — RequestMetricsMiddleware,
the celery-beat probe/snapshot tasks, and the alert engine. Screens are
honestly empty until real traffic and probes have accrued.

Run via ``python manage.py seed_health``. Re-running only fills gaps.
"""
from __future__ import annotations

from django.conf import settings

from .constants import PERM_VIEW, PERM_MANAGE


PROBE_BASE = getattr(settings, "HEALTH_PROBE_BASE_URL", "https://api.codexvision.io")
SSL_DOMAIN = getattr(settings, "HEALTH_SSL_DOMAIN", "api.codexvision.io")

SERVICES = [
    ("web", "Web Frontend", "Edge", "Tier 1", "internal", 10),
    ("api", "API · DRF", "Core", "Tier 1", "internal", 20),
    ("auth", "Auth / JWT", "Core", "Tier 1", "internal", 30),
    # Module services are route groups of the monolith; status derives from
    # live request metrics on their prefixes (constants.ROUTE_PREFIX_SERVICES).
    ("schools", "Schools & Onboarding", "Modules", "Tier 2", "internal", 40),
    ("billing", "Billing & Fees", "Modules", "Tier 2", "internal", 50),
    ("reports", "Report Engine", "Modules", "Tier 3", "internal", 60),
    ("celery", "Celery Workers", "Async", "Tier 2", "internal", 70),
    ("postgres", "PostgreSQL", "Data", "Tier 1", "datastore", 80),
    ("redis", "Redis", "Data", "Tier 1", "datastore", 90),
    ("smtp", "Zoho SMTP", "External", "Ext", "external", 100),
    ("payments", "Payment Gateway", "External", "Ext", "external", 110),
    ("dns", "DNS / SSL", "External", "Ext", "external", 120),
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
        defaults={"description": "Platform health and observability."})
    if resource.description != "Platform health and observability.":
        resource.description = "Platform health and observability."
        resource.save(update_fields=["description"])
    for action_name, desc in [("view", "View observability data"),
                              ("manage", "Manage incidents, alerts and deployments")]:
        PermissionAction.objects.get_or_create(name=action_name, defaults={"description": desc})

    for key, action, sens in [(PERM_VIEW, "view", "NORMAL"), (PERM_MANAGE, "manage", "SENSITIVE")]:
        permission = Permission.objects.filter(key=key).first()
        if permission is None:
            permission = Permission.objects.create(
                module=module, resource=resource,
                action=PermissionAction.objects.get(name=action),
                description=f"Health: {action}",
                sensitivity_level=sens,
            )
        elif permission.description != f"Health: {action}":
            permission.description = f"Health: {action}"
            permission.save(update_fields=["description"])
    _log(stdout, f"  permissions: {PERM_VIEW}, {PERM_MANAGE}")


def seed_services(stdout=None):
    from .models import MonitoredService
    for key, name, group, tier, kind, order in SERVICES:
        MonitoredService.objects.get_or_create(
            key=key,
            defaults={"name": name, "group": group, "tier": tier,
                      "kind": kind, "sort_order": order},
        )
    # Retire registry entries no longer in the list (e.g. the old fictional
    # "admissions" group) so the console never shows unmonitorable services.
    keys = {key for key, *_ in SERVICES}
    retired = MonitoredService.objects.exclude(key__in=keys).filter(is_active=True).update(is_active=False)
    if retired:
        _log(stdout, f"  services retired: {retired}")
    _log(stdout, f"  services: {MonitoredService.objects.filter(is_active=True).count()}")


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
    # Real TCP reachability of the configured mail relay.
    smtp_host = getattr(settings, "EMAIL_HOST", "smtp.zoho.com")
    smtp_port = getattr(settings, "EMAIL_PORT", 587)
    mk("smtp", "SMTP reachability", CheckType.TCP, f"{smtp_host}:{smtp_port}", {"timeout": 5})
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


def run(stdout=None):
    _log(stdout, "Seeding vs_health (configuration only — telemetry comes from live collectors)")
    seed_permissions(stdout)
    seed_services(stdout)
    seed_checks(stdout)
    seed_alert_rules(stdout)
    seed_slos(stdout)
    _log(stdout, "Done.")
