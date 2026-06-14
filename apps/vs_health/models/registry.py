"""Service registry, deployment annotations, and SLO definitions.

These are the *configured* entities an operator manages; live signal data
(metrics, uptime results, alerts) reference them.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from vs_health.constants import HealthStatus
from .base import TimeStampedModel


class ServiceKind(models.TextChoices):
    INTERNAL = "internal", "Internal"      # Django/DRF surfaces, Celery
    DATASTORE = "datastore", "Datastore"   # Postgres, Redis
    EXTERNAL = "external", "External"       # SMTP, payment gateway, DNS/SSL


class MonitoredService(TimeStampedModel):
    """One logical service VIGIL watches (e.g. 'API · DRF', 'Redis').

    ``current_status`` is a denormalised cache kept fresh by the uptime/alert
    tasks so the Command Center grid renders without recomputing per request.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.SlugField(max_length=40, unique=True, help_text="Stable id, e.g. 'api'.")
    name = models.CharField(max_length=120)
    group = models.CharField(max_length=40, blank=True, default="", help_text="UI grouping, e.g. 'Core'.")
    tier = models.CharField(max_length=20, blank=True, default="", help_text="e.g. 'Tier 1'.")
    kind = models.CharField(max_length=20, choices=ServiceKind.choices, default=ServiceKind.INTERNAL)

    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=100)

    current_status = models.CharField(
        max_length=12, choices=HealthStatus.choices, default=HealthStatus.UNKNOWN, db_index=True,
    )
    status_changed_at = models.DateTimeField(null=True, blank=True)

    config = models.JSONField(default=dict, blank=True, help_text="Arbitrary probe/display config.")

    class Meta:
        ordering = ["sort_order", "name"]
        indexes = [models.Index(fields=["is_active", "current_status"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"

    def set_status(self, status: str) -> None:
        """Persist a new status only when it actually changes (cheap + stamps time)."""
        if status != self.current_status:
            self.current_status = status
            self.status_changed_at = timezone.now()
            self.save(update_fields=["current_status", "status_changed_at", "updated_at"])


class Deployment(TimeStampedModel):
    """A deploy / feature-flag / config change, drawn as a chart annotation."""

    class Kind(models.TextChoices):
        DEPLOY = "deploy", "Deploy"
        FLAG = "flag", "Feature Flag"
        CONFIG = "config", "Config Change"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.CharField(max_length=60, blank=True, default="")
    environment = models.CharField(max_length=40, default="production")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.DEPLOY)
    actor = models.CharField(max_length=120, blank=True, default="", help_text="Who/what shipped it (e.g. 'CI/CD').")
    text = models.CharField(max_length=255, blank=True, default="")
    deployed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-deployed_at"]

    def __str__(self) -> str:
        return f"{self.version or self.kind} @ {self.deployed_at:%Y-%m-%d %H:%M}"


class SLO(TimeStampedModel):
    """Service-level objective. Attainment & error budget are computed live."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        MonitoredService, on_delete=models.CASCADE, related_name="slos",
    )
    name = models.CharField(max_length=120, blank=True, default="")
    target_pct = models.DecimalField(max_digits=6, decimal_places=3, default=99.900)
    window_days = models.PositiveSmallIntegerField(default=30)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["service__sort_order", "name"]
        unique_together = ("service", "name")

    def __str__(self) -> str:
        return f"SLO<{self.service.key} {self.target_pct}% / {self.window_days}d>"
