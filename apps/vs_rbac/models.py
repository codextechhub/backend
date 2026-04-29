from __future__ import annotations

import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from vs_schools.models import Branch, School

User = settings.AUTH_USER_MODEL


def _unique_slug(model_class, name, slug_field="slug", exclude_pk=None):
    base = slugify(name)
    slug = base
    n = 1
    while True:
        qs = model_class.objects.filter(**{slug_field: slug})
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        if not qs.exists():
            return slug
        slug = f"{base}-{n}"
        n += 1


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

    # If True, schooles cannot grant this directly; must go through approval workflow (RoleChangeRequest)
    is_restricted = models.BooleanField(default=False)

    # Optional: for more advanced policy/UX; safe to keep lightweight
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
        constraints = [
            models.UniqueConstraint(
                fields=["permission", "depends_on"],
                name="uq_permission_dependency",
            )
        ]

    def __str__(self) -> str:
        return f"{self.permission_id} depends on {self.depends_on_id}"


# -----------------------------------------------------------------------------
# Permission Groups (shared — attachable to both school and platform roles)
# -----------------------------------------------------------------------------
class PermissionGroup(TimeStampedModel):
    """Named, reusable bundle of permissions.

    Groups are containers only — they grant nothing on their own. Role
    templates (school and platform) can attach one or more groups and the
    runtime evaluator flattens group permissions into the effective set.

    Attributes:
        name: Human-readable group label (case-insensitive unique).
        description: Purpose and intended audience for the group.
        is_system: True for Vision-seeded groups; False for custom groups.
        is_active: Soft-delete / hide toggle.
        permissions: M2M to ``Permission`` via ``GroupPermission``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)

    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    permissions = models.ManyToManyField(
        Permission,
        through="GroupPermission",
        related_name="groups",
        blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["is_active"]),
        ]

    def __str__(self) -> str:
        return self.name


class GroupPermission(TimeStampedModel):
    """Join table placing a ``Permission`` inside a ``PermissionGroup``."""

    group = models.ForeignKey(
        PermissionGroup,
        on_delete=models.CASCADE,
        related_name="group_permissions",
    )
    permission = models.ForeignKey(
        Permission,
        to_field="key",
        db_column="permission_key",
        on_delete=models.CASCADE,
        related_name="group_memberships",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "permission"],
                name="uq_group_permission_once",
            )
        ]
        indexes = [
            models.Index(fields=["group"]),
            models.Index(fields=["permission"]),
        ]

    def __str__(self) -> str:
        return f"{self.group_id}:{self.permission_id}"


# -----------------------------------------------------------------------------
# Suggested Role Templates (platform-owned library)
# -----------------------------------------------------------------------------
class SuggestedRoleTemplate(models.Model):
    """Platform-owned library of pre-built role suggestions.

    These are read-only records seeded by CodeX Vision.
    No institution owns or modifies these directly.
    When an institution selects one, a RoleTemplate is created
    for their institution using this suggestion as the source.
    """

    key = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default='')

    scope = models.CharField(
        max_length=20,
        choices=[
            ('institution', 'Institution-wide'),
            ('branch', 'Branch-scoped'),
            ('class', 'Class-scoped'),
            ('portal', 'Portal only'),
        ]
    )

    tier = models.CharField(
        max_length=1,
        choices=[('A', 'Core'), ('B', 'Module-Dependent'), ('C', 'Optional')],
        default='A'
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['tier', 'name']
        verbose_name = 'Suggested Role Template'
        verbose_name_plural = 'Suggested Role Templates'

    def __str__(self):
        return f'{self.name} ({self.key})'


class SuggestedRolePermission(models.Model):
    """Default permissions attached to a SuggestedRoleTemplate.

    When an institution selects this suggestion, these permissions
    are copied into their RoleTemplate's RolePermission records.
    """
    suggested_role = models.ForeignKey(
        SuggestedRoleTemplate,
        on_delete=models.CASCADE,
        related_name='default_permissions'
    )
    permission = models.ForeignKey(
        'Permission',
        to_field='key',
        db_column='permission_key',
        on_delete=models.CASCADE,
        related_name='suggested_role_defaults'
    )

    class Meta:
        unique_together = [['suggested_role', 'permission']]
        verbose_name = 'Suggested Role Permission'
        verbose_name_plural = 'Suggested Role Permissions'

    def __str__(self):
        return f'{self.suggested_role.key}:{self.permission_id}'


# -----------------------------------------------------------------------------
# Role Templates (school-scoped)
# -----------------------------------------------------------------------------
class RoleTemplate(TimeStampedModel):
    """School-scoped role blueprint owned by a specific school school.

    Attributes:
        school: School that owns the template; acts as tenant boundary.
        name: Human readable label surfaced in admin UIs.
        description: Optional context for auditors and approvers.
        status: Current lifecycle (active/inactive/archived).
        is_system_role: Locks the record to Vision-managed roles.
        is_locked: Prevents school edits while elevated workflows run.
        version: Incremented when permissions change for cache busting.
        created_by: User that created the template, if tracked.
        permissions: Many-to-many relationship via ``RolePermission``.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        ARCHIVED = "ARCHIVED", "Archived"


    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="role_templates",
        blank=True,
        null=True,
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="role_templates",
        blank=True,
        null=True,
    )

    suggested_from = models.ForeignKey(
        'SuggestedRoleTemplate',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_roles',
    )

    id = models.SlugField(max_length=120, primary_key=True, editable=False)
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)

    # System roles are provisioned/owned by Vision; schooles might not be able to edit these.
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

    # Permission groups attached to this role (flattened at runtime)
    groups = models.ManyToManyField(
        "PermissionGroup",
        through="RoleGroup",
        related_name="roles",
        blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["school", "status"]),
            models.Index(fields=["school", "is_locked"]),
        ]

    def __str__(self) -> str:
        return f"{self.school_id}:{self.name}"

    def bump_version(self):
        self.version = (self.version or 1) + 1

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = _unique_slug(RoleTemplate, self.name)
        super().save(*args, **kwargs)


