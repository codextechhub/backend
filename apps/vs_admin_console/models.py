# backend/apps/admin_console/models.py
from __future__ import annotations

import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


# -----------------------------------------------------------------------------
# Shared base
# -----------------------------------------------------------------------------

class TimeStampedModel(models.Model):
    """Common created/updated timestamps."""
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ImmutableCreateModel(TimeStampedModel):
    """
    Append-only / immutable after creation.
    Useful for audit logs and security trails.
    """
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError(f"{self.__class__.__name__} is immutable once created.")
        return super().save(*args, **kwargs)


def default_expires_in(minutes: int = 30):
    return timezone.now() + timedelta(minutes=minutes)


# -----------------------------------------------------------------------------
# Cross-module references (keep them as strings so apps can be rearranged)
# -----------------------------------------------------------------------------
TENANT_MODEL = getattr(settings, "VISION_TENANT_MODEL", "tenancy.Tenant")  # Module 1
EXTERNAL_USER_MODEL = getattr(settings, "VISION_EXTERNAL_USER_MODEL", None)  # Module 3 (optional)

# Internal staff identity should usually be settings.AUTH_USER_MODEL
STAFF_USER_MODEL = settings.AUTH_USER_MODEL


# -----------------------------------------------------------------------------
# Internal Admin RBAC (console-specific)
# -----------------------------------------------------------------------------

class SensitivityLevel(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class AdminRole(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=80, unique=True)
    description = models.TextField(blank=True)
    is_system_locked = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.name


class AdminPermission(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    key = models.CharField(max_length=120, unique=True)  # e.g. "admin_console.tenant.reset"
    description = models.TextField(blank=True)
    sensitivity_level = models.CharField(
        max_length=16, choices=SensitivityLevel.choices, default=SensitivityLevel.LOW
    )

    def __str__(self) -> str:
        return self.key


class AdminRolePermission(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    admin_role = models.ForeignKey(AdminRole, on_delete=models.CASCADE, related_name="role_permissions")
    admin_permission = models.ForeignKey(AdminPermission, on_delete=models.CASCADE, related_name="permission_roles")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["admin_role", "admin_permission"],
                name="uq_admin_role_permission",
            )
        ]


class AdminUserRoleAssignment(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    admin_user = models.ForeignKey(
        STAFF_USER_MODEL, on_delete=models.CASCADE, related_name="admin_console_role_assignments"
    )
    admin_role = models.ForeignKey(AdminRole, on_delete=models.CASCADE, related_name="user_assignments")
    assigned_by = models.ForeignKey(
        STAFF_USER_MODEL, on_delete=models.PROTECT, related_name="admin_console_role_assignments_given"
    )
    assigned_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["admin_user", "admin_role"],
                name="uq_admin_user_role",
            )
        ]


# -----------------------------------------------------------------------------
# Tenant context + sessions (optional persistence)
# -----------------------------------------------------------------------------

class AdminSession(TimeStampedModel):
    """
    Optional server-side session record for console activity.
    (You can use Django sessions only; this is for ops/audit correlation.)
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    admin_user = models.ForeignKey(
        STAFF_USER_MODEL, on_delete=models.CASCADE, related_name="admin_console_sessions"
    )

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    def clean(self):
        if self.expires_at <= timezone.now():
            raise ValidationError({"expires_at": "expires_at must be in the future."})


class TenantContextSelection(TimeStampedModel):
    """
    Last selected tenant context by a staff user (useful for UX + forensic trails).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    tenant = models.ForeignKey(TENANT_MODEL, on_delete=models.CASCADE, related_name="admin_console_context_selections")
    admin_user = models.ForeignKey(
        STAFF_USER_MODEL, on_delete=models.CASCADE, related_name="admin_console_context_selections"
    )
    selected_at = models.DateTimeField(default=timezone.now, editable=False)
    source = models.CharField(max_length=60, blank=True)  # e.g. "dashboard_search", "deep_link"

    class Meta:
        indexes = [
            models.Index(fields=["admin_user", "-selected_at"]),
            models.Index(fields=["tenant", "-selected_at"]),
        ]


