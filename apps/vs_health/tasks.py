"""Celery beat tasks: probes, queue snapshots, alert evaluation, rollups, pruning.

All tasks are idempotent and best-effort — a missed or eager run is safe. They
are scheduled in ``apps/celery.py``.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Avg, Count, Q
from django.utils import timezone

from .constants import HealthStatus, KNOWN_QUEUES, ROUTE_PREFIX_SERVICES, worst_status

logger = logging.getLogger(__name__)

# BackgroundJob.kind → design queue name (used for throughput/failure rollups).
KIND_TO_QUEUE = {
    "import": "imports",
    "export": "exports",
    "email": "notifications",
    "notification": "notifications",
    "provision": "provisioning",
    "report": "reports",
}


# ---------------------------------------------------------------------------
# Uptime probes
# ---------------------------------------------------------------------------

# Execute active probes and refresh service cards from their latest results.
@shared_task
def run_uptime_checks_task() -> dict:
    """Execute every active uptime check and refresh each service's status."""
    from .models import UptimeCheck, UptimeCheckResult, MonitoredService
    from . import probes

    ran = 0
    affected_services = set()
    for check in UptimeCheck.objects.filter(is_active=True).select_related("service"):
        # Probe failures are stored as CRITICAL/UNKNOWN outcomes, not raised exceptions.
        outcome = probes.execute(check)
        UptimeCheckResult.objects.create(
            uptime_check=check, service=check.service,
            status=outcome["status"], response_ms=outcome["response_ms"],
            status_code=outcome["status_code"], error=outcome["error"] or "",
            meta=outcome["meta"] or {},
        )
        ran += 1
        affected_services.add(check.service_id)

    # Roll each touched service up to the worst status across its latest results.
    for svc in MonitoredService.objects.filter(id__in=affected_services):
        latest_per_check = []
        for check in svc.checks.filter(is_active=True):
            res = check.results.order_by("-checked_at").first()
            if res:
                latest_per_check.append(res.status)
        if latest_per_check:
            svc.set_status(worst_status(latest_per_check))

    module_updates = refresh_module_service_statuses()
    return {
        "checks_run": ran,
        "services_updated": len(affected_services) + module_updates,
    }


# Derive monolith module health from route-level request metrics.
def refresh_module_service_statuses(window_minutes: int = 15) -> int:
    """Derive module-service status from real request metrics.

    The "module" services (schools/billing/reports) are route groups of the
    monolith, not separate processes — nothing can probe them independently.
    Their honest status is the observed error rate + p95 latency of their own
    routes over the trailing window; with zero traffic there is no signal and
    the status is UNKNOWN, never a claimed green.
    """
    from .models import MonitoredService, RequestMetric
    from .services import _status_for_error_rate, _status_for_latency, percentile_from_hist
    from .constants import HISTOGRAM_SIZE

    since = timezone.now() - timedelta(minutes=window_minutes)
    updated = 0
    for key, prefixes in ROUTE_PREFIX_SERVICES.items():
        svc = MonitoredService.objects.filter(key=key, is_active=True).first()
        if svc is None:
            continue
        route_q = Q()
        # Prefix groups map logical modules onto real DRF routes.
        for prefix in prefixes:
            route_q |= Q(route__startswith=prefix)
        rows = RequestMetric.objects.filter(bucket_start__gte=since).filter(route_q)

        requests = 0
        errors = 0
        hist = [0] * HISTOGRAM_SIZE
        for row in rows.values_list("request_count", "status_5xx", "latency_hist"):
            requests += row[0]
            errors += row[1]
            for i, count in enumerate(row[2][:HISTOGRAM_SIZE]):
                hist[i] += count

        if requests == 0:
            # No traffic is no signal; do not report green for an unobserved module.
            svc.set_status(HealthStatus.UNKNOWN)
        else:
            error_rate = round(errors / requests * 100, 2)
            p95 = percentile_from_hist(hist, 95)
            svc.set_status(worst_status([
                _status_for_error_rate(error_rate),
                _status_for_latency(p95),
            ]))
        updated += 1
    return updated


