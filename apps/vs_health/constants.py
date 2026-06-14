"""Shared enums and tuning constants for the vs_health (VIGIL) app.

Kept separate from ``models`` so non-model code (middleware, collectors,
services, tasks) can import status labels and the latency-histogram layout
without dragging in the ORM.
"""
from __future__ import annotations

from django.db import models


# ---------------------------------------------------------------------------
# Status vocabulary — semantic, paired with shape/icon in the UI (never colour
# alone). Mirrors the design's --status-* tokens.
# ---------------------------------------------------------------------------

class HealthStatus(models.TextChoices):
    HEALTHY = "healthy", "Healthy"
    WARNING = "warning", "Warning"
    CRITICAL = "critical", "Critical"
    UNKNOWN = "unknown", "Unknown"


# Severity ordering used to roll several statuses up into one (worst wins).
STATUS_RANK = {
    HealthStatus.UNKNOWN: 0,
    HealthStatus.HEALTHY: 1,
    HealthStatus.WARNING: 2,
    HealthStatus.CRITICAL: 3,
}


def worst_status(statuses) -> str:
    """Return the most severe status from an iterable (warning/critical win)."""
    worst = HealthStatus.UNKNOWN
    seen = False
    for s in statuses:
        seen = True
        if STATUS_RANK.get(s, 0) > STATUS_RANK.get(worst, 0):
            worst = s
    return worst if seen else HealthStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Latency histogram
# ---------------------------------------------------------------------------
# Per-request latency is folded into fixed exponential millisecond buckets so a
# single rollup row stays tiny yet still yields good p50/p95/p99 estimates when
# many rows are merged. ``LATENCY_BUCKETS_MS`` are upper bounds; one extra
# overflow bucket (>last bound) is implied, so a histogram is a list of
# ``len(LATENCY_BUCKETS_MS) + 1`` integer counts.
LATENCY_BUCKETS_MS = [
    5, 10, 25, 50, 75, 100, 150, 200, 300, 500,
    750, 1000, 1500, 2000, 3000, 5000, 10000,
]
HISTOGRAM_SIZE = len(LATENCY_BUCKETS_MS) + 1

# Bucket width, in seconds, that request metrics are aggregated into.
METRIC_BUCKET_SECONDS = 60

# The Celery queues the platform runs (mirrors apps/celery.py + the design).
KNOWN_QUEUES = ["imports", "exports", "notifications", "provisioning", "reports", "celery"]


# ---------------------------------------------------------------------------
# RBAC permission keys (registered as module.resource.action rows by seed)
# ---------------------------------------------------------------------------
PERM_VIEW = "platform.health.view"
PERM_MANAGE = "platform.health.manage"
