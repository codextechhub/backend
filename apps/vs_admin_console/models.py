from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

class ImpersonationSession(models.Model):
    """
    Simple time-boxed impersonation record.
    The real impersonation mechanics can live in services/middleware;
    this model just tracks 'who impersonated who' and for how long.
    """

    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("ENDED", "Ended"),
        ("EXPIRED", "Expired"),
    ]

    staff_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="impersonation_sessions_started",
    )
    tenant = models.ForeignKey(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="impersonation_sessions",
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="impersonation_sessions_as_target",
    )

    justification = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="ACTIVE")

    started_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        if self.ends_at is not None and self.ends_at <= self.started_at:
            raise ValidationError("ends_at must be after started_at.")
        if not self.justification.strip():
            raise ValidationError("justification is required.")
        if self.tenant_id and self.target_user_id and self.target_user.tenant_id != self.tenant_id:
            raise ValidationError("Target user must belong to the impersonation tenant.")

    def end(self):
        """Convenience method."""
        if self.status == "ENDED":
            return
        self.status = "ENDED"
        self.ended_at = timezone.now()
        self.save(update_fields=["status", "ended_at"])

    def __str__(self):
        return f"Impersonation {self.staff_user_id} -> {self.target_user_id} ({self.status})"