# ---------------------------------------------------------------------------
# Queue snapshot
# ---------------------------------------------------------------------------

# Read Redis broker queue depths when the configured broker supports LLEN.
def _broker_depths() -> dict:
    """LLEN per queue list on the Redis broker. Empty dict if unavailable."""
    from django.conf import settings
    url = getattr(settings, "CELERY_BROKER_URL", "")
    if not url.startswith("redis"):
        return {}
    try:
        import redis
        client = redis.from_url(url, socket_connect_timeout=3)
        return {q: int(client.llen(q) or 0) for q in KNOWN_QUEUES}
    except Exception:
        logger.debug("broker depth probe failed", exc_info=True)
        return {}


# Estimate worker capacity from Celery inspect without failing the task on broker issues.
def _worker_counts() -> tuple[int, int]:
    """(active, idle) worker estimate from Celery inspect. (0,0) if no workers."""
    try:
        from apps.celery import app
        insp = app.control.inspect(timeout=2)
        stats = insp.stats() or {}
        active = insp.active() or {}
        total = sum((s.get("pool", {}).get("max-concurrency", 0)) for s in stats.values())
        busy = sum(len(v) for v in active.values())
        return busy, max(0, total - busy)
    except Exception:
        return 0, 0


# Capture queue depth, recent job outcomes, and Celery service posture.
@shared_task
def capture_queue_snapshot_task() -> dict:
    """Snapshot depth + trailing-minute throughput/failures for each queue."""
    from core.models import BackgroundJob
    from .models import QueueSnapshot

    depths = _broker_depths()
    workers_active, workers_idle = _worker_counts()
    window_start = timezone.now() - timedelta(minutes=1)

    # Trailing-window job aggregates grouped by mapped queue.
    # Throughput/failure signals come from tracked jobs rather than broker messages alone.
    recent = BackgroundJob.objects.filter(created_at__gte=window_start)
    per_queue = {q: {"throughput": 0, "failed": 0, "running": 0} for q in KNOWN_QUEUES}
    for job in recent.values("kind", "status"):
        q = KIND_TO_QUEUE.get((job["kind"] or "").lower(), "celery")
        bucket = per_queue.setdefault(q, {"throughput": 0, "failed": 0, "running": 0})
        if job["status"] == BackgroundJob.Status.SUCCEEDED:
            bucket["throughput"] += 1
        elif job["status"] == BackgroundJob.Status.FAILED:
            bucket["failed"] += 1
        elif job["status"] == BackgroundJob.Status.RUNNING:
            bucket["running"] += 1

    created = 0
    for name in KNOWN_QUEUES:
        depth = depths.get(name, 0)
        agg = per_queue.get(name, {"throughput": 0, "failed": 0, "running": 0})
        failed = agg["failed"]
        retrying = agg["running"]
        retry_storm = failed >= 50
        # Depth and retry storms both indicate queue saturation.
        if depth >= 5000 or retry_storm:
            status = HealthStatus.CRITICAL
        elif depth >= 2000 or failed >= 10:
            status = HealthStatus.WARNING
        else:
            status = HealthStatus.HEALTHY
        QueueSnapshot.objects.create(
            queue_name=name, depth=depth,
            throughput_per_min=float(agg["throughput"]),
            failed=failed, retrying=retrying, dead=0,
            workers_active=workers_active, workers_idle=workers_idle,
            retry_storm=retry_storm, status=status,
        )
        created += 1

    # The Celery service card reflects real worker presence: workers online →
    # healthy; broker reachable but no workers → critical (jobs would stall);
    # broker unreachable/not redis → unknown (no signal, no claim).
    from .models import MonitoredService
    celery_svc = MonitoredService.objects.filter(key="celery", is_active=True).first()
    if celery_svc:
        # Worker presence is the strongest signal for whether async jobs can drain.
        if workers_active + workers_idle > 0:
            celery_svc.set_status(HealthStatus.HEALTHY)
        elif depths:
            celery_svc.set_status(HealthStatus.CRITICAL)
        else:
            celery_svc.set_status(HealthStatus.UNKNOWN)
    return {"snapshots": created, "workers_active": workers_active}