class RolePermission(TimeStampedModel):
    """Join table capturing permission grants on school role templates.

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
        constraints = [
            models.UniqueConstraint(fields=["role", "permission"], name="uq_role_permission_once"),
        ]
        indexes = [
            models.Index(fields=["role", "granted"]),
            models.Index(fields=["permission", "granted"]),
        ]

    def __str__(self) -> str:
        return f"{self.role_id}:{self.permission_id} ({'grant' if self.granted else 'deny'})"


class RoleGroup(TimeStampedModel):
    """Attaches a ``PermissionGroup`` to a school ``RoleTemplate``.

    Permissions from attached groups are unioned with any direct
    ``RolePermission`` grants at runtime. Explicit denies on
    ``RolePermission`` still win over grants derived from groups.
    """

    role = models.ForeignKey(
        RoleTemplate,
        on_delete=models.CASCADE,
        related_name="role_groups",
    )
    group = models.ForeignKey(
        PermissionGroup,
        on_delete=models.CASCADE,
        related_name="role_attachments",
    )
    attached_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attached_role_groups",
    )
    attached_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["role", "group"],
                name="uq_role_group_once",
            )
        ]
        indexes = [
            models.Index(fields=["role"]),
            models.Index(fields=["group"]),
        ]

    def __str__(self) -> str:
        return f"{self.role_id}:{self.group_id}"


# -----------------------------------------------------------------------------
# Assign roles to users (school scoped)
# -----------------------------------------------------------------------------
class UserRoleAssignment(TimeStampedModel):
    """School-scoped assignment of a ``RoleTemplate`` to a specific user.

    Attributes:
        school: School boundary that owns the assignment record.
        user: Actor receiving the permissions.
        role: Template being assigned; must belong to the same school.
        assignment_status: Active vs revoked state machine.
        assigned_by/assigned_at: Metadata on who granted the role and when.
        revoked_by/revoked_at: Metadata on revocation events.
        reason_note: Free-form justification captured for audits.

    Methods:
        clean: Validates school consistency between role and assignment.
        revoke: Helper that stamps revoke metadata in one call.
    """
    class AssignmentStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REVOKED = "REVOKED", "Revoked"


    school = models.ForeignKey(
        School,
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
            models.Index(fields=["school", "user", "assignment_status"]),
            models.Index(fields=["school", "role", "assignment_status"]),
        ]
        constraints = []

    def __str__(self) -> str:
        return f"{self.school_id}:{self.user_id}->{self.role_id} ({self.assignment_status})"

    def clean(self):
        if self.role_id and self.school_id and self.role.school_id != self.school_id:
            raise ValidationError("Role must belong to the same school as the assignment.")

    def revoke(self, by_user=None, reason: str = ""):
        self.assignment_status = self.AssignmentStatus.REVOKED
        self.revoked_at = timezone.now()
        self.revoked_by = by_user
        self.reason_note = reason or self.reason_note


# -----------------------------------------------------------------------------
# Approval workflow: School -> Vision (role changes)
# -----------------------------------------------------------------------------
class RoleChangeRequest(TimeStampedModel):
    """Workflow record for school-to-Vision approval of role edits.

    Attributes:
        school: Tenant requesting the change.
        requested_by: School operator initiating request.
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


    school = models.ForeignKey(
        School,
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
        indexes = [
            models.Index(fields=["school", "status", "submitted_at"]),
            models.Index(fields=["status", "submitted_at"]),
        ]

    def __str__(self) -> str:
        return f"RCR:{self.id} ({self.status})"

    def clean(self):
        # Cross-school safety: target role must belong to same school
        if self.target_role_id and self.school_id and self.target_role.school_id != self.school_id:
            raise ValidationError("Target role must belong to the same school as the request.")
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

    id = models.SlugField(max_length=120, primary_key=True, editable=False)
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

    groups = models.ManyToManyField(
        "PermissionGroup",
        through="PlatformRoleGroup",
        related_name="platform_roles",
        blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["is_locked"]),
        ]

    def __str__(self) -> str:
        return self.name

    def bump_version(self):
        self.version = (self.version or 1) + 1

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = _unique_slug(PlatformRoleTemplate, self.name, slug_field="id")
        super().save(*args, **kwargs)


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