# -----------------------------------------------------------------------------
# Audit (append-only, fail-closed logic sits in services, but storage is here)
# -----------------------------------------------------------------------------

class AdminActionType(models.TextChoices):
    AUTH_LOGIN = "auth_login", "Auth Login"
    TENANT_CREATE = "tenant_create", "Tenant Create"
    TENANT_EDIT = "tenant_edit", "Tenant Edit"
    TENANT_SUSPEND = "tenant_suspend", "Tenant Suspend"
    TENANT_UNSUSPEND = "tenant_unsuspend", "Tenant Unsuspend"
    TENANT_RESET = "tenant_reset", "Tenant Reset"
    PROVISIONING_RETRY = "provisioning_retry", "Provisioning Retry"
    IMPORT_RERUN = "import_rerun", "Import Re-run"
    MANUAL_FIX = "manual_fix", "Manual Fix"
    FLAG_TOGGLE = "flag_toggle", "Feature Flag Toggle"
    IMPERSONATION_START = "impersonation_start", "Impersonation Start"
    IMPERSONATION_END = "impersonation_end", "Impersonation End"
    ROLE_REQUEST_DECISION = "role_request_decision", "Role Request Decision"
    SECURITY_ACTION = "security_action", "Security Action"
    ADMIN_ROLE_CHANGE = "admin_role_change", "Admin Role Change"
    VIEW = "view", "Read View"


class AdminActionLog(ImmutableCreateModel):
    """
    Immutable, append-only log for admin console actions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    actor = models.ForeignKey(
        STAFF_USER_MODEL, on_delete=models.PROTECT, related_name="admin_console_action_logs"
    )
    tenant = models.ForeignKey(
        TENANT_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="admin_console_action_logs"
    )

    action_type = models.CharField(max_length=64, choices=AdminActionType.choices)
    sensitivity_level = models.CharField(
        max_length=16, choices=SensitivityLevel.choices, default=SensitivityLevel.LOW
    )

    payload_summary = models.JSONField(default=dict, blank=True)  # keep this masked/summarized
    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False)

    result_status = models.CharField(max_length=20, default="succeeded")  # succeeded/failed/blocked
    failure_reason = models.TextField(blank=True)

    occurred_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "-occurred_at"]),
            models.Index(fields=["actor", "-occurred_at"]),
            models.Index(fields=["action_type", "-occurred_at"]),
            models.Index(fields=["correlation_id"]),
        ]


class ConfirmationToken(TimeStampedModel):
    """
    Confirmation token for sensitive actions (reset, impersonation, etc).
    This is NOT an auth token; it is an explicit user confirmation artifact.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    actor = models.ForeignKey(
        STAFF_USER_MODEL, on_delete=models.CASCADE, related_name="admin_console_confirmation_tokens"
    )
    tenant = models.ForeignKey(
        TENANT_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="admin_console_confirmation_tokens"
    )

    action_type = models.CharField(max_length=64, choices=AdminActionType.choices)
    issued_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField(default=lambda: default_expires_in(10))
    consumed = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["actor", "action_type", "-issued_at"]),
            models.Index(fields=["tenant", "action_type", "-issued_at"]),
        ]

    def clean(self):
        if self.expires_at <= self.issued_at:
            raise ValidationError({"expires_at": "expires_at must be after issued_at."})

    def consume(self):
        if self.consumed:
            raise ValidationError("Confirmation token already consumed.")
        if timezone.now() >= self.expires_at:
            raise ValidationError("Confirmation token expired.")
        self.consumed = True
        self.save(update_fields=["consumed", "updated_at"])


# -----------------------------------------------------------------------------
# Provisioning monitoring + retries (state is per tenant)
# -----------------------------------------------------------------------------

class PipelineStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class ProvisioningPipelineState(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(
        TENANT_MODEL, on_delete=models.CASCADE, related_name="provisioning_pipeline_state"
    )

    overall_status = models.CharField(max_length=16, choices=PipelineStatus.choices, default=PipelineStatus.QUEUED)
    retries_count = models.PositiveIntegerField(default=0)

    last_error_code = models.CharField(max_length=80, blank=True)
    last_error_message = models.TextField(blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"ProvisioningPipelineState({self.tenant_id}, {self.overall_status})"


class ProvisioningStepState(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    pipeline_state = models.ForeignKey(
        ProvisioningPipelineState, on_delete=models.CASCADE, related_name="steps"
    )

    step_key = models.CharField(max_length=80)  # e.g. "seed_defaults", "create_schema"
    status = models.CharField(max_length=16, choices=PipelineStatus.choices, default=PipelineStatus.QUEUED)

    is_retryable = models.BooleanField(default=False)
    attempt_count = models.PositiveIntegerField(default=0)

    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.TextField(blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["pipeline_state", "step_key"],
                name="uq_pipeline_step_key",
            )
        ]
        indexes = [
            models.Index(fields=["pipeline_state", "status"]),
            models.Index(fields=["step_key"]),
        ]


class ProvisioningRetryRecord(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    tenant = models.ForeignKey(TENANT_MODEL, on_delete=models.CASCADE, related_name="provisioning_retry_records")
    step_state = models.ForeignKey(ProvisioningStepState, on_delete=models.PROTECT, related_name="retry_records")
    actor = models.ForeignKey(STAFF_USER_MODEL, on_delete=models.PROTECT, related_name="provisioning_retry_records")

    reason = models.TextField()
    requested_at = models.DateTimeField(default=timezone.now, editable=False)
    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False)

    result_status = models.CharField(max_length=16, choices=PipelineStatus.choices, default=PipelineStatus.QUEUED)
    result_error = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "-requested_at"]),
            models.Index(fields=["correlation_id"]),
        ]


# -----------------------------------------------------------------------------
# Imports + error visibility + manual remediation
# -----------------------------------------------------------------------------

class ImportJobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELED = "canceled", "Canceled"


class ImportJob(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    tenant = models.ForeignKey(TENANT_MODEL, on_delete=models.CASCADE, related_name="import_jobs")
    dataset_type = models.CharField(max_length=80)  # e.g. "students", "staff", "fees"
    status = models.CharField(max_length=16, choices=ImportJobStatus.choices, default=ImportJobStatus.QUEUED)

    progress_pct = models.PositiveSmallIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)

    created_by = models.ForeignKey(
        STAFF_USER_MODEL, on_delete=models.PROTECT, related_name="created_import_jobs"
    )

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["dataset_type", "status"]),
        ]

    def clean(self):
        if self.progress_pct > 100:
            raise ValidationError({"progress_pct": "progress_pct cannot exceed 100."})


class ImportRowError(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    import_job = models.ForeignKey(ImportJob, on_delete=models.CASCADE, related_name="row_errors")

    row_number = models.PositiveIntegerField()
    field = models.CharField(max_length=120, blank=True)
    error_code = models.CharField(max_length=80)
    message = models.TextField()
    severity = models.CharField(max_length=20, default="error")  # warning/error/critical

    raw_value = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["import_job", "row_number"]),
            models.Index(fields=["error_code"]),
            models.Index(fields=["severity"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["import_job", "row_number", "field", "error_code"],
                name="uq_import_row_error_identity",
            )
        ]


