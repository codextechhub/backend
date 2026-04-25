from __future__ import annotations

import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from vs_schools.models import School


# -----------------------------------------------------------------------------
# Reusable base
# -----------------------------------------------------------------------------

class TimeStampedModel(models.Model):
    """Abstract base that adds immutable created/updated timestamps.

    Attributes:
        created_at (DateTimeField): Stores when the row was first created.
        updated_at (DateTimeField): Automatically refreshed on every save.
    """
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# -----------------------------------------------------------------------------
# Small enums / choices
# -----------------------------------------------------------------------------

class AuditSeverity(models.TextChoices):
    """Normalized severity levels attached to every audit event."""
    INFO = "INFO", "Info"
    WARNING = "WARNING", "Warning"
    CRITICAL = "CRITICAL", "Critical"


class AuditActorType(models.TextChoices):
    """Defines whether an event was triggered by a user or the system."""
    USER = "USER", "User"
    SYSTEM = "SYSTEM", "System"


class AuditStatus(models.TextChoices):
    """Represents the outcome of the audited action."""
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"
    DENIED = "DENIED", "Denied"
    PARTIAL = "PARTIAL", "Partial"


class AuditModuleKey(models.TextChoices):
    """High-level product surface or module where the action occurred."""
    ONBOARDING = "ONBOARDING", "Onboarding"
    IDENTITY = "IDENTITY", "Identity & Auth"
    USER = "USER", "User Management"
    RBAC = "RBAC", "Roles & Permissions"
    IMPORT = "IMPORT", "Data Import"
    CONFIG = "CONFIG", "System Configuration"
    FINANCE = "FINANCE", "Finance"
    PROCUREMENT = "PROCUREMENT", "Procurement"
    SCHOOL = "SCHOOL", "School Management"
    BRANCH = "BRANCH", "Branch Management"
    SYSTEM = "SYSTEM", "System"


class AuditActionType(models.TextChoices):
    """Specific auditable actions that can be emitted by the platform."""
    # Generic CRUD
    CREATE = "CREATE", "Create"
    UPDATE = "UPDATE", "Update"
    DELETE = "DELETE", "Delete"

    # Identity / authentication
    USER_CREATED = "USER_CREATED", "User Created"
    USER_INVITED = "USER_INVITED", "User Invited"
    ACCOUNT_ACTIVATED = "ACCOUNT_ACTIVATED", "Account Activated"
    LOGIN_SUCCESS = "LOGIN_SUCCESS", "Login Success"
    LOGIN_FAILED = "LOGIN_FAILED", "Login Failed"
    TOKEN_REVOKED = "TOKEN_REVOKED", "Token Revoked"
    FORCE_LOGOUT = "FORCE_LOGOUT", "Force Logout"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED", "Account Locked"
    ACCOUNT_UNLOCKED = "ACCOUNT_UNLOCKED", "Account Unlocked"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED", "Account Suspended"
    ACCOUNT_REACTIVATED = "ACCOUNT_REACTIVATED", "Account Reactivated"
    ACCOUNT_DEACTIVATED = "ACCOUNT_DEACTIVATED", "Account Deactivated"
    PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED", "Password Reset Requested"
    PASSWORD_RESET = "PASSWORD_RESET", "Password Reset"
    PASSWORD_CHANGED = "PASSWORD_CHANGED", "Password Changed"
    EMAIL_CHANGED = "EMAIL_CHANGED", "Email Changed"

    # Data import
    DATA_FILE_UPLOADED = "DATA_FILE_UPLOADED", "Data File Uploaded"
    DATA_IMPORT_STARTED = "DATA_IMPORT_STARTED", "Data Import Started"
    DATA_IMPORT_ROW_PROCESSED = "DATA_IMPORT_ROW_PROCESSED", "Data Import Row Processed"
    DATA_IMPORT_COMPLETED = "DATA_IMPORT_COMPLETED", "Data Import Completed"
    DATA_IMPORT_FAILED = "DATA_IMPORT_FAILED", "Data Import Failed"
    DATA_IMPORT_ROLLED_BACK = "DATA_IMPORT_ROLLED_BACK", "Data Import Rolled Back"

    # RBAC
    ROLE_ASSIGNED = "ROLE_ASSIGNED", "Role Assigned"
    ROLE_CHANGED = "ROLE_CHANGED", "Role Changed"
    PERMISSION_CHANGED = "PERMISSION_CHANGED", "Permission Changed"

    # Other
    CONFIG_CHANGED = "CONFIG_CHANGED", "Configuration Changed"
    FINANCIAL_TRANSACTION = "FINANCIAL_TRANSACTION", "Financial Transaction"
    PROCUREMENT_ACTION = "PROCUREMENT_ACTION", "Procurement Action"
    EXPORT_REQUESTED = "EXPORT_REQUESTED", "Export Requested"
    EXPORT_COMPLETED = "EXPORT_COMPLETED", "Export Completed"
    EXPORT_FAILED = "EXPORT_FAILED", "Export Failed"
    CUSTOM = "CUSTOM", "Custom"