class PlatformRoleGroup(TimeStampedModel):
    """Attaches a ``PermissionGroup`` to a ``PlatformRoleTemplate``."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    role = models.ForeignKey(
        PlatformRoleTemplate,
        on_delete=models.CASCADE,
        related_name="role_groups",
    )
    group = models.ForeignKey(
        PermissionGroup,
        on_delete=models.CASCADE,
        related_name="platform_role_attachments",
    )
    attached_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attached_platform_role_groups",
    )
    attached_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["role", "group"],
                name="uq_platform_role_group_once",
            )
        ]
        indexes = [
            models.Index(fields=["role"]),
            models.Index(fields=["group"]),
        ]

    def __str__(self) -> str:
        return f"{self.role_id}:{self.group_id}"


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
        constraints = []

    def __str__(self) -> str:
        return f"{self.user_id}->{self.role_id} ({self.assignment_status})"

    def revoke(self, by_user=None, reason: str = ""):
        if self.assignment_status == self.AssignmentStatus.REVOKED:
            return self

        self.assignment_status = self.AssignmentStatus.REVOKED
        self.revoked_at = timezone.now()
        self.revoked_by = by_user

        if reason:
            self.reason_note = reason

        return self


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
    """Platform analogue of ``RoleChangeDeltaItem`` tracking requested diffs.

    Attributes:
        request: Parent ``PlatformRoleChangeRequest``.
        permission: Permission key being added or removed.
        operation: ``ADD`` or ``REMOVE`` action stored via ``Operation`` choices.
    """

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
