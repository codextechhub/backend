# backend/apps/vision_admin_console/models.py
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from vs_institutions.models import Institution


class TimeStampedModel(models.Model):
    """Reusable timestamps (you'll see this pattern a lot in Django projects)."""
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AdminActionLog(TimeStampedModel):
    """
    A simple log of what an internal admin did in the Admin Console.

    This is the main "audit-like" record for the console UI.
    (Your full platform audit system can still exist elsewhere.)
    """

    ACTION_CHOICES = [
        ("INSTITUTION_CREATE", "Institution: Create"),
        ("INSTITUTION_UPDATE", "Institution: Update"),
        ("INSTITUTION_SUSPEND", "Institution: Suspend"),
        ("INSTITUTION_UNSUSPEND", "Institution: Unsuspend"),
        ("INSTITUTION_RESET", "Institution: Reset"),
        ("PROVISIONING_RETRY", "Provisioning: Retry"),
        ("IMPORT_RETRY", "Import: Retry"),
        ("IMPERSONATION_START", "Impersonation: Start"),
        ("IMPERSONATION_STOP", "Impersonation: Stop"),
        ("FEATURE_FLAG_SET", "Feature Flag: Set"),
    ]

    RESULT_CHOICES = [
        ("SUCCESS", "Success"),
        ("FAILED", "Failed"),
        ("BLOCKED", "Blocked"),
    ]

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="admin_console_actions",
    )
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="admin_console_actions",
        help_text="Some actions can be global, so institution can be empty.",
    )

    action = models.CharField(max_length=40, choices=ACTION_CHOICES)
    result = models.CharField(max_length=10, choices=RESULT_CHOICES, default="SUCCESS")

    reason = models.TextField(blank=True, help_text="Why the admin performed this action (required for risky actions).")

    # Useful for storing extra info like: {"step": "seed_defaults"} or {"flag": "new_ui", "enabled": true}
    metadata = models.JSONField(default=dict, blank=True)

    error_message = models.TextField(blank=True)

    def clean(self):
        # Basic rule: require a reason for high-risk actions
        risky_actions = {"INSTITUTION_RESET", "IMPERSONATION_START"}
        if self.action in risky_actions and not self.reason.strip():
            raise ValidationError("Reason is required for resets and impersonation.")

    def __str__(self):
        return f"{self.action} by {self.actor_id} ({self.result})"


class ImpersonationSession(TimeStampedModel):
    """
    Simple time-boxed impersonation record.
    The real impersonation mechanics can live in services/middleware;
    this model just tracks 'who impersonated who' and for how long.
    """

    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("ENDED", "Ended"),
    ]

    staff_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="impersonation_sessions_started",
    )
    institution = models.ForeignKey(
        Institution,
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
    ends_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        if self.ends_at <= self.started_at:
            raise ValidationError("ends_at must be after started_at.")
        if not self.justification.strip():
            raise ValidationError("justification is required.")

    def end(self):
        """Convenience method."""
        if self.status == "ENDED":
            return
        self.status = "ENDED"
        self.ended_at = timezone.now()
        self.save(update_fields=["status", "ended_at", "updated_at"])

    def __str__(self):
        return f"Impersonation {self.staff_user_id} -> {self.target_user_id} ({self.status})"


class FeatureFlag(TimeStampedModel):
    """
    Simple per-institution feature flag override.
    """
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="feature_flags",
    )

    key = models.CharField(max_length=120)
    enabled = models.BooleanField(default=False)

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="feature_flags_updated",
    )

    reason = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["institution", "key"], name="uq_feature_flag_per_institution"),
        ]

    def __str__(self):
        return f"{self.institution_id}:{self.key}={self.enabled}"


class ProvisioningEvent(TimeStampedModel):
    """
    Tracks provisioning progress for an institution.
    Keep it simple: a step name + status + message.
    """
    STATUS_CHOICES = [
        ("QUEUED", "Queued"),
        ("RUNNING", "Running"),
        ("SUCCEEDED", "Succeeded"),
        ("FAILED", "Failed"),
    ]

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="provisioning_events",
    )

    step = models.CharField(max_length=120, help_text="Example: create_schema, seed_defaults, create_admin")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES)
    message = models.TextField(blank=True)

    def __str__(self):
        return f"{self.institution_id}:{self.step} ({self.status})"


class ImportJobLog(TimeStampedModel):
    """
    Minimal import job tracking record for the Admin Console view.
    If your import engine has its own table, you may not need this;
    you can just reference that table instead.
    """
    STATUS_CHOICES = [
        ("QUEUED", "Queued"),
        ("RUNNING", "Running"),
        ("SUCCEEDED", "Succeeded"),
        ("FAILED", "Failed"),
    ]

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="import_job_logs",
    )

    job_type = models.CharField(max_length=120, help_text="Example: students_import, staff_import, classes_import")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES)

    total_rows = models.PositiveIntegerField(default=0)
    failed_rows = models.PositiveIntegerField(default=0)

    error_report = models.JSONField(default=dict, blank=True, help_text="Store a summary of errors, not full row data.")

    def __str__(self):
        return f"{self.institution_id}:{self.job_type} ({self.status})"