class ExportFormat(models.TextChoices):
    """Available file formats for audit exports."""
    CSV = "CSV", "CSV"


class ExportJobStatus(models.TextChoices):
    """Lifecycle states for `AuditExportJob` records."""
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"
    EXPIRED = "EXPIRED", "Expired"


class ComplianceRuleType(models.TextChoices):
    """Categories of compliance automation handled by the audit service."""
    RETENTION = "RETENTION", "Retention"
    MASKING = "MASKING", "Masking"
    ACCESS = "ACCESS", "Access"
    EXPORT = "EXPORT", "Export"


# -----------------------------------------------------------------------------
# Main audit event model
# -----------------------------------------------------------------------------

class AuditEvent(models.Model):
    """Immutable record describing a sensitive action performed in the product.

    Attributes:
        id (UUIDField): Primary key uniquely identifying the event row.
        module_key (CharField): Product surface where the action occurred.
        action_type (CharField): Specific action identifier.
        severity (CharField): Normalized severity (info, warning, critical).
        status (CharField): Outcome label such as success or failed.
        actor_type (CharField): Distinguishes user-triggered versus system events.
        actor_user (ForeignKey): Optional reference to the acting user account.
        actor_label (CharField): Human-readable fallback for system/service actors.
        entity_type (CharField): Model name or logical entity category affected.
        entity_id (CharField): Identifier of the entity, stored as text.
        entity_label (CharField): Friendly label rendered in downstream UIs.
        summary (TextField): Short sentence that explains what happened.
        before_data (JSONField): Lightweight snapshot before the action.
        diff_data (JSONField): Summary of changes or a diff for large objects.
        metadata (JSONField): Arbitrary context such as IP, job ids, or notes.
        event_at (DateTimeField): Canonical timestamp for the action.
        is_locked (BooleanField): Guards the append-only guarantee.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Classification
    module_key = models.CharField(
        max_length=40,
        choices=AuditModuleKey.choices,
        db_index=True,
    )
    action_type = models.CharField(
        max_length=80,
        choices=AuditActionType.choices,
        db_index=True,
    )
    severity = models.CharField(
        max_length=20,
        choices=AuditSeverity.choices,
        default=AuditSeverity.INFO,
        db_index=True,
    )
    status = models.CharField(
        max_length=20,
        choices=AuditStatus.choices,
        default=AuditStatus.SUCCESS,
        db_index=True,
    )

    # Actor attribution
    actor_type = models.CharField(
        max_length=20,
        choices=AuditActorType.choices,
        default=AuditActorType.USER,
        db_index=True,
    )
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performed_audit_events",
    )

    # Target / entity trail support
    entity_type = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Examples: UserAccount, School, ImportJob, Invoice, RoleTemplate"
    )
    entity_id = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Stored as string so UUID/int/external refs can all fit."
    )
    entity_label = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Human-readable target label."
    )

    # Human-readable summary
    summary = models.TextField(
        blank=True,
        null=True,
        help_text="Short explanation of what happened."
    )

    # Change snapshots
    before_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="State before the action. Keep lightweight and safe."
    )
    diff_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="State diff or change summary. Useful for large objects where before/after would be too heavy."
    )

    # Extra metadata (IP, user agent, job ids, references, etc.)
    metadata = models.JSONField(default=dict, blank=True, null=True)

    # Canonical event time
    event_at = models.DateTimeField(default=timezone.now, db_index=True)

    # Immutability flag
    is_locked = models.BooleanField(
        default=True,
        editable=False,
        help_text="Audit rows are append-only and must not be edited/deleted normally."
    )

    class Meta:
        ordering = ["-event_at"]
        indexes = [
            models.Index(fields=["module_key", "action_type", "event_at"]),
            models.Index(fields=["entity_type", "entity_id", "event_at"]),
            models.Index(fields=["actor_type", "actor_user", "event_at"]),
            models.Index(fields=["severity", "status", "event_at"]),
        ]

    def __str__(self) -> str:
        """Return a concise identifier for admin screens and logs."""
        return f"{self.action_type} on {self.entity_type}:{self.entity_id}"

    def clean(self):
        """Run model-level validation to guarantee required context."""
        # If actor type is USER, try to enforce presence of actor_user
        if self.actor_type == AuditActorType.USER and not self.actor_user:
            raise ValidationError("User-attributed audit events require actor_user or actor_label.")

        # Global event can have school=None, but school-scoped events usually should not
        if not self.entity_type:
            raise ValidationError("entity_type is required.")
        if not self.entity_id:
            raise ValidationError("entity_id is required.")

    def save(self, *args, **kwargs):
        """
        Append-only behavior:
        once created, normal updates are blocked.
        """
        if self.pk and AuditEvent.objects.filter(pk=self.pk).exists():
            raise ValidationError("AuditEvent is immutable and cannot be updated.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Disallow deletes to preserve the integrity of audit trails."""
        raise ValidationError("AuditEvent cannot be deleted because audit logs are append-only.")


