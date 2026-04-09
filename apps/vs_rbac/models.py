from __future__ import annotations

import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from vs_institutions.models import Institution

User = settings.AUTH_USER_MODEL


# -----------------------------------------------------------------------------
# Shared base
# -----------------------------------------------------------------------------
class TimeStampedModel(models.Model):
    """Abstract base that tracks creation and last update timestamps."""

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# -----------------------------------------------------------------------------
# Permission Registry (global, Vision-owned)
# -----------------------------------------------------------------------------
class Permission(TimeStampedModel):
    """Vision-owned registry for reusable permissions.

    Attributes:
        key: Primary identifier written in dotted form (module.section.action).
        module_key: Top-level module bucket so UIs can group permissions.
        action: Operation keyword such as ``view`` or ``approve``.
        description: Optional human summary rendered in management screens.
        sensitivity_level: Flagged via ``Sensitivity`` for audit and review queues.
        is_restricted: Marks permissions that must flow through approvals.
        is_active: Lightweight flag for soft-deleting or hiding items.

    Example keys:
        ``finance.invoice.view``
        ``finance.invoice.approve``
        ``students.profile.update``
    """

    class Sensitivity(models.TextChoices):
        NORMAL = "NORMAL", "Normal"
        SENSITIVE = "SENSITIVE", "Sensitive"
        CRITICAL = "CRITICAL", "Critical"

    key = models.CharField(max_length=180, primary_key=True)
    module_key = models.CharField(max_length=64)
    action = models.CharField(max_length=64)
    description = models.TextField(blank=True)

    sensitivity_level = models.CharField(
        max_length=16,
        choices=Sensitivity.choices,
        default=Sensitivity.NORMAL,
    )

    # If True, institutions cannot grant this directly; must go through approval workflow
    is_restricted = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["module_key", "action"]),
            models.Index(fields=["is_restricted", "sensitivity_level"]),
        ]

    def __str__(self) -> str:
        return self.key


class PermissionDependency(TimeStampedModel):
    """Explicit dependency graph between permissions.

    Example:
        ``finance.invoice.approve`` -> ``finance.invoice.view``
    """
    permission = models.ForeignKey(
        Permission,
        to_field="key",
        db_column="permission_key",
        on_delete=models.CASCADE,
        related_name="dependencies",
    )
    depends_on = models.ForeignKey(
        Permission,
        to_field="key",
        db_column="depends_on_key",
        on_delete=models.CASCADE,
        related_name="required_by",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["permission", "depends_on"],
                name="uq_permission_dependency",
            )
        ]

    def __str__(self) -> str:
        return f"{self.permission_id} depends on {self.depends_on_id}"


