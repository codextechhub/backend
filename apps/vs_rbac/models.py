from __future__ import annotations

import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from vs_institutions.models import Branch

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
    module_key = models.CharField(max_length=64)     # e.g. "finance", "students"
    action = models.CharField(max_length=64)         # e.g. "view", "create", "approve", "export"
    description = models.TextField(blank=True)

    sensitivity_level = models.CharField(
        max_length=16,
        choices=Sensitivity.choices,
        default=Sensitivity.NORMAL,
    )

    # If True, branches cannot grant this directly; must go through approval workflow (RoleChangeRequest)
    is_restricted = models.BooleanField(default=False)

    # Optional: for more advanced policy/UX; safe to keep lightweight
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "rbac_permission"
        indexes = [
            models.Index(fields=["module_key", "action"]),
            models.Index(fields=["is_restricted", "sensitivity_level"]),
        ]

    def __str__(self) -> str:
        return self.key


class PermissionDependency(TimeStampedModel):
    """Explicit dependency graph between permissions.

    Attributes:
        permission: Permission that requires another capability before use.
        depends_on: Permission that must already be granted.

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
        db_table = "rbac_permission_dependency"
        constraints = [
            models.UniqueConstraint(
                fields=["permission", "depends_on"],
                name="uq_permission_dependency",
            )
        ]

    def __str__(self) -> str:
        return f"{self.permission_id} depends on {self.depends_on_id}"


# -----------------------------------------------------------------------------
# Role Templates (branch-scoped)
# -----------------------------------------------------------------------------
class RoleTemplate(TimeStampedModel):
    """Branch-scoped role blueprint owned by a specific institution branch.

    Attributes:
        branch: Branch that owns the template; acts as tenant boundary.
        name: Human readable label surfaced in admin UIs.
        description: Optional context for auditors and approvers.
        status: Current lifecycle (active/inactive/archived).
        is_system_role: Locks the record to Vision-managed roles.
        is_locked: Prevents branch edits while elevated workflows run.
        version: Incremented when permissions change for cache busting.
        created_by: User that created the template, if tracked.
        permissions: Many-to-many relationship via ``RolePermission``.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        ARCHIVED = "ARCHIVED", "Archived"


    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="role_templates",
    )

    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)

    # System roles are provisioned/owned by Vision; branches might not be able to edit these.
    is_system_role = models.BooleanField(default=False)

    # Locked means "read-only except elevated actors"
    is_locked = models.BooleanField(default=False)

    # Incremented on each successful permission update (useful for cache keys)
    version = models.PositiveIntegerField(default=1)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_roles",
    )

    # Many-to-many through RolePermission for extra metadata
    permissions = models.ManyToManyField(
        Permission,
        through="RolePermission",
        related_name="roles",
        blank=True,
    )

    class Meta:
        db_table = "rbac_role_template"
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["branch", "is_locked"]),
            models.Index(Lower("name"), name="idx_role_name_lower"),
        ]
        constraints = [
            # role names unique per branch (case-insensitive)
            models.UniqueConstraint(
                Lower("name"),
                "branch",
                name="uq_role_name_per_branch_ci",
            )
        ]

    def __str__(self) -> str:
        return f"{self.branch_id}:{self.name}"

    def clean(self):
        # Safety: archived roles should not be locked/unlocked by mistake (policy choice)
        if self.status == self.Status.ARCHIVED and self.is_locked is False:
            # Not strictly required; remove if you don't want this rule
            pass

    def bump_version(self):
        self.version = (self.version or 1) + 1