# -----------------------------------------------------------------------------
# Per-entity trail summary
# -----------------------------------------------------------------------------

class EntityAuditTrail(models.Model):
    """Cached rollup that accelerates per-entity audit trail lookups.

    Attributes:
        id (UUIDField): Primary key for the rollup row.
        entity_type (CharField): Model or logical type of the tracked entity.
        entity_id (CharField): Identifier of the tracked entity stored as text.
        entity_label (CharField): Friendly label for UI displays.
        event_count (PositiveIntegerField): Number of recorded audit events.
        first_event_at (DateTimeField): Timestamp of the earliest event.
        last_event_at (DateTimeField): Timestamp of the most recent event.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    entity_type = models.CharField(max_length=100, db_index=True)
    entity_id = models.CharField(max_length=100, db_index=True)
    entity_label = models.CharField(max_length=255, blank=True)

    event_count = models.PositiveIntegerField(default=0)
    first_event_at = models.DateTimeField(null=True, blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("entity_type", "entity_id")
        indexes = [
            models.Index(fields=["entity_type", "entity_id"]),
            models.Index(fields=["last_event_at"]),
        ]

    def __str__(self) -> str:
        """Represent the summarized trail for admin list displays."""
        return f"Trail<{self.entity_type}:{self.entity_id}>"

    def register_event(self, event: AuditEvent) -> None:
        """
        Convenience helper for services/signals.
        """
        self.event_count += 1
        if not self.first_event_at or event.event_at < self.first_event_at:
            self.first_event_at = event.event_at
        if not self.last_event_at or event.event_at > self.last_event_at:
            self.last_event_at = event.event_at
        self.save(update_fields=["event_count", "first_event_at", "last_event_at"])


# -----------------------------------------------------------------------------
# Audit export tracking
# -----------------------------------------------------------------------------

class AuditExportJob(models.Model):
    """Persists background export jobs so audit logs can be downloaded later.

    Attributes:
        id (UUIDField): Primary key for the export job.
        requested_by (ForeignKey): User who initiated the export.
        export_format (CharField): File format requested (e.g., CSV).
        status (CharField): Lifecycle state such as pending, running, or failed.
        filter_payload (JSONField): Serialized filters applied to the export.
        file_name (CharField): User-facing download filename.
        file_path (CharField): Storage location of the exported artifact.
        row_count (PositiveIntegerField): Number of rows written.
        failure_reason (TextField): Diagnostic details if the export fails.
        requested_at (DateTimeField): When the export was queued.
        started_at (DateTimeField): When asynchronous processing began.
        completed_at (DateTimeField): When processing finished successfully.
        expires_at (DateTimeField): Deadline after which downloads should expire.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_audit_exports",
    )

    export_format = models.CharField(
        max_length=10,
        choices=ExportFormat.choices,
        default=ExportFormat.CSV,
    )
    status = models.CharField(
        max_length=20,
        choices=ExportJobStatus.choices,
        default=ExportJobStatus.PENDING,
        db_index=True,
    )

    # Snapshot of filters used for export
    filter_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Stores search/date/action/entity filters used for this export."
    )

    file_name = models.CharField(max_length=255, blank=True)
    file_path = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path/object key in storage."
    )

    row_count = models.PositiveIntegerField(default=0)
    failure_reason = models.TextField(blank=True)

    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self) -> str:
        """Return the identifier and state for admin readability."""
        return f"AuditExportJob<{self.id}> - {self.status}"

    def mark_running(self):
        """Transition the job into the running state and stamp start time."""
        self.status = ExportJobStatus.RUNNING
        self.started_at = timezone.now()
        self.save(update_fields=["status", "started_at"])

    def mark_completed(self, *, row_count: int = 0, file_name: str = "", file_path: str = "", expires_in_days: int):
        """Mark the job as completed, persist file metadata, and set expiry."""
        self.status = ExportJobStatus.COMPLETED
        self.row_count = row_count
        self.file_name = file_name
        self.file_path = file_path
        self.completed_at = timezone.now()
        self.expires_at = timezone.now() + timedelta(days=expires_in_days) if expires_in_days > 0 else None

        self.save(
            update_fields=[
                "status",
                "row_count",
                "file_name",
                "file_path",
                "completed_at",
                "expires_at",
            ]
        )

    def mark_failed(self, reason: str):
        """Capture failure state and reason for later troubleshooting."""
        self.status = ExportJobStatus.FAILED
        self.failure_reason = reason
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "failure_reason", "completed_at"])

    @property
    def is_expired(self) -> bool:
        """Return True when the exported artifact is past its expiry window."""
        return bool(self.expires_at and timezone.now() >= self.expires_at)


