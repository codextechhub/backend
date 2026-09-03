"""Celery beat tasks: probes, queue snapshots, alert evaluation, rollups, pruning.

All tasks are idempotent and best-effort - a missed or eager run is safe. They
are scheduled in ``apps/celery.py``.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.utils import timezone

from .constants import (
    HealthStatus,
    KNOWN_QUEUES,
    REQUEST_METRIC_SERVICE_PREFIXES,
    ROUTE_PREFIX_SERVICES,
    worst_status,
)

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
    monolith, not separate processes - nothing can probe them independently.
    Their honest status is the observed error rate + p95 latency of their own
    routes over the trailing window; with too little traffic (see
    ``MIN_P95_SAMPLE``) there is no usable signal and the status is UNKNOWN,
    never a claimed green nor a red one slow request produced.
    """
    from .models import MonitoredService, RequestMetric
    from .services import percentile_from_hist, window_status
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

        error_rate = round(errors / requests * 100, 2) if requests else 0.0
        p95 = percentile_from_hist(hist, 95)
        # window_status returns UNKNOWN for zero traffic *and* for windows below
        # the small-sample floor - both are "no signal", not a claim.
        svc.set_status(window_status(requests, error_rate, p95))
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

    # Real worker presence: workers online is healthy, a reachable broker with
    # no workers is critical because jobs would stall, and an unreachable
    # broker is unknown because there is no signal to claim anything from.
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

# Resolve the current observed value for an alert rule metric.
def _current_metric_value(rule):
    """Resolve the live value a rule is evaluated against, or None.

    None means "no evaluable signal this run": ``AlertRule.breaches(None)`` is
    False, so the rule neither fires nor blocks an open alert from resolving.
    """
    from .models import QueueSnapshot, UptimeDailyRollup, UptimeCheckResult, AlertRule, CheckType
    from . import services
    from .constants import MIN_P95_SAMPLE

    tr = services.parse_range("15m")

    # Ratio and percentile estimates both: under the sample floor one slow or
    # one failed request swings them past any threshold.
    if rule.metric in (AlertRule.Metric.ERROR_RATE, AlertRule.Metric.P95_LATENCY):
        qs = services._base_qs(tr.start, tr.end)
        if rule.target_service_id:
            prefixes = REQUEST_METRIC_SERVICE_PREFIXES.get(rule.target_service.key)
            if not prefixes:
                return None
            route_q = Q()
            for prefix in prefixes:
                route_q |= Q(route__startswith=prefix)
            qs = qs.filter(route_q)
        totals = services._totals(qs)
        if totals["requests"] < MIN_P95_SAMPLE:
            return None
        if rule.metric == AlertRule.Metric.ERROR_RATE:
            return totals["error_rate"]
        return services.percentile_from_hist(services._merged_hist(qs), 95)
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
    """Evaluate enabled rules serially and notify operators after sustained breaches."""
    from .models import AlertRule, Alert

    fired = resolved = notification_records = 0
    rule_ids = list(
        AlertRule.objects.filter(is_enabled=True).values_list("id", flat=True)
    )
    for rule_id in rule_ids:
        with transaction.atomic():
            # The rule row is the mutex for its evaluation state, so overlapping beat
            # runs cannot both see a first breach and open duplicate incidents.
            rule = (
                AlertRule.objects.select_for_update()
                .filter(id=rule_id, is_enabled=True)
                .first()
            )
            if rule is None:
                continue
            value = _current_metric_value(rule)
            breaching = rule.breaches(value)
            open_alert = (
                Alert.objects.filter(rule=rule, status=Alert.Status.FIRING)
                .select_related("incident")
                .first()
            )
            now = timezone.now()

            if breaching:
                if rule.breach_started_at is None:
                    rule.breach_started_at = now
                    rule.save(update_fields=["breach_started_at", "updated_at"])
                sustained_for = (now - rule.breach_started_at).total_seconds()
                ready = rule.duration_sec == 0 or sustained_for >= rule.duration_sec
                if ready and not open_alert:
                    title = (
                        f"{rule.name}: {value} "
                        f"{rule.get_comparator_display()} {rule.threshold}"
                    )
                    incident = _open_auto_incident(rule, title, value)
                    alert = Alert.objects.create(
                        rule=rule,
                        severity=rule.severity,
                        title=title,
                        service=rule.target_service,
                        value=value,
                        threshold=rule.threshold,
                        status=Alert.Status.FIRING,
                        incident=incident,
                    )
                    notification_records += _dispatch_alert_notification(alert)
                    fired += 1
            else:
                if rule.breach_started_at is not None:
                    rule.breach_started_at = None
                    rule.save(update_fields=["breach_started_at", "updated_at"])
                if open_alert:
                    # Clearing the metric resolves the alert and may resolve its incident.
                    open_alert.status = Alert.Status.RESOLVED
                    open_alert.resolved_at = now
                    open_alert.value = value
                    open_alert.save(update_fields=["status", "resolved_at", "value"])
                    _maybe_resolve_auto_incident(open_alert.incident)
                    resolved += 1
    return {
        "fired": fired,
        "resolved": resolved,
        "notification_records": notification_records,
    }


