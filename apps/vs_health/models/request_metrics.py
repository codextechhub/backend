"""Per-request rollup metric — the backbone of the golden signals.

One row aggregates every request that shares a (1-minute bucket, route,
method, tenant). Latency is folded into a fixed histogram so percentiles can
be estimated after merging arbitrary rows/time-ranges without storing raw
samples. Written by ``vs_health.collectors`` via F() increments so concurrent
gunicorn workers safely merge into the same row.
"""
from __future__ import annotations

import uuid

from django.db import models

from vs_health.constants import HISTOGRAM_SIZE


def _empty_hist() -> list:
    return [0] * HISTOGRAM_SIZE


class RequestMetric(models.Model):
    """Aggregated request counters + latency histogram for one bucket/route/tenant.

    Notes:
        * ``route`` is the *resolved* URL pattern (e.g. ``v1/finance/invoices/``),
          never the raw path, to keep cardinality bounded.
        * ``school`` is nullable: unauthenticated / platform-scoped requests
          have no tenant. This column is for slicing, not isolation — the table
          is global observability data gated by platform RBAC.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    bucket_start = models.DateTimeField(db_index=True, help_text="Start of the 1-minute window (UTC).")
    route = models.CharField(max_length=255, db_index=True)
    method = models.CharField(max_length=10)
    school = models.ForeignKey(
        "vs_schools.School", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    request_count = models.PositiveIntegerField(default=0)
    status_2xx = models.PositiveIntegerField(default=0)
    status_3xx = models.PositiveIntegerField(default=0)
    status_4xx = models.PositiveIntegerField(default=0)
    status_5xx = models.PositiveIntegerField(default=0)
    throttled_count = models.PositiveIntegerField(default=0)

    latency_sum_ms = models.FloatField(default=0.0)
    latency_max_ms = models.FloatField(default=0.0)
    latency_hist = models.JSONField(default=_empty_hist, help_text="Counts per LATENCY_BUCKETS_MS bucket (+overflow).")

    class Meta:
        # One canonical row per dimension tuple — collectors upsert into it.
        unique_together = ("bucket_start", "route", "method", "school")
        indexes = [
            models.Index(fields=["bucket_start", "route"]),
            models.Index(fields=["bucket_start", "school"]),
        ]
        ordering = ["-bucket_start"]

    def __str__(self) -> str:
        return f"{self.method} {self.route} @ {self.bucket_start:%H:%M} ({self.request_count})"

    @property
    def error_count(self) -> int:
        return self.status_5xx

    @property
    def avg_latency_ms(self) -> float:
        return (self.latency_sum_ms / self.request_count) if self.request_count else 0.0