# -----------------------------------------------------------------------------
# Compliance rules
# -----------------------------------------------------------------------------

class ComplianceRule(TimeStampedModel):
    """Configurable policy applied to audit events for compliance automation.

    Attributes:
        id (UUIDField): Primary key for the rule.
        name (CharField): Human-readable identifier shown in admin tools.
        description (TextField): Optional longer explanation of the rule.
        rule_type (CharField): Category such as retention, masking, or export.
        school (ForeignKey): Tenant that owns the rule, if any.
        module_key (CharField): Optional module filter that narrows scope.
        action_type (CharField): Optional action filter that narrows scope.
        is_active (BooleanField): Indicates whether enforcement is enabled.
        retention_days (PositiveIntegerField): Required duration for retention rules.
        masking_fields (JSONField): Field paths to mask in stored payloads.
        config (JSONField): Arbitrary structured settings for custom logic.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)

    rule_type = models.CharField(
        max_length=20,
        choices=ComplianceRuleType.choices,
        db_index=True,
    )

    # Scope
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="audit_compliance_rules",
        help_text="Null means global rule."
    )
    module_key = models.CharField(
        max_length=40,
        choices=AuditModuleKey.choices,
        blank=True,
        help_text="Optional: restrict rule to one module."
    )
    action_type = models.CharField(
        max_length=80,
        blank=True,
        help_text="Optional: restrict rule to one action."
    )

    is_active = models.BooleanField(default=True)

    # Flexible policy data
    retention_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Used mainly for retention rules."
    )
    masking_fields = models.JSONField(
        default=list,
        blank=True,
        help_text="Field names to mask in before/after/metadata/export."
    )
    config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extra policy configuration."
    )

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["rule_type", "is_active"]),
            models.Index(fields=["school", "rule_type"]),
        ]

    def __str__(self) -> str:
        """Use the rule name whenever the object is cast to a string."""
        return self.name

    def clean(self):
        """Ensure required policy attributes are present before saving."""
        if self.rule_type == ComplianceRuleType.RETENTION and not self.retention_days:
            raise ValidationError("Retention rules require retention_days.")

    def applies_to_event(self, event: AuditEvent) -> bool:
        """Return True if the rule scope matches the provided audit event."""
        if not self.is_active:
            return False

        if self.school_id and event.school_id != self.school_id:
            return False

        if self.module_key and event.module_key != self.module_key:
            return False

        if self.action_type and event.action_type != self.action_type:
            return False

        return True
