"""Aggregation/query layer feeding the API views.

Everything that turns raw rollup rows into the numbers a screen shows lives
here: time-range parsing, histogram→percentile math, golden-signal assembly,
service/tenant/queue/uptime/SLO aggregation, and reliability stats. Views stay
thin; this module owns the analytics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Count, Sum, Avg, Q, F
from django.db.models.functions import Trunc
from django.utils import timezone

from .constants import LATENCY_BUCKETS_MS, HISTOGRAM_SIZE, HealthStatus, worst_status


# ---------------------------------------------------------------------------
# Time ranges
# ---------------------------------------------------------------------------

@dataclass
# Concrete analytics window used by charts, deltas, and rollups.
class TimeRange:
    key: str
    start: object
    end: object
    prev_start: object   # start of the immediately-preceding window (for deltas)
    trunc: str           # Trunc kind for series resampling
    points: int          # nominal sparkline length


# range key -> (duration, trunc kind, nominal point count)
_RANGES = {
    "live": (timedelta(minutes=15), "minute", 15),
    "15m": (timedelta(minutes=15), "minute", 15),
    "1h": (timedelta(hours=1), "minute", 60),
    "6h": (timedelta(hours=6), "minute", 36),   # 10-min effective after resample
    "24h": (timedelta(hours=24), "hour", 24),
    "7d": (timedelta(days=7), "hour", 28),
    "30d": (timedelta(days=30), "day", 30),
}


# Resolve API range parameters into current and previous comparison windows.
def parse_range(key: str | None, start_raw: str | None = None, end_raw: str | None = None) -> TimeRange:
    """Resolve a ?range= query value into concrete window boundaries."""
    if start_raw and end_raw:
        # Custom windows are capped so ad hoc requests cannot scan unbounded telemetry.
        from django.utils.dateparse import parse_datetime
        start, end = parse_datetime(start_raw), parse_datetime(end_raw)
        if start and end and start < end and end - start <= timedelta(days=90):
            duration = end - start
            return TimeRange(key="custom", start=start, end=end, prev_start=start-duration,
                             trunc="hour" if duration <= timedelta(days=7) else "day", points=48)
    key = (key or "1h").lower()
    duration, trunc, points = _RANGES.get(key, _RANGES["1h"])
    end = timezone.now()
    start = end - duration
    return TimeRange(key=key, start=start, end=end, prev_start=start - duration,
                     trunc=trunc, points=points)


# Convert a time range to minutes while avoiding divide-by-zero in live windows.
def _minutes(tr: TimeRange) -> float:
    return max((tr.end - tr.start).total_seconds() / 60.0, 1.0)


# ---------------------------------------------------------------------------
# Histogram percentiles
# ---------------------------------------------------------------------------

# Merge persisted latency histograms before percentile estimation.
def merge_hist(hists) -> list:
    """Element-wise sum of several latency histograms."""
    out = [0] * HISTOGRAM_SIZE
    for h in hists:
        if not h:
            continue
        for i in range(min(HISTOGRAM_SIZE, len(h))):
            out[i] += h[i]
    return out


# Estimate a percentile from compact latency buckets.
def percentile_from_hist(hist, p: float) -> float:
    """Estimate the p-th percentile (ms) from a bucketed histogram.

    Linear interpolation within the matched bucket; the overflow bucket reports
    the top bound as a floor (we can't know how far past it samples landed).
    """
    total = sum(hist or [])
    if not total:
        return 0.0
    target = total * (p / 100.0)
    cumulative = 0
    for i, count in enumerate(hist):
        prev = cumulative
        cumulative += count
        if cumulative >= target and count:
            if i >= len(LATENCY_BUCKETS_MS):
                # Overflow samples only prove latency exceeded the last bound.
                return float(LATENCY_BUCKETS_MS[-1])
            lower = 0.0 if i == 0 else float(LATENCY_BUCKETS_MS[i - 1])
            upper = float(LATENCY_BUCKETS_MS[i])
            frac = (target - prev) / count
            return round(lower + (upper - lower) * frac, 1)
    return float(LATENCY_BUCKETS_MS[-1])


# ---------------------------------------------------------------------------
# Core request aggregation
# ---------------------------------------------------------------------------

# Base RequestMetric queryset shared by aggregate and drill-down views.
def _base_qs(start, end, tenant_id=None, route=None):
    from .models import RequestMetric
    qs = RequestMetric.objects.filter(bucket_start__gte=start, bucket_start__lt=end)
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    if route is not None:
        qs = qs.filter(route=route)
    return qs


# Aggregate request counts, status families, and average latency from rollup rows.
def _totals(qs) -> dict:
    agg = qs.aggregate(
        reqs=Sum("request_count"),
        s2=Sum("status_2xx"), s3=Sum("status_3xx"),
        s4=Sum("status_4xx"), s5=Sum("status_5xx"),
        throttled=Sum("throttled_count"),
        lat_sum=Sum("latency_sum_ms"),
    )
    reqs = agg["reqs"] or 0
    s5 = agg["s5"] or 0
    return {
        "requests": reqs,
        "s2": agg["s2"] or 0, "s3": agg["s3"] or 0,
        "s4": agg["s4"] or 0, "s5": s5,
        "throttled": agg["throttled"] or 0,
        "error_rate": round((s5 / reqs * 100), 3) if reqs else 0.0,
        "avg_ms": round((agg["lat_sum"] or 0) / reqs, 1) if reqs else 0.0,
    }


# Pull all latency histograms from a queryset and merge them.
def _merged_hist(qs) -> list:
    return merge_hist(qs.values_list("latency_hist", flat=True))


def _delta(curr: float, prev: float) -> float:
    """Percentage change vs previous window (rounded)."""
    if prev in (0, None):
        return 0.0 if not curr else 100.0
    return round((curr - prev) / prev * 100.0, 1)


# Build the command-center KPI cards for latency, traffic, errors, and saturation.
def golden_signals(tr: TimeRange, tenant_id=None) -> dict:
    """The four KPI tiles + their sparklines and vs-previous deltas."""
    qs = _base_qs(tr.start, tr.end, tenant_id)
    # Previous window powers deltas without changing the current data slice.
    prev_qs = _base_qs(tr.prev_start, tr.start, tenant_id)

    totals = _totals(qs)
    prev_totals = _totals(prev_qs)
    p95 = percentile_from_hist(_merged_hist(qs), 95)
    prev_p95 = percentile_from_hist(_merged_hist(prev_qs), 95)

    minutes = _minutes(tr)
    rpm = round(totals["requests"] / minutes, 1)
    prev_rpm = round(prev_totals["requests"] / _minutes(tr), 1)

    series = request_series(tr, tenant_id)
    spark_traffic = [pt["requests"] for pt in series]
    spark_errors = [pt["error_rate"] for pt in series]
    spark_latency = [pt["p95"] for pt in series]

    # Saturation currently comes from datastore probes rather than request rows.
    saturation = _saturation(tr)

    return {
        "latency": {
            "value": p95, "unit": "ms", "delta": _delta(p95, prev_p95),
            "status": _status_for_latency(p95), "spark": spark_latency,
        },
        "traffic": {
            "value": rpm, "unit": "/min", "delta": _delta(rpm, prev_rpm),
            "status": HealthStatus.HEALTHY, "spark": spark_traffic,
        },
        "errors": {
            "value": totals["error_rate"], "unit": "%",
            "delta": _delta(totals["error_rate"], prev_totals["error_rate"]),
            "status": _status_for_error_rate(totals["error_rate"]), "spark": spark_errors,
        },
        "saturation": {
            "value": saturation["value"], "unit": "%", "delta": 0.0,
            "status": saturation["status"], "spark": [],
        },
    }


# Build chart-ready request series at the range-specific granularity.
def request_series(tr: TimeRange, tenant_id=None, route=None) -> list:
    """Time-bucketed traffic/error/latency series for charts.

    Resampled at ``tr.trunc`` granularity. p95 per bucket is computed from the
    merged histogram of that bucket's rows.
    """
    qs = _base_qs(tr.start, tr.end, tenant_id, route)
    rows = (
        qs.annotate(t=Trunc("bucket_start", tr.trunc))
        .values("t")
        .annotate(
            requests=Sum("request_count"),
            s2=Sum("status_2xx"), s3=Sum("status_3xx"),
            s4=Sum("status_4xx"), s5=Sum("status_5xx"),
        )
        .order_by("t")
    )
    # p95 per bucket needs the histograms, fetched in one pass keyed by bucket.
    hist_map: dict = {}
    # Percentiles need merged histograms per chart bucket, not simple averages.
    for t, hist in qs.annotate(tb=Trunc("bucket_start", tr.trunc)).values_list("tb", "latency_hist"):
        hist_map.setdefault(t, []).append(hist)

    out = []
    for r in rows:
        reqs = r["requests"] or 0
        s5 = r["s5"] or 0
        out.append({
            "t": r["t"].isoformat(),
            "requests": reqs,
            "status_2xx": r["s2"] or 0, "status_3xx": r["s3"] or 0,
            "status_4xx": r["s4"] or 0, "status_5xx": s5,
            "error_rate": round(s5 / reqs * 100, 3) if reqs else 0.0,
            "p95": percentile_from_hist(merge_hist(hist_map.get(r["t"], [])), 95),
        })
    return out


# Derive saturation from datastore probe metadata captured inside uptime checks.
def _saturation(tr: TimeRange) -> dict:
    """Worst datastore resource utilisation in-window (from uptime check meta)."""
    from .models import UptimeCheckResult
    results = UptimeCheckResult.objects.filter(
        checked_at__gte=tr.start, checked_at__lt=tr.end,
    ).exclude(meta={})
    worst = 0.0
    for meta in results.values_list("meta", flat=True):
        pct = (meta or {}).get("mem_pct")
        if pct is not None and pct > worst:
            worst = pct
    return {
        "value": round(worst, 1),
        "status": (HealthStatus.CRITICAL if worst >= 90 else
                   HealthStatus.WARNING if worst >= 75 else HealthStatus.HEALTHY),
    }


# Convert p95 latency into the shared health status vocabulary.
def _status_for_latency(p95: float) -> str:
    if p95 >= 600:
        return HealthStatus.CRITICAL
    if p95 >= 400:
        return HealthStatus.WARNING
    return HealthStatus.HEALTHY


# Convert 5xx error rate into the shared health status vocabulary.
def _status_for_error_rate(rate: float) -> str:
    if rate >= 5:
        return HealthStatus.CRITICAL
    if rate >= 1:
        return HealthStatus.WARNING
    return HealthStatus.HEALTHY


# ---------------------------------------------------------------------------
# Per-endpoint stats (API & Endpoint Health)
# ---------------------------------------------------------------------------

# Aggregate per-route health for the endpoint table.
def endpoint_stats(tr: TimeRange, tenant_id=None) -> list:
    """One entry per (route, method) with percentiles, rpm, error & throttle."""
    qs = _base_qs(tr.start, tr.end, tenant_id)
    grouped = (
        qs.values("route", "method")
        .annotate(reqs=Sum("request_count"), s5=Sum("status_5xx"),
                  s4=Sum("status_4xx"), s3=Sum("status_3xx"), s2=Sum("status_2xx"),
                  throttled=Sum("throttled_count"))
        .order_by("-reqs")
    )
    # Histograms per (route, method) for percentiles.
    hist_map: dict = {}
    # Keep histograms keyed by route/method so percentiles match each row.
    for route, method, hist in qs.values_list("route", "method", "latency_hist"):
        hist_map.setdefault((route, method), []).append(hist)

    minutes = _minutes(tr)
    out = []
    for g in grouped:
        key = (g["route"], g["method"])
        merged = merge_hist(hist_map.get(key, []))
        reqs = g["reqs"] or 0
        s5 = g["s5"] or 0
        err = round(s5 / reqs * 100, 3) if reqs else 0.0
        out.append({
            "route": g["route"],
            "method": g["method"],
            "requests": reqs,
            "rpm": round(reqs / minutes, 1),
            "p50": percentile_from_hist(merged, 50),
            "p95": percentile_from_hist(merged, 95),
            "p99": percentile_from_hist(merged, 99),
            "error_rate": err,
            "throttled": g["throttled"] or 0,
            "codes": {
                "x2": g["s2"] or 0, "x3": g["s3"] or 0,
                "x4": g["s4"] or 0, "x5": s5,
            },
            "status": (_status_for_error_rate(err) if err
                       else _status_for_latency(percentile_from_hist(merged, 95))),
        })
    return out


# Build one endpoint drill-down with histogram and top affected tenants.
def endpoint_detail(tr: TimeRange, route: str) -> dict:
    """Histogram + per-tenant breakdown for one route (drill-down drawer)."""
    qs = _base_qs(tr.start, tr.end, route=route)
    merged = _merged_hist(qs)
    totals = _totals(qs)
    by_tenant = (
        qs.exclude(tenant__isnull=True)
        .values("tenant_id", "tenant__name")
        .annotate(reqs=Sum("request_count"), s5=Sum("status_5xx"))
        .order_by("-reqs")[:10]
    )
    tenants = [{
        "tenant_id": t["tenant_id"],
        "name": t["tenant__name"],
        "requests": t["reqs"] or 0,
        "error_rate": round((t["s5"] or 0) / t["reqs"] * 100, 3) if t["reqs"] else 0.0,
    } for t in by_tenant]
    return {
        "route": route,
        "totals": totals,
        "p50": percentile_from_hist(merged, 50),
        "p95": percentile_from_hist(merged, 95),
        "p99": percentile_from_hist(merged, 99),
        "histogram": {"buckets": LATENCY_BUCKETS_MS, "counts": merged},
        "series": request_series(tr, route=route),
        "affected_tenants": tenants,
    }


# ---------------------------------------------------------------------------
# Tenant Health
# ---------------------------------------------------------------------------

# Aggregate health by tenant and flag unusually heavy request volume.
def tenant_stats(tr: TimeRange) -> list:
    """Per-institution golden signals + noisy-neighbour flag."""
    qs = _base_qs(tr.start, tr.end).exclude(tenant__isnull=True)
    grouped = (
        qs.values("tenant_id", "tenant__name")
        .annotate(reqs=Sum("request_count"), s5=Sum("status_5xx"))
        .order_by("-reqs")
    )
    rows = list(grouped)
    if not rows:
        return []

    hist_map: dict = {}
    for tid, hist in qs.values_list("tenant_id", "latency_hist"):
        hist_map.setdefault(tid, []).append(hist)

    minutes = _minutes(tr)
    total_reqs = sum(r["reqs"] or 0 for r in rows)
    # Average request load is used only as a relative noisy-neighbour baseline.
    avg_reqs = total_reqs / len(rows) if rows else 0

    out = []
    for r in rows:
        reqs = r["reqs"] or 0
        s5 = r["s5"] or 0
        err = round(s5 / reqs * 100, 3) if reqs else 0.0
        p95 = percentile_from_hist(merge_hist(hist_map.get(r["tenant_id"], [])), 95)
        # Noisy neighbour: consuming >3x the mean request volume.
        noisy = bool(avg_reqs and reqs > avg_reqs * 3)
        out.append({
            "tenant_id": r["tenant_id"],
            "name": r["tenant__name"],
            "requests": reqs,
            "rpm": round(reqs / minutes, 1),
            "error_rate": err,
            "p95": p95,
            "noisy": noisy,
            "status": (_status_for_error_rate(err) if err
                       else _status_for_latency(p95)),
        })
    return out


# ---------------------------------------------------------------------------
# Service grid / overall posture
# ---------------------------------------------------------------------------

# Return active monitored services sorted by most severe current status.
def service_grid() -> list:
    from .models import MonitoredService
    services = MonitoredService.objects.filter(is_active=True)
    # Worst-first ordering for the Command Center grid.
    rank = {HealthStatus.CRITICAL: 0, HealthStatus.WARNING: 1,
            HealthStatus.UNKNOWN: 2, HealthStatus.HEALTHY: 3}
    out = [{
        "key": s.key, "name": s.name, "group": s.group, "tier": s.tier,
        "kind": s.kind, "status": s.current_status,
        "status_changed_at": s.status_changed_at.isoformat() if s.status_changed_at else None,
    } for s in services]
    out.sort(key=lambda x: rank.get(x["status"], 4))
    return out


# Collapse all service states into the top-level status banner.
def overall_posture() -> dict:
    from .models import MonitoredService, Incident
    statuses = list(
        MonitoredService.objects.filter(is_active=True).values_list("current_status", flat=True)
    )
    crit = statuses.count(HealthStatus.CRITICAL)
    warn = statuses.count(HealthStatus.WARNING)
    if crit:
        overall, label = "critical", f"{crit} service{'s' if crit > 1 else ''} down"
    elif warn:
        overall, label = "warning", f"{warn} service{'s' if warn > 1 else ''} degraded"
    else:
        overall, label = "operational", "All systems operational"
    # Active incident count is shown beside service-derived posture.
    active = Incident.objects.filter(~Q(status=Incident.Status.RESOLVED)).count()
    return {"overall": overall, "label": label, "critical": crit,
            "warning": warn, "active_incidents": active}


def global_uptime(days: int = 30) -> float | None:
    """Mean uptime across services over the last *days* (from daily rollups).

    None when no rollups exist yet — an uptime figure must never be claimed
    without a single real check behind it.
    """
    from .models import UptimeDailyRollup
    since = (timezone.now() - timedelta(days=days)).date()
    agg = UptimeDailyRollup.objects.filter(day__gte=since).aggregate(v=Avg("uptime_pct"))
    return round(float(agg["v"]), 3) if agg["v"] is not None else None


# ---------------------------------------------------------------------------
# Queues (Background Jobs)
# ---------------------------------------------------------------------------

# Return the latest queue snapshots and worker availability summary.
def queue_overview() -> dict:
    """Latest snapshot per queue + a short depth trend, plus worker totals."""
    from .models import QueueSnapshot
    from .constants import KNOWN_QUEUES

    queues = []
    workers_active = workers_idle = 0
    for name in KNOWN_QUEUES:
        latest = QueueSnapshot.objects.filter(queue_name=name).order_by("-captured_at").first()
        if not latest:
            # Queues without snapshots stay absent rather than pretending to be healthy.
            continue
        trend = list(
            QueueSnapshot.objects.filter(queue_name=name)
            .order_by("-captured_at")[:40].values_list("depth", flat=True)
        )[::-1]
        workers_active = max(workers_active, latest.workers_active)
        workers_idle = max(workers_idle, latest.workers_idle)
        queues.append({
            "name": name, "depth": latest.depth, "status": latest.status,
            "throughput_per_min": latest.throughput_per_min,
            "failed": latest.failed, "retrying": latest.retrying, "dead": latest.dead,
            "retry_storm": latest.retry_storm, "avg_duration_sec": latest.avg_duration_sec,
            "depth_trend": trend,
            "captured_at": latest.captured_at.isoformat(),
        })
    return {
        "queues": queues,
        "workers": {"active": workers_active, "idle": workers_idle,
                    "total": workers_active + workers_idle},
    }


# ---------------------------------------------------------------------------
# Uptime monitors
# ---------------------------------------------------------------------------

# Build uptime monitor cards from daily rollups and recent raw checks.
def uptime_monitors(window_days: int = 90) -> list:
    """Per service: uptime % windows, 90-segment bar, response series, SSL."""
    from .models import MonitoredService, UptimeDailyRollup, UptimeCheckResult, CheckType
    since = (timezone.now() - timedelta(days=window_days)).date()
    out = []
    for svc in MonitoredService.objects.filter(is_active=True):
        daily = list(
            UptimeDailyRollup.objects.filter(service=svc, day__gte=since).order_by("day")
        )
        segs = [{"day": d.day.isoformat(), "status": d.worst_status, "uptime": float(d.uptime_pct)}
                for d in daily]
        recent = UptimeCheckResult.objects.filter(service=svc).order_by("-checked_at")[:48]
        resp_series = [{"t": r.checked_at.isoformat(), "ms": r.response_ms}
                       for r in reversed(list(recent)) if r.response_ms is not None]

        def _window(d):
            # Empty windows report 100% until real checks arrive for that service.
            ds = (timezone.now() - timedelta(days=d)).date()
            vals = [float(x.uptime_pct) for x in daily if x.day >= ds]
            return round(sum(vals) / len(vals), 4) if vals else 100.0

        ssl = (
            UptimeCheckResult.objects.filter(service=svc, uptime_check__check_type=CheckType.SSL)
            .order_by("-checked_at").first()
        )
        ssl_meta = (ssl.meta if ssl else {}) or {}
        out.append({
            "key": svc.key, "name": svc.name, "status": svc.current_status,
            "uptime_24h": _window(1), "uptime_7d": _window(7),
            "uptime_30d": _window(30), "uptime_90d": _window(90),
            "segments": segs,
            "response_series": resp_series,
            "avg_response_ms": round(sum(p["ms"] for p in resp_series) / len(resp_series), 1)
            if resp_series else None,
            "ssl": {"days_left": ssl_meta.get("ssl_days_left"),
                    "domain": ssl_meta.get("domain")} if ssl_meta else None,
        })
    return out


# ---------------------------------------------------------------------------
# SLOs & error budgets
# ---------------------------------------------------------------------------

# Compute SLO attainment and remaining error budget for active objectives.
def slo_status() -> list:
    from .models import SLO, UptimeDailyRollup
    out = []
    for slo in SLO.objects.filter(is_active=True).select_related("service"):
        since = (timezone.now() - timedelta(days=slo.window_days)).date()
        vals = list(
            UptimeDailyRollup.objects.filter(service=slo.service, day__gte=since)
            .values_list("uptime_pct", flat=True)
        )
        current = round(sum(float(v) for v in vals) / len(vals), 4) if vals else 100.0
        target = float(slo.target_pct)
        # Error budget remaining as a % of the allowed downtime budget.
        allowed = 100.0 - target
        # Downtime consumed beyond the target eats into the allowed error budget.
        used = max(0.0, 100.0 - current)
        budget_remaining = round(max(0.0, (allowed - used) / allowed * 100), 1) if allowed else 100.0
        out.append({
            "service": slo.service.name, "service_key": slo.service.key,
            "target": target, "current": current,
            "window_days": slo.window_days,
            "error_budget_remaining": budget_remaining,
            "breached": current < target,
        })
    return out


# ---------------------------------------------------------------------------
# Reliability stats (MTTA / MTTR / counts)
# ---------------------------------------------------------------------------

# Compute incident response and recovery statistics for the selected window.
def reliability_stats(days: int = 30) -> dict:
    from .models import Incident
    since = timezone.now() - timedelta(days=days)
    incidents = Incident.objects.filter(started_at__gte=since)

    acks, resolves = [], []
    # MTTA and MTTR only include incidents with the relevant timestamp present.
    for inc in incidents:
        if inc.acknowledged_at:
            acks.append((inc.acknowledged_at - inc.started_at).total_seconds() / 60.0)
        if inc.resolved_at:
            resolves.append((inc.resolved_at - inc.started_at).total_seconds() / 60.0)
    return {
        "mtta_min": round(sum(acks) / len(acks), 1) if acks else None,
        "mttr_min": round(sum(resolves) / len(resolves), 1) if resolves else None,
        "incidents": incidents.count(),
        "active": incidents.filter(~Q(status=Incident.Status.RESOLVED)).count(),
        "window_days": days,
    }
