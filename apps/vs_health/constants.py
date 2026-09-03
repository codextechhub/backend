"""Shared enums and tuning constants for the Health module.

Kept separate from ``models`` so non-model code (middleware, collectors,
services, tasks) can import status labels and the latency-histogram layout
without dragging in the ORM.

Latency is folded into fixed exponential millisecond buckets rather than
stored per request, so a rollup row stays tiny while still yielding good p50,
p95 and p99 estimates once many rows are merged. ``LATENCY_BUCKETS_MS`` holds
upper bounds and one overflow bucket above the last bound is implied, making a
histogram a list of ``len(LATENCY_BUCKETS_MS) + 1`` counts.

``MIN_P95_SAMPLE`` is the floor below which those estimates are not reported at
all. Traffic here runs at roughly one or two requests a minute, so a fifteen
minute window routinely holds a couple of dozen requests, and a p95 or a 5xx
rate drawn from that few samples is noise: one slow report request pushes p95
past any latency threshold and opens a SEV2, and one 500 reads as a 20% error
rate. Under the floor, percentile and ratio statuses report UNKNOWN and alert
rules skip evaluation entirely, so the module never claims a green it cannot
support and never raises a red it cannot justify. Thirty is the conventional
smallest sample at which a tail estimate is worth quoting; even there p95 rests
on the top two observations, so treat it as a floor and not a guarantee.
"""
from __future__ import annotations

from django.db import models


# ---------------------------------------------------------------------------
# Status vocabulary - semantic, paired with shape/icon in the UI (never colour
# alone). Mirrors the design's --status-* tokens.
# ---------------------------------------------------------------------------

# Health state vocabulary shared by probes, rollups, and UI summaries.
class HealthStatus(models.TextChoices):
    HEALTHY = "healthy", "Healthy"
    WARNING = "warning", "Warning"
    CRITICAL = "critical", "Critical"
    UNKNOWN = "unknown", "Unknown"


# Severity ordering used to roll several statuses up into one (worst wins).
# Numeric severity ranking used when several checks roll up into one status.
STATUS_RANK = {
    HealthStatus.UNKNOWN: 0,
    HealthStatus.HEALTHY: 1,
    HealthStatus.WARNING: 2,
    HealthStatus.CRITICAL: 3,
}


# Collapse several check statuses into the single worst visible service state.
def worst_status(statuses) -> str:
    """Return the most severe status from an iterable (warning/critical win)."""
    worst = HealthStatus.UNKNOWN
    seen = False
    for s in statuses:
        # UNKNOWN only wins when no stronger signal appears.
        seen = True
        if STATUS_RANK.get(s, 0) > STATUS_RANK.get(worst, 0):
            worst = s
    return worst if seen else HealthStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Latency histogram
# ---------------------------------------------------------------------------
#: Upper bounds, in milliseconds. See the module docstring.
LATENCY_BUCKETS_MS = [
    5, 10, 25, 50, 75, 100, 150, 200, 300, 500,
    750, 1000, 1500, 2000, 3000, 5000, 10000,
]
HISTOGRAM_SIZE = len(LATENCY_BUCKETS_MS) + 1

# ---------------------------------------------------------------------------
# Small-sample floor for ratio/percentile signals
# ---------------------------------------------------------------------------
#: Fewest samples a percentile or ratio may be claimed from. See the module
#: docstring.
MIN_P95_SAMPLE = 30

#: Bucket width, in seconds, that request metrics are folded into before
#: persistence.
METRIC_BUCKET_SECONDS = 60

#: The Celery queues the platform runs; mirrors apps/celery.py.
KNOWN_QUEUES = ["imports", "exports", "notifications", "provisioning", "reports", "celery"]

#: Module "services" are route groups of the monolith, not separate processes.
#: Their status is derived from request metrics on these prefixes, never probed.
ROUTE_PREFIX_SERVICES = {
    "schools": ("/v1/i/",),
    "billing": ("/v1/finance/", "/v1/payments/"),
    "reports": ("/v1/finance/reports/",),
}

#: The service boundary a request-derived alert rule needs. A service absent
#: here supports no error-rate or latency rule: RequestMetric has no signal for
#: it.
REQUEST_METRIC_SERVICE_PREFIXES = {
    "api": ("/v1/",),
    "auth": ("/v1/user/",),
    **ROUTE_PREFIX_SERVICES,
}


# ---------------------------------------------------------------------------
# RBAC permission keys (registered as module.resource.action rows by seed)
# ---------------------------------------------------------------------------
# RBAC keys protecting observability reads and health-management writes.
PERM_VIEW = "platform.health.view"
PERM_MANAGE = "platform.health.manage"