# -----------------------------------------------------------------------------
# Role Templates (institution-scoped)
# -----------------------------------------------------------------------------
class RoleTemplate(TimeStampedModel):
    """Institution-scoped role blueprint.

    Attributes:
        institution: Institution that owns the template; acts as tenant boundary.
        name: Human readable label surfaced in admin UIs.
        description: Optional context for auditors and approvers.
        status: Current lifecycle (active/inactive/archived).
        is_system_role: Locks the record to Vision-managed roles.
        is_locked: Prevents institution edits while elevated workflows run.
        version: Incremented when permissions change for cache busting.
        created_by: User that created the template, if tracked.
        permissions: Many-to-many relationship via ``RolePermission``.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        ARCHIVED = "ARCHIVED", "Archived"

    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="role_templates",
    )

    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)

    is_system_role = models.BooleanField(default=False)
    is_locked = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_roles",
    )

    permissions = models.ManyToManyField(
        Permission,
        through="RolePermission",
        related_name="roles",
        blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["institution", "status"]),
            models.Index(fields=["institution", "is_locked"]),
            models.Index(Lower("name"), name="idx_role_name_lower"),
        ]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "institution",
                name="uq_role_name_per_institution_ci",
            )
        ]

    def __str__(self) -> str:
        return f"{self.institution_id}:{self.name}"

    def clean(self):
        if self.status == self.Status.ARCHIVED and self.is_locked is False:
            pass

    def bump_version(self):
        self.version = (self.version or 1) + 1


class RolePermission(TimeStampedModel):
    """Join table capturing permission grants on role templates."""

    role = models.ForeignKey(RoleTemplate, on_delete=models.CASCADE, related_name="role_permissions")
    permission = models.ForeignKey(
        Permission,
        to_field="key",
        db_column="permission_key",
        on_delete=models.CASCADE,
        related_name="role_permissions",
    )

    granted = models.BooleanField(default=True)

    granted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_role_permissions",
    )
    granted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["role", "permission"], name="uq_role_permission_once"),
        ]
        indexes = [
            models.Index(fields=["role", "granted"]),
            models.Index(fields=["permission", "granted"]),
        ]

    def __str__(self) -> str:
        return f"{self.role_id}:{self.permission_id} ({'grant' if self.granted else 'deny'})"


# -----------------------------------------------------------------------------
# Assign roles to users (institution-scoped)
# -----------------------------------------------------------------------------
class UserRoleAssignment(TimeStampedModel):
    """Institution-scoped assignment of a ``RoleTemplate`` to a specific user.

    Attributes:
        institution: Institution boundary that owns the assignment record.
        user: Actor receiving the permissions.
        role: Template being assigned; must belong to the same institution.
        assignment_status: Active vs revoked state machine.
    """
    class AssignmentStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REVOKED = "REVOKED", "Revoked"

    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="role_assignments",
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="role_assignments",
    )

    role = models.ForeignKey(
        RoleTemplate,
        on_delete=models.PROTECT,
        related_name="user_assignments",
    )

    assignment_status = models.CharField(
        max_length=12,
        choices=AssignmentStatus.choices,
        default=AssignmentStatus.ACTIVE,
    )

    assigned_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_roles",
    )
    assigned_at = models.DateTimeField(default=timezone.now)

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revoked_roles",
    )

    reason_note = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["institution", "user", "assignment_status"]),
            models.Index(fields=["institution", "role", "assignment_status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "user", "role"],
                condition=Q(assignment_status="ACTIVE"),
                name="uq_active_assignment_user_role_institution",
            )
        ]

    def __str__(self) -> str:
        return f"{self.institution_id}:{self.user_id}->{self.role_id} ({self.assignment_status})"

    def clean(self):
        if self.role_id and self.institution_id and self.role.institution_id != self.institution_id:
            raise ValidationError("Role must belong to the same institution as the assignment.")

    def revoke(self, by_user=None, reason: str = ""):
        self.assignment_status = self.AssignmentStatus.REVOKED
        self.revoked_at = timezone.now()
        self.revoked_by = by_user
        self.reason_note = reason or self.reason_note


# -----------------------------------------------------------------------------
# Approval workflow: Institution -> Vision (role changes)
# -----------------------------------------------------------------------------
class RoleChangeRequest(TimeStampedModel):
    """Workflow record for institution-to-Vision approval of role edits."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DENIED = "DENIED", "Denied"
        APPLY_FAILED = "APPLY_FAILED", "Apply Failed"

    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="role_change_requests",
    )

    requested_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="role_change_requests_made",
    )

    target_role = models.ForeignKey(
        RoleTemplate,
        on_delete=models.PROTECT,
        related_name="change_requests",
    )

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)

    justification = models.TextField()

    reviewer = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="role_change_requests_reviewed",
    )
    reviewer_notes = models.TextField(blank=True)

    submitted_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)

    impact_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["institution", "status", "submitted_at"]),
            models.Index(fields=["status", "submitted_at"]),
        ]

    def __str__(self) -> str:
        return f"RCR:{self.id} ({self.status})"

    def clean(self):
        if self.target_role_id and self.institution_id and self.target_role.institution_id != self.institution_id:
            raise ValidationError("Target role must belong to the same institution as the request.")
        if not self.justification or not self.justification.strip():
            raise ValidationError("Justification is required.")

    def mark_denied(self, reviewer, notes: str):
        self.status = self.Status.DENIED
        self.reviewer = reviewer
        self.reviewer_notes = notes
        self.decided_at = timezone.now()

    def mark_approved(self, reviewer, notes: str = ""):
        self.status = self.Status.APPROVED
        self.reviewer = reviewer
        self.reviewer_notes = notes
        self.decided_at = timezone.now()

    def mark_apply_failed(self, reviewer, notes: str):
        self.status = self.Status.APPLY_FAILED
        self.reviewer = reviewer
        self.reviewer_notes = notes
        self.decided_at = timezone.now()


class RoleChangeDeltaItem(TimeStampedModel):
    """Atomic permission diffs attached to a RoleChangeRequest."""

    class Operation(models.TextChoices):
        ADD = "ADD", "Add"
        REMOVE = "REMOVE", "Remove"

    request = models.ForeignKey(
        RoleChangeRequest,
        on_delete=models.CASCADE,
        related_name="delta_items",
    )

    permission = models.ForeignKey(
        Permission,
        to_field="key",
        db_column="permission_key",
        on_delete=models.PROTECT,
        related_name="delta_items",
    )

    operation = models.CharField(max_length=8, choices=Operation.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["request", "permission", "operation"],
                name="uq_request_permission_operation",
            )
        ]

    def __str__(self) -> str:
        return f"{self.request_id} {self.operation} {self.permission_id}"