# ---------------------------------------------------------------------------
# Alert evaluation + auto-incidents
# ---------------------------------------------------------------------------

# Allocate the next human-readable auto-incident code.
def _next_incident_code() -> str:
    from .models import Incident
    last = Incident.objects.filter(code__startswith="INC-").order_by("-code").first()
    n = 2000
    if last:
        try:
            n = int(last.code.split("-")[1])
        except (IndexError, ValueError):
            pass
    return f"INC-{n + 1}"


# Resolve the current observed value for an alert rule metric.
def _current_metric_value(rule):
    """Resolve the live value a rule is evaluated against, or None."""
    from .models import QueueSnapshot, UptimeDailyRollup, UptimeCheckResult, AlertRule, CheckType
    from . import services

    tr = services.parse_range("15m")
    # Widen the window to the rule's sustained-for duration when it's longer.
    if rule.duration_sec > 900:
        # Sustained rules evaluate over their configured duration when it exceeds 15m.
        tr.start = tr.end - timedelta(seconds=rule.duration_sec)

    if rule.metric == AlertRule.Metric.ERROR_RATE:
        return services._totals(services._base_qs(tr.start, tr.end))["error_rate"]
    if rule.metric == AlertRule.Metric.P95_LATENCY:
        return services.percentile_from_hist(
            services._merged_hist(services._base_qs(tr.start, tr.end)), 95)
    if rule.metric == AlertRule.Metric.QUEUE_DEPTH:
        latest = (QueueSnapshot.objects.filter(queue_name=rule.target_queue or "celery")
                  .order_by("-captured_at").first())
        return latest.depth if latest else None
    if rule.metric == AlertRule.Metric.SSL_DAYS_LEFT:
        if not rule.target_service_id:
            return None
        res = (UptimeCheckResult.objects.filter(
            service=rule.target_service, uptime_check__check_type=CheckType.SSL)
            .order_by("-checked_at").first())
        return (res.meta or {}).get("ssl_days_left") if res else None
    if rule.metric == AlertRule.Metric.UPTIME_PCT:
        if not rule.target_service_id:
            return None
        since = (timezone.now() - timedelta(days=1)).date()
        agg = UptimeDailyRollup.objects.filter(
            service=rule.target_service, day__gte=since).aggregate(v=Avg("uptime_pct"))
        return float(agg["v"]) if agg["v"] is not None else None
    return None


# Fire and resolve alerts, opening or closing auto-incidents as needed.
@shared_task
def evaluate_alert_rules_task() -> dict:
    """Fire/resolve alerts from rule breaches and auto-manage their incidents."""
    from .models import AlertRule, Alert, Incident

    fired = resolved = 0
    for rule in AlertRule.objects.filter(is_enabled=True).select_related("target_service"):
        value = _current_metric_value(rule)
        breaching = rule.breaches(value)
        open_alert = Alert.objects.filter(rule=rule, status=Alert.Status.FIRING).first()

        if breaching and not open_alert:
            # First breach opens exactly one firing alert and one linked auto-incident.
            title = f"{rule.name}: {value} {rule.get_comparator_display()} {rule.threshold}"
            incident = _open_auto_incident(rule, title, value)
            Alert.objects.create(
                rule=rule, severity=rule.severity, title=title,
                service=rule.target_service, value=value, threshold=rule.threshold,
                status=Alert.Status.FIRING, incident=incident,
            )
            fired += 1
        elif not breaching and open_alert:
            # Clearing the metric resolves the alert and may resolve the linked incident.
            open_alert.status = Alert.Status.RESOLVED
            open_alert.resolved_at = timezone.now()
            open_alert.value = value
            open_alert.save(update_fields=["status", "resolved_at", "value"])
            _maybe_resolve_auto_incident(open_alert.incident)
            resolved += 1
    return {"fired": fired, "resolved": resolved}