# Create the incident record attached to a newly firing alert.
def _open_auto_incident(rule, title, value):
    from .models import Incident
    incident = Incident.objects.create(
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


# Deliver a firing alert to every platform operator who can manage health incidents.
def _dispatch_alert_notification(alert) -> int:
    from vs_rbac.evaluator import resolve_users_with_permission
    from vs_tenants.models import Tenant

    from .constants import PERM_MANAGE
    from .models import AlertRule

    if alert.rule.channel != AlertRule.Channel.EMAIL_AND_IN_APP:
        logger.error(
            "Health alert %s has unsupported channel %s.",
            alert.id,
            alert.rule.channel,
        )
        alert.incident.add_event(
            kind="update",
            who="Alertmanager",
            text="Notification delivery failed because the rule channel is unsupported.",
        )
        return 0

    try:
        platform_tenant = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        recipients = list(resolve_users_with_permission(
            tenant=platform_tenant,
            branch=None,
            permission_key=PERM_MANAGE,
        ))
        if not recipients:
            logger.error(
                "Health alert %s has no active platform.health.manage recipients.",
                alert.id,
            )
            alert.incident.add_event(
                kind="update",
                who="Alertmanager",
                text=(
                    "Notification delivery failed because no active platform operator "
                    "holds platform.health.manage."
                ),
            )
            return 0

        from vs_notifications.notify import send_notification

        notification_ids = send_notification(
            event_key="health.alert_fired",
            context={
                "incident_code": alert.incident.code,
                "rule_name": alert.rule.name,
                "severity_label": alert.get_severity_display(),
                "service_name": (
                    alert.service.name if alert.service_id else "Platform-wide"
                ),
                "observed_value": alert.value,
                "comparator": alert.rule.get_comparator_display(),
                "threshold": alert.threshold,
                "fired_at": alert.fired_at.isoformat(),
            },
            recipients=recipients,
            tenant=platform_tenant,
            metadata={
                "health_alert_id": str(alert.id),
                "incident_id": str(alert.incident_id),
                "incident_code": alert.incident.code,
            },
        )
        expected_records = len(recipients) * 2
        if len(notification_ids) == expected_records:
            delivery_text = (
                "Created email and in-app notifications for "
                f"{len(recipients)} platform operator(s)."
            )
        else:
            delivery_text = (
                f"Created {len(notification_ids)} of {expected_records} expected "
                "notification records. Review the health alert event and templates."
            )
            logger.error(
                "Health alert %s created %d of %d expected notification records.",
                alert.id,
                len(notification_ids),
                expected_records,
            )
        alert.incident.add_event(
            kind="update",
            who="Alertmanager",
            text=delivery_text,
        )
        return len(notification_ids)
    except Exception:
        # The incident must survive a routing or template failure so the health
        # console exposes both the outage and why nobody was contacted.
        logger.exception("Health alert %s notification dispatch failed.", alert.id)
        alert.incident.add_event(
            kind="update",
            who="Alertmanager",
            text="Notification dispatch failed. Review notification delivery logs.",
        )
        return 0


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