class RolePermission(TimeStampedModel):
    """Join table capturing permission grants on branch role templates.

    Attributes:
        role: ``RoleTemplate`` receiving the grant or deny record.
        permission: ``Permission`` key linked through ``permission_key`` column.
        granted: Boolean flag so future explicit denies can be represented.
        granted_by: (Optional) actor who made the last change.
        granted_at: Timestamp of the latest update for audit trails.
    """

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
        db_table = "rbac_role_permission"
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
# Assign roles to users (branch scoped)
# -----------------------------------------------------------------------------
class UserRoleAssignment(TimeStampedModel):
    """Branch-scoped assignment of a ``RoleTemplate`` to a specific user.

    Attributes:
        branch: Branch boundary that owns the assignment record.
        user: Actor receiving the permissions.
        role: Template being assigned; must belong to the same branch.
        assignment_status: Active vs revoked state machine.
        assigned_by/assigned_at: Metadata on who granted the role and when.
        revoked_by/revoked_at: Metadata on revocation events.
        reason_note: Free-form justification captured for audits.

    Methods:
        clean: Validates branch consistency between role and assignment.
        revoke: Helper that stamps revoke metadata in one call.
    """
    class AssignmentStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REVOKED = "REVOKED", "Revoked"


    branch = models.ForeignKey(
        Branch,
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
        db_table = "rbac_user_role_assignment"
        indexes = [
            models.Index(fields=["branch", "user", "assignment_status"]),
            models.Index(fields=["branch", "role", "assignment_status"]),
        ]
        constraints = [
            # Prevent duplicate active assignment of same role to same user in same branch
            models.UniqueConstraint(
                fields=["branch", "user", "role"],
                condition=Q(assignment_status="ACTIVE"),
                name="uq_active_assignment_user_role_branch",
            )
        ]

    def __str__(self) -> str:
        return f"{self.branch_id}:{self.user_id}->{self.role_id} ({self.assignment_status})"

    def clean(self):
        # Cross-branch safety: role must belong to the same branch
        if self.role_id and self.branch_id and self.role.branch_id != self.branch_id:
            raise ValidationError("Role must belong to the same branch as the assignment.")
        # You can add a similar check for user.branch if your UserAccount has branch FK.

    def revoke(self, by_user=None, reason: str = ""):
        self.assignment_status = self.AssignmentStatus.REVOKED
        self.revoked_at = timezone.now()
        self.revoked_by = by_user
        self.reason_note = reason or self.reason_note


# -----------------------------------------------------------------------------
# Approval workflow: Branch -> Vision (role changes)
# -----------------------------------------------------------------------------
class RoleChangeRequest(TimeStampedModel):
    """Workflow record for branch-to-Vision approval of role edits.

    Attributes:
        branch: Tenant requesting the change.
        requested_by: Branch operator initiating request.
        target_role: ``RoleTemplate`` being modified.
        status: State machine captured via ``Status`` choices.
        justification: Required explanation for Vision reviewers.
        reviewer/reviewer_notes: Outcome metadata once decided.
        submitted_at/decided_at: Audit timestamps.
        impact_summary: Cached diff to help reviewers.

    Helper methods:
        mark_denied/mark_approved/mark_apply_failed: Convenience status transitions.
    """
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DENIED = "DENIED", "Denied"
        APPLY_FAILED = "APPLY_FAILED", "Apply Failed"


    branch = models.ForeignKey(
        Branch,
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

    # Optional: store derived info so reviewers don’t have to recompute
    impact_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "rbac_role_change_request"
        indexes = [
            models.Index(fields=["branch", "status", "submitted_at"]),
            models.Index(fields=["status", "submitted_at"]),
        ]

    def __str__(self) -> str:
        return f"RCR:{self.id} ({self.status})"

    def clean(self):
        # Cross-branch safety: target role must belong to same branch
        if self.target_role_id and self.branch_id and self.target_role.branch_id != self.branch_id:
            raise ValidationError("Target role must belong to the same branch as the request.")
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
    """Normalized list of atomic permission diffs attached to a request.

    Attributes:
        request: Parent ``RoleChangeRequest``.
        permission: Permission key being added or removed.
        operation: ``ADD`` or ``REMOVE`` to describe the action.
    """

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
        db_table = "rbac_role_change_delta_item"
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
    """Global counterpart of ``RoleTemplate`` for Vision internal teams.

    Attributes:
        id: UUID primary key to avoid collisions across regions.
        name/description: Human context for auditors and tooling.
        status: Lifecycle control to archive or pause templates.
        is_system_role: Marks templates that only core platform may edit.
        is_locked: Prevents edits outside elevated workflows.
        version: Incremented when permissions change to invalidate caches.
        created_by: Platform user who authored the template.
        permissions: Many-to-many via ``PlatformRolePermission``.

    Examples:
        Vision Super Admin, Support Officer, Compliance Reviewer, etc.
    """

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

    # System-owned means only top-level platform actors should edit it
    is_system_role = models.BooleanField(default=True)

    # Locked means read-only except very elevated actors
    is_locked = models.BooleanField(default=False)

    # Version bump whenever permissions change
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
        db_table = "platform_rbac_role_template"
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


# -----------------------------------------------------------------------------
# Platform Role <-> Permission mapping
# -----------------------------------------------------------------------------
class PlatformRolePermission(TimeStampedModel):
    """Permission grant records attached to ``PlatformRoleTemplate`` entries.

    Attributes:
        id: UUID for immutable audit references.
        role: Platform role receiving the grant/deny.
        permission: Global ``Permission`` being referenced.
        granted: Allows eventual explicit deny semantics if required.
        granted_by/granted_at: Capture actor context for compliance teams.
    """

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
        db_table = "platform_rbac_role_permission"
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
    """Vision-internal record that maps staff to platform role templates.

    Attributes:
        user: Internal account receiving privileges.
        role: ``PlatformRoleTemplate`` granted to the user.
        assignment_status: Active or revoked state.
        assigned_by/assigned_at: Audit data for the grant event.
        revoked_by/revoked_at: Audit data for the revoke event.
        reason_note: Optional justification for grant or revoke.

    Methods:
        revoke: Helper to flip status and stamp metadata atomically.
    """
    class AssignmentStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REVOKED = "REVOKED", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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
        db_table = "platform_rbac_user_role_assignment"
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
# Platform approval workflow for restricted permission changes
# -----------------------------------------------------------------------------
class PlatformRoleChangeRequest(TimeStampedModel):
    """Approval workflow for restricted edits to platform role templates.

    Attributes:
        requested_by: Vision staff member initiating the request.
        target_role: ``PlatformRoleTemplate`` slated for changes.
        status: Current lifecycle using ``Status`` choices.
        justification: Required rationale for auditability.
        reviewer/reviewer_notes: Outcome metadata.
        submitted_at/decided_at: Lifecycle timestamps.
        impact_summary: Cached diff for quick reviewer context.
    """
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DENIED = "DENIED", "Denied"
        APPLY_FAILED = "APPLY_FAILED", "Apply Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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
        db_table = "platform_rbac_role_change_request"
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
    """Platform analogue of ``RoleChangeDeltaItem`` tracking requested diffs.

    Attributes:
        request: Parent ``PlatformRoleChangeRequest``.
        permission: Permission key being added or removed.
        operation: ``ADD`` or ``REMOVE`` action stored via ``Operation`` choices.
    """

    class Operation(models.TextChoices):
        ADD = "ADD", "Add"
        REMOVE = "REMOVE", "Remove"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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
        db_table = "platform_rbac_role_change_delta_item"
        constraints = [
            models.UniqueConstraint(
                fields=["request", "permission", "operation"],
                name="uq_platform_request_permission_operation",
            )
        ]

    def __str__(self) -> str:
        return f"{self.request_id} {self.operation} {self.permission_id}"