class ManualFixRecord(TimeStampedModel):
    """
    Tracks a manual remediation action with before/after snapshots.
    (Use this to satisfy: cannot apply manual fix without capturing snapshots.)
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    tenant = models.ForeignKey(TENANT_MODEL, on_delete=models.PROTECT, related_name="manual_fix_records")
    actor = models.ForeignKey(STAFF_USER_MODEL, on_delete=models.PROTECT, related_name="manual_fix_records")

    entity_type = models.CharField(max_length=80)  # e.g. "Student", "Staff", "FeeItem"
    entity_id = models.CharField(max_length=120)   # string for flexibility across systems
    fields_changed = models.CharField(max_length=300, blank=True)

    before_snapshot = models.JSONField(default=dict)
    after_snapshot = models.JSONField(default=dict)

    reason = models.TextField()
    source_context = models.CharField(max_length=120, blank=True)  # e.g. import_job_id or ticket_id
    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["correlation_id"]),
        ]

    def clean(self):
        if not self.before_snapshot or not self.after_snapshot:
            raise ValidationError("ManualFixRecord requires both before_snapshot and after_snapshot.")


# -----------------------------------------------------------------------------
# Feature flags (per tenant overrides)
# -----------------------------------------------------------------------------

class FeatureFlagRisk(models.TextChoices):
    SAFE = "safe", "Safe"
    RISKY = "risky", "Risky"
    CRITICAL = "critical", "Critical"


class FeatureFlag(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    flag_key = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    risk_level = models.CharField(max_length=16, choices=FeatureFlagRisk.choices, default=FeatureFlagRisk.SAFE)
    default_value = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.flag_key


class TenantFeatureFlagOverride(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    tenant = models.ForeignKey(TENANT_MODEL, on_delete=models.CASCADE, related_name="feature_flag_overrides")
    feature_flag = models.ForeignKey(FeatureFlag, on_delete=models.CASCADE, related_name="tenant_overrides")

    value = models.BooleanField(default=False)
    changed_by = models.ForeignKey(
        STAFF_USER_MODEL, on_delete=models.PROTECT, related_name="feature_flag_overrides_changed"
    )
    changed_at = models.DateTimeField(default=timezone.now, editable=False)
    change_reason = models.TextField(blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "feature_flag"],
                name="uq_tenant_feature_flag_override",
            )
        ]
        indexes = [
            models.Index(fields=["tenant"]),
            models.Index(fields=["feature_flag"]),
            models.Index(fields=["correlation_id"]),
        ]


# -----------------------------------------------------------------------------
# Impersonation (audited, timeboxed)
# -----------------------------------------------------------------------------

class ImpersonationStatus(models.TextChoices):
    REQUESTED = "requested", "Requested"
    APPROVED = "approved", "Approved"
    DENIED = "denied", "Denied"
    ACTIVE = "active", "Active"
    ENDED = "ended", "Ended"
    EXPIRED = "expired", "Expired"


class ImpersonationReason(models.TextChoices):
    BUG = "bug_reproduction", "Bug Reproduction"
    DATA = "data_verification", "Data Verification"
    WALKTHROUGH = "support_walkthrough", "Support Walkthrough"
    BILLING = "billing_issue", "Billing Issue"
    OTHER = "other", "Other"


class ImpersonationSession(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    tenant = models.ForeignKey(TENANT_MODEL, on_delete=models.PROTECT, related_name="impersonation_sessions")

    staff_actor = models.ForeignKey(
        STAFF_USER_MODEL, on_delete=models.PROTECT, related_name="impersonation_sessions_initiated"
    )

    # If your external users are in another DB/app, keep this as UUID/Char.
    # If you *do* have a Django model for external users, you can swap it by setting VISION_EXTERNAL_USER_MODEL.
    if EXTERNAL_USER_MODEL:
        target_user = models.ForeignKey(EXTERNAL_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    else:
        target_user_id = models.UUIDField()

    reason_category = models.CharField(max_length=32, choices=ImpersonationReason.choices)
    justification = models.TextField()
    ticket_reference = models.CharField(max_length=120, blank=True)

    status = models.CharField(max_length=16, choices=ImpersonationStatus.choices, default=ImpersonationStatus.REQUESTED)

    approved_by = models.ForeignKey(
        STAFF_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="impersonation_sessions_approved"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["correlation_id"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(expires_at__gt=models.F("created_at")),
                name="ck_impersonation_expires_after_created",
            )
        ]

    def clean(self):
        if not self.justification.strip():
            raise ValidationError({"justification": "Justification is required."})
        if self.expires_at <= timezone.now():
            raise ValidationError({"expires_at": "expires_at must be in the future."})

    def mark_active(self):
        now = timezone.now()
        if self.status not in {ImpersonationStatus.APPROVED, ImpersonationStatus.REQUESTED}:
            raise ValidationError("Only approved/requested sessions can be activated.")
        if now >= self.expires_at:
            self.status = ImpersonationStatus.EXPIRED
            self.ended_at = now
            self.save(update_fields=["status", "ended_at", "updated_at"])
            raise ValidationError("Session already expired.")
        self.status = ImpersonationStatus.ACTIVE
        self.started_at = self.started_at or now
        self.save(update_fields=["status", "started_at", "updated_at"])

    def end(self, reason: str = ""):
        now = timezone.now()
        if self.status in {ImpersonationStatus.ENDED, ImpersonationStatus.EXPIRED, ImpersonationStatus.DENIED}:
            return
        self.status = ImpersonationStatus.ENDED if now < self.expires_at else ImpersonationStatus.EXPIRED
        self.ended_at = now
        self.save(update_fields=["status", "ended_at", "updated_at"])


# -----------------------------------------------------------------------------
# Role change requests (surface + decisions)
# -----------------------------------------------------------------------------

class RoleRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DENIED = "denied", "Denied"
    CANCELED = "canceled", "Canceled"


class RoleChangeRequest(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    tenant = models.ForeignKey(TENANT_MODEL, on_delete=models.PROTECT, related_name="role_change_requests")

    # External user identities (do NOT cross tenant boundary in services)
    requester_id = models.UUIDField()
    target_user_id = models.UUIDField()

    requested_role = models.CharField(max_length=120)
    status = models.CharField(max_length=16, choices=RoleRequestStatus.choices, default=RoleRequestStatus.PENDING)

    decided_by = models.ForeignKey(
        STAFF_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="role_change_requests_decided"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "status", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def clean(self):
        if self.status in {RoleRequestStatus.APPROVED, RoleRequestStatus.DENIED} and not self.decided_by_id:
            raise ValidationError("decided_by is required when approving/denying.")
        if self.status == RoleRequestStatus.DENIED and not self.decision_reason.strip():
            raise ValidationError("decision_reason is required when denying.")


# -----------------------------------------------------------------------------
# Security alerts + system health snapshots
# -----------------------------------------------------------------------------

class AlertSeverity(models.TextChoices):
    INFO = "info", "Info"
    WARNING = "warning", "Warning"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class AlertStatus(models.TextChoices):
    OPEN = "open", "Open"
    ACKED = "acked", "Acknowledged"
    RESOLVED = "resolved", "Resolved"


class SecurityAlert(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    tenant = models.ForeignKey(
        TENANT_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="security_alerts"
    )

    alert_type = models.CharField(max_length=80)  # e.g. "boundary_violation", "privilege_escalation_attempt"
    severity = models.CharField(max_length=16, choices=AlertSeverity.choices, default=AlertSeverity.INFO)

    title = models.CharField(max_length=160)
    details = models.TextField(blank=True)

    occurred_at = models.DateTimeField(default=timezone.now, editable=False)
    correlation_id = models.UUIDField(null=True, blank=True)

    status = models.CharField(max_length=16, choices=AlertStatus.choices, default=AlertStatus.OPEN)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "-occurred_at"]),
            models.Index(fields=["severity", "-occurred_at"]),
            models.Index(fields=["status", "-occurred_at"]),
            models.Index(fields=["correlation_id"]),
        ]


class HealthStatus(models.TextChoices):
    OK = "ok", "OK"
    DEGRADED = "degraded", "Degraded"
    DOWN = "down", "Down"
    UNKNOWN = "unknown", "Unknown"


class SystemHealthSnapshot(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    service_key = models.CharField(max_length=80)  # e.g. "audit", "provisioning_queue", "imports"
    status = models.CharField(max_length=16, choices=HealthStatus.choices, default=HealthStatus.UNKNOWN)

    error_rate = models.FloatField(default=0.0)
    queue_depth = models.IntegerField(default=0)

    captured_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=["service_key", "-captured_at"]),
            models.Index(fields=["status", "-captured_at"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(error_rate__gte=0.0), name="ck_health_error_rate_nonnegative"),
        ]
