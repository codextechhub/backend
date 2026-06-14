"""Uptime / synthetic checks and their results.

A ``UptimeCheck`` is a recurring probe against a service; each run writes a
``UptimeCheckResult``. Raw results power the response-time chart and the
recent end of the signature uptime bar; ``UptimeDailyRollup`` compresses
history so the 90-day bar stays cheap after raw results are pruned.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from vs_health.constants import HealthStatus
from .base import TimeStampedModel
from .registry import MonitoredService


class CheckType(models.TextChoices):
    HTTP = "http", "HTTP"
    TCP = "tcp", "TCP"
    REDIS = "redis", "Redis ping"
    POSTGRES = "postgres", "Postgres query"
    SSL = "ssl", "SSL expiry"
    INTERNAL = "internal", "Internal/derived"


class UptimeCheck(TimeStampedModel):
    """Configuration for one recurring probe against a service."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        MonitoredService, on_delete=models.CASCADE, related_name="checks",
    )
    name = models.CharField(max_length=120)
    check_type = models.CharField(max_length=12, choices=CheckType.choices, default=CheckType.HTTP)
    target = models.CharField(max_length=500, blank=True, default="", help_text="URL / host:port / dsn / domain.")
    interval_sec = models.PositiveIntegerField(default=300)
    region = models.CharField(max_length=20, blank=True, default="", help_text="Optional probe origin, e.g. 'los'.")
    expected = models.JSONField(default=dict, blank=True, help_text="e.g. {'status': 200, 'warn_ms': 400}.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["service__sort_order", "name"]

    def __str__(self) -> str:
        return f"{self.name} [{self.check_type}]"


class UptimeCheckResult(models.Model):
    """A single probe execution outcome."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    uptime_check = models.ForeignKey(UptimeCheck, on_delete=models.CASCADE, related_name="results")
    service = models.ForeignKey(MonitoredService, on_delete=models.CASCADE, related_name="check_results")
    checked_at = models.DateTimeField(default=timezone.now, db_index=True)
    status = models.CharField(max_length=12, choices=HealthStatus.choices, default=HealthStatus.UNKNOWN)
    response_ms = models.FloatField(null=True, blank=True)
    status_code = models.PositiveIntegerField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    meta = models.JSONField(default=dict, blank=True, help_text="e.g. {'ssl_days_left': 9}.")

    class Meta:
        ordering = ["-checked_at"]
        indexes = [
            models.Index(fields=["service", "-checked_at"]),
            models.Index(fields=["uptime_check", "-checked_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.uptime_check_id} {self.status} @ {self.checked_at:%H:%M}"


class UptimeDailyRollup(models.Model):
    """One row per service per day — drives the long-window uptime bars."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(MonitoredService, on_delete=models.CASCADE, related_name="daily_uptime")
    day = models.DateField(db_index=True)
    uptime_pct = models.DecimalField(max_digits=7, decimal_places=4, default=100)
    worst_status = models.CharField(max_length=12, choices=HealthStatus.choices, default=HealthStatus.HEALTHY)
    total_checks = models.PositiveIntegerField(default=0)
    failed_checks = models.PositiveIntegerField(default=0)
    avg_response_ms = models.FloatField(null=True, blank=True)

    class Meta:
        unique_together = ("service", "day")
        ordering = ["-day"]
        indexes = [models.Index(fields=["service", "-day"])]

    def __str__(self) -> str:
        return f"{self.service_id} {self.day} {self.uptime_pct}%"