# Create the incident record attached to a newly firing alert.
def _open_auto_incident(rule, title, value):
    from .models import Incident
    incident = Incident.objects.create(
        code=_next_incident_code(),
        title=title,
        severity=rule.severity,
        status=Incident.Status.INVESTIGATING,
        source=Incident.Source.AUTO,
        owner_label="Alertmanager",
        team="Platform",
        summary=f"Auto-opened from alert rule '{rule.name}'. Observed {value}.",
    )
    if rule.target_service_id:
        incident.services.add(rule.target_service)
    incident.add_event(kind="opened", who="Alertmanager",
                       text=f"{rule.name} breached: {value} {rule.get_comparator_display()} {rule.threshold}.")
    return incident


# Resolve auto-incidents only after all linked alerts have cleared.
def _maybe_resolve_auto_incident(incident):
    from .models import Incident, Alert
    if not incident or incident.source != Incident.Source.AUTO:
        return
    if incident.status == Incident.Status.RESOLVED:
        return
    # Multiple alert rules can point at one auto-incident; wait for all to clear.
    still_firing = Alert.objects.filter(incident=incident, status=Alert.Status.FIRING).exists()
    if still_firing:
        return
    incident.status = Incident.Status.RESOLVED
    incident.resolved_at = timezone.now()
    incident.save(update_fields=["status", "resolved_at", "updated_at"])
    incident.add_event(kind="resolved", who="Alertmanager", text="All linked alerts cleared.")


# ---------------------------------------------------------------------------
# Rollups + retention
# ---------------------------------------------------------------------------

# Fold raw uptime probe results into daily service rollups.
@shared_task
def rollup_uptime_daily_task(days_back: int = 2) -> dict:
    """Aggregate raw uptime results into per-service daily rollups."""
    from .models import MonitoredService, UptimeCheckResult, UptimeDailyRollup

    today = timezone.now().date()
    written = 0
    for offset in range(days_back + 1):
        day = today - timedelta(days=offset)
        day_start = timezone.make_aware(timezone.datetime(day.year, day.month, day.day))
        day_end = day_start + timedelta(days=1)
        for svc in MonitoredService.objects.filter(is_active=True):
            results = UptimeCheckResult.objects.filter(
                service=svc, checked_at__gte=day_start, checked_at__lt=day_end)
            total = results.count()
            if not total:
                # Do not create synthetic uptime rows when no probes ran for the day.
                continue
            failed = results.filter(
                status__in=[HealthStatus.CRITICAL, HealthStatus.WARNING]).count()
            uptime = round((total - failed) / total * 100, 4)
            statuses = list(results.values_list("status", flat=True))
            avg_ms = results.exclude(response_ms__isnull=True).aggregate(v=Avg("response_ms"))["v"]
            UptimeDailyRollup.objects.update_or_create(
                service=svc, day=day,
                defaults={
                    "uptime_pct": uptime,
                    "worst_status": worst_status(statuses),
                    "total_checks": total, "failed_checks": failed,
                    "avg_response_ms": round(avg_ms, 1) if avg_ms is not None else None,
                },
            )
            written += 1
    return {"rollups_written": written}


# Apply retention windows to raw observability rows.
@shared_task
def prune_health_metrics_task() -> dict:
    """Retention: drop raw rows past their window (rollups keep the long view)."""
    from .models import RequestMetric, UptimeCheckResult, QueueSnapshot, Alert

    now = timezone.now()
    # Rollups keep long-term visibility, so raw high-cardinality rows can expire.
    deleted = {}
    deleted["request_metrics"] = RequestMetric.objects.filter(
        bucket_start__lt=now - timedelta(days=7)).delete()[0]
    deleted["uptime_results"] = UptimeCheckResult.objects.filter(
        checked_at__lt=now - timedelta(days=7)).delete()[0]
    deleted["queue_snapshots"] = QueueSnapshot.objects.filter(
        captured_at__lt=now - timedelta(days=3)).delete()[0]
    deleted["resolved_alerts"] = Alert.objects.filter(
        status=Alert.Status.RESOLVED, resolved_at__lt=now - timedelta(days=30)).delete()[0]
    return deleted
