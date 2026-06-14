"""Celery / Redis queue snapshots.

The *task table* on the Background Jobs screen reads ``core.models.BackgroundJob``
directly (every task is tracked there). This model only stores the periodic
queue-level snapshot — depth (from the Redis broker) plus failure/throughput
aggregates — so the live depth-trend bars have history to draw.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from vs_health.constants import HealthStatus


class QueueSnapshot(models.Model):
    """Point-in-time stats for one Celery queue."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)
    queue_name = models.CharField(max_length=64, db_index=True)

    depth = models.PositiveIntegerField(default=0, help_text="Messages waiting in the broker.")
    throughput_per_min = models.FloatField(default=0.0, help_text="Tasks completed in the trailing minute.")
    failed = models.PositiveIntegerField(default=0)
    retrying = models.PositiveIntegerField(default=0)
    dead = models.PositiveIntegerField(default=0)
    avg_duration_sec = models.FloatField(null=True, blank=True)

    workers_active = models.PositiveIntegerField(default=0)
    workers_idle = models.PositiveIntegerField(default=0)
    retry_storm = models.BooleanField(default=False, help_text="Abnormal retry spike detected.")
    status = models.CharField(max_length=12, choices=HealthStatus.choices, default=HealthStatus.HEALTHY)

    class Meta:
        ordering = ["-captured_at"]
        indexes = [models.Index(fields=["queue_name", "-captured_at"])]

    def __str__(self) -> str:
        return f"{self.queue_name} depth={self.depth} @ {self.captured_at:%H:%M}"
