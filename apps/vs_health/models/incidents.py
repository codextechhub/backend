"""Incidents, war-room timeline, alert rules, and firing alerts.

Incidents are created two ways:
  * manually by an operator (``source = MANUAL``), or
  * automatically when an ``AlertRule`` breaches (``source = AUTO``) via the
    ``evaluate_alert_rules_task`` beat job.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from vs_health.constants import HealthStatus
from .base import TimeStampedModel
from .registry import MonitoredService


class Severity(models.IntegerChoices):
    SEV1 = 1, "SEV1 — Critical"
    SEV2 = 2, "SEV2 — Major"
    SEV3 = 3, "SEV3 — Minor"
    SEV4 = 4, "SEV4 — Low"


class Incident(TimeStampedModel):
    """An operational incident with a lifecycle and a timeline."""

    class Status(models.TextChoices):
        INVESTIGATING = "investigating", "Investigating"
        IDENTIFIED = "identified", "Identified"
        MONITORING = "monitoring", "Monitoring"
        RESOLVED = "resolved", "Resolved"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        AUTO = "auto", "Auto"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=20, unique=True, help_text="Human ref, e.g. 'INC-2041'.")
    title = models.CharField(max_length=255)
    severity = models.IntegerField(choices=Severity.choices, default=Severity.SEV3, db_index=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.INVESTIGATING, db_index=True,
    )
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.MANUAL)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="owned_incidents",
    )
    owner_label = models.CharField(max_length=120, blank=True, default="", help_text="Fallback owner name.")
    team = models.CharField(max_length=80, blank=True, default="")

    services = models.ManyToManyField(MonitoredService, blank=True, related_name="incidents")
    summary = models.TextField(blank=True, default="")
    postmortem = models.TextField(blank=True, default="")
    affected_tenant_count = models.PositiveIntegerField(default=0)

    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["status", "-started_at"]),
            models.Index(fields=["severity", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.title}"

    @property
    def is_active(self) -> bool:
        return self.status != self.Status.RESOLVED

    def add_event(self, *, kind: str, text: str, who: str = "System") -> "IncidentEvent":
        """Append a timeline entry (war-room update)."""
        return IncidentEvent.objects.create(incident=self, kind=kind, text=text, who=who)


class IncidentEvent(models.Model):
    """A single chronological entry on an incident's war-room timeline."""

    class Kind(models.TextChoices):
        OPENED = "opened", "Opened"
        ACK = "ack", "Acknowledged"
        UPDATE = "update", "Update"
        STATUS = "status", "Status change"
        RESOLVED = "resolved", "Resolved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name="timeline")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.UPDATE)
    who = models.CharField(max_length=120, blank=True, default="")
    text = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.incident_id} {self.kind} @ {self.created_at:%H:%M}"


class AlertRule(TimeStampedModel):
    """A threshold condition evaluated against recent metrics by the beat task."""

    class Metric(models.TextChoices):
        ERROR_RATE = "error_rate", "Error rate (%)"
        P95_LATENCY = "p95_latency", "p95 latency (ms)"
        QUEUE_DEPTH = "queue_depth", "Queue depth"
        SSL_DAYS_LEFT = "ssl_days_left", "SSL days left"
        UPTIME_PCT = "uptime_pct", "Uptime (%)"

    class Comparator(models.TextChoices):
        GT = "gt", ">"
        GTE = "gte", ">="
        LT = "lt", "<"
        LTE = "lte", "<="

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    metric = models.CharField(max_length=20, choices=Metric.choices)
    comparator = models.CharField(max_length=4, choices=Comparator.choices, default=Comparator.GT)
    threshold = models.FloatField()
    duration_sec = models.PositiveIntegerField(default=300, help_text="Sustained-for window before firing.")
    severity = models.IntegerField(choices=Severity.choices, default=Severity.SEV2)

    target_service = models.ForeignKey(
        MonitoredService, on_delete=models.CASCADE, null=True, blank=True,
        related_name="alert_rules", help_text="Null = applies platform-wide.",
    )
    target_queue = models.CharField(max_length=64, blank=True, default="", help_text="For queue_depth rules.")
    channel = models.CharField(max_length=60, blank=True, default="", help_text="e.g. 'PagerDuty', 'Slack #sre'.")
    is_enabled = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.metric} {self.comparator} {self.threshold})"

    def breaches(self, value: float) -> bool:
        """True when *value* violates this rule's comparator/threshold."""
        if value is None:
            return False
        c, t = self.comparator, self.threshold
        if c == self.Comparator.GT:
            return value > t
        if c == self.Comparator.GTE:
            return value >= t
        if c == self.Comparator.LT:
            return value < t
        if c == self.Comparator.LTE:
            return value <= t
        return False


class Alert(models.Model):
    """A firing (or resolved) instance of an ``AlertRule`` breach."""

    class Status(models.TextChoices):
        FIRING = "firing", "Firing"
        RESOLVED = "resolved", "Resolved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rule = models.ForeignKey(AlertRule, on_delete=models.CASCADE, related_name="alerts")
    severity = models.IntegerField(choices=Severity.choices, default=Severity.SEV2)
    title = models.CharField(max_length=255)
    service = models.ForeignKey(
        MonitoredService, on_delete=models.SET_NULL, null=True, blank=True, related_name="alerts",
    )
    value = models.FloatField(null=True, blank=True)
    threshold = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.FIRING, db_index=True)
    fired_at = models.DateTimeField(default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    incident = models.ForeignKey(
        Incident, on_delete=models.SET_NULL, null=True, blank=True, related_name="alerts",
    )

    class Meta:
        ordering = ["-fired_at"]
        indexes = [models.Index(fields=["status", "-fired_at"])]

    def __str__(self) -> str:
        return f"{self.title} [{self.status}]"