# -----------------------------------------------------------------------------
# Platform Role Template (Vision-owned / global)
# -----------------------------------------------------------------------------
class PlatformRoleTemplate(TimeStampedModel):
    """Global counterpart of ``RoleTemplate`` for Vision internal teams."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        ARCHIVED = "ARCHIVED", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )

    is_system_role = models.BooleanField(default=True)
    is_locked = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_platform_roles",
    )

    permissions = models.ManyToManyField(
        Permission,
        through="PlatformRolePermission",
        related_name="platform_roles",
        blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["is_locked"]),
            models.Index(Lower("name"), name="idx_platform_role_name_lower"),
        ]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                name="uq_platform_role_name_ci",
            )
        ]

    def __str__(self) -> str:
        return self.name

    def bump_version(self):
        self.version = (self.version or 1) + 1


class PlatformRolePermission(TimeStampedModel):
    """Permission grant records attached to ``PlatformRoleTemplate``."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    role = models.ForeignKey(
        PlatformRoleTemplate,
        on_delete=models.CASCADE,
        related_name="role_permissions",
    )

    permission = models.ForeignKey(
        Permission,
        to_field="key",
        db_column="permission_key",
        on_delete=models.CASCADE,
        related_name="platform_role_permissions",
    )

    granted = models.BooleanField(default=True)

    granted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_platform_role_permissions",
    )

    granted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["role", "permission"],
                name="uq_platform_role_permission_once",
            )
        ]
        indexes = [
            models.Index(fields=["role", "granted"]),
            models.Index(fields=["permission", "granted"]),
        ]

    def __str__(self) -> str:
        return f"{self.role_id}:{self.permission_id} ({'grant' if self.granted else 'deny'})"


# -----------------------------------------------------------------------------
# Assign platform roles to Vision/internal users
# -----------------------------------------------------------------------------
class PlatformUserRoleAssignment(TimeStampedModel):
    """Vision-internal record that maps staff to platform role templates."""

    class AssignmentStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REVOKED = "REVOKED", "Revoked"

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="platform_role_assignments",
    )

    role = models.ForeignKey(
        PlatformRoleTemplate,
        on_delete=models.PROTECT,
        related_name="user_assignments",
    )

    assignment_status = models.CharField(
        max_length=12,
        choices=AssignmentStatus.choices,
        default=AssignmentStatus.ACTIVE,
    )

    assigned_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_platform_roles",
    )

    assigned_at = models.DateTimeField(default=timezone.now)

    revoked_at = models.DateTimeField(null=True, blank=True)

    revoked_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revoked_platform_roles",
    )

    reason_note = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "assignment_status"]),
            models.Index(fields=["role", "assignment_status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role"],
                condition=Q(assignment_status="ACTIVE"),
                name="uq_active_platform_assignment_user_role",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}->{self.role_id} ({self.assignment_status})"

    def revoke(self, by_user=None, reason: str = ""):
        self.assignment_status = self.AssignmentStatus.REVOKED
        self.revoked_at = timezone.now()
        self.revoked_by = by_user
        self.reason_note = reason or self.reason_note


# -----------------------------------------------------------------------------
# Platform approval workflow
# -----------------------------------------------------------------------------
class PlatformRoleChangeRequest(TimeStampedModel):
    """Approval workflow for restricted edits to platform role templates."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DENIED = "DENIED", "Denied"
        APPLY_FAILED = "APPLY_FAILED", "Apply Failed"

    requested_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="platform_role_change_requests_made",
    )

    target_role = models.ForeignKey(
        PlatformRoleTemplate,
        on_delete=models.PROTECT,
        related_name="change_requests",
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )

    justification = models.TextField()

    reviewer = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="platform_role_change_requests_reviewed",
    )

    reviewer_notes = models.TextField(blank=True)

    submitted_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)

    impact_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "submitted_at"]),
        ]

    def __str__(self) -> str:
        return f"PRCR:{self.id} ({self.status})"

    def clean(self):
        if not self.justification or not self.justification.strip():
            raise ValidationError("Justification is required.")

    def mark_denied(self, reviewer, notes: str):
        self.status = self.Status.DENIED
        self.reviewer = reviewer
        self.reviewer_notes = notes
        self.decided_at = timezone.now()

    def mark_approved(self, reviewer, notes: str = ""):
        self.status = self.Status.APPROVED
        self.reviewer = reviewer
        self.reviewer_notes = notes
        self.decided_at = timezone.now()

    def mark_apply_failed(self, reviewer, notes: str):
        self.status = self.Status.APPLY_FAILED
        self.reviewer = reviewer
        self.reviewer_notes = notes
        self.decided_at = timezone.now()


class PlatformRoleChangeDeltaItem(TimeStampedModel):
    """Platform analogue of ``RoleChangeDeltaItem``."""

    class Operation(models.TextChoices):
        ADD = "ADD", "Add"
        REMOVE = "REMOVE", "Remove"

    request = models.ForeignKey(
        PlatformRoleChangeRequest,
        on_delete=models.CASCADE,
        related_name="delta_items",
    )

    permission = models.ForeignKey(
        Permission,
        to_field="key",
        db_column="permission_key",
        on_delete=models.PROTECT,
        related_name="platform_delta_items",
    )

    operation = models.CharField(max_length=8, choices=Operation.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["request", "permission", "operation"],
                name="uq_platform_request_permission_operation",
            )
        ]

    def __str__(self) -> str:
        return f"{self.request_id} {self.operation} {self.permission_id}"
