from __future__ import annotations

import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from django.utils import timezone
from django.utils.text import slugify

from vs_schools.models import Branch, School

from .managers import TenantAwareManager

User = settings.AUTH_USER_MODEL


def _unique_slug(model_class, name, slug_field="id", exclude_pk=None):
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
# Permission vocabulary (Vision-owned, admin-manageable)
# -----------------------------------------------------------------------------

class PermissionModule(TimeStampedModel):
    """Top-level module bucket, e.g. 'finance', 'students'."""

    name = models.SlugField(max_length=64, primary_key=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-updated_at", "name"]

    def __str__(self) -> str:
        return self.name


class PermissionResource(TimeStampedModel):
    """Resource scoped to a module, e.g. 'invoice' under 'finance'."""

    module = models.ForeignKey(
        PermissionModule,
        on_delete=models.CASCADE,
        related_name="resources",
    )
    name = models.SlugField(max_length=64)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [["module", "name"]]
        ordering = ["-updated_at", "module", "name"]

    def __str__(self) -> str:
        return f"{self.module_id}.{self.name}"


class PermissionAction(TimeStampedModel):
    """Reusable action keyword, e.g. 'view', 'create', 'approve'."""

    name = models.SlugField(max_length=64, primary_key=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-updated_at", "name"]

    def __str__(self) -> str:
        return self.name


# -----------------------------------------------------------------------------
# Permission Registry (global, Vision-owned)
# -----------------------------------------------------------------------------
class Permission(TimeStampedModel):
    """Vision-owned registry for reusable permissions.

    The permission key is auto-built as ``module.resource.action`` from the
    three FK references. Example: ``finance.invoice.view``.

    Attributes:
        key: Primary identifier (auto-generated, do not set manually).
        module: FK to PermissionModule (e.g. 'finance').
        resource: FK to PermissionResource (e.g. 'invoice' under 'finance').
        action: FK to PermissionAction (e.g. 'view').
        sensitivity_level: Flagged via ``Sensitivity`` for audit queues.
        is_restricted: Marks permissions that must flow through approvals.
        is_active: Soft-delete / hide toggle.
    """

    class Sensitivity(models.TextChoices):
        NORMAL = "NORMAL", "Normal"
        SENSITIVE = "SENSITIVE", "Sensitive"
        CRITICAL = "CRITICAL", "Critical"

    key = models.CharField(max_length=180, primary_key=True)

    module = models.ForeignKey(
        PermissionModule,
        db_column="module_key",
        db_constraint=False,
        on_delete=models.PROTECT,
        related_name="permissions",
    )
    resource = models.ForeignKey(
        PermissionResource,
        db_column="resource_key",
        db_constraint=False,
        on_delete=models.PROTECT,
        related_name="permissions",
    )
    action = models.ForeignKey(
        PermissionAction,
        db_column="action_key",
        db_constraint=False,
        on_delete=models.PROTECT,
        related_name="permissions",
    )

    description = models.TextField(blank=True)

    sensitivity_level = models.CharField(
        max_length=16,
        choices=Sensitivity.choices,
        default=Sensitivity.NORMAL,
    )

    is_restricted = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["module", "action"]),
            models.Index(fields=["is_restricted", "sensitivity_level"]),
        ]
        ordering = ["-updated_at", "module", "resource", "action"]

    def save(self, *args, **kwargs):
        if not kwargs.get('update_fields'):
            self.key = f"{self.module_id}.{self.resource.name}.{self.action_id}"
        super().save(*args, **kwargs)

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
        ordering = ["-updated_at", "name"]
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
# Prebuilt Role Templates (platform-owned library)
# -----------------------------------------------------------------------------
class PrebuiltRoleTemplate(models.Model):
    """Platform-owned library of pre-built role suggestions.

    These are read-only records seeded by CodeX Vision.
    No institution owns or modifies these directly.
    When an institution selects one, a TenantRoleTemplate is created
    for their tenant using this suggestion as the source.
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
        verbose_name = 'Prebuilt Role Template'
        verbose_name_plural = 'Prebuilt Role Templates'

    def __str__(self):
        return f'{self.name} ({self.key})'


class PrebuiltRolePermission(models.Model):
    """Default permissions attached to a PrebuiltRoleTemplate.

    When an institution selects this suggestion, these permissions
    are copied into their TenantRoleTemplate's TenantRolePermission records.
    """
    prebuilt_role = models.ForeignKey(
        PrebuiltRoleTemplate,
        on_delete=models.CASCADE,
        related_name='default_permissions'
    )
    permission = models.ForeignKey(
        'Permission',
        to_field='key',
        db_column='permission_key',
        on_delete=models.CASCADE,
        related_name='prebuilt_role_defaults'
    )

    class Meta:
        unique_together = [['prebuilt_role', 'permission']]
        verbose_name = 'Prebuilt Role Permission'
        verbose_name_plural = 'Prebuilt Role Permissions'

    def __str__(self):
        return f'{self.prebuilt_role.key}:{self.permission_id}'


# -----------------------------------------------------------------------------
# Unified tenant RBAC (migration target for school + platform role systems)
# -----------------------------------------------------------------------------

class TenantRoleTemplate(TimeStampedModel):
    """Role blueprint owned by one tenant, optionally narrowed to a branch."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        ARCHIVED = "ARCHIVED", "Archived"

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="role_templates",
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="tenant_role_templates",
        null=True, blank=True,
    )
    key = models.SlugField(max_length=120)
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    is_system_role = models.BooleanField(default=False)
    is_locked = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_tenant_roles",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "key"], name="uq_tenant_role_key"),
            models.UniqueConstraint(fields=["tenant", "name"], name="uq_tenant_role_name"),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "branch", "status"]),
        ]

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.school.tenant_id != self.tenant_id:
            raise ValidationError("Role branch must belong to the role tenant.")

    def __str__(self):
        return f"{self.tenant_id}:{self.name}"


class TenantRolePermission(TimeStampedModel):
    role = models.ForeignKey(
        TenantRoleTemplate, on_delete=models.CASCADE, related_name="role_permissions",
    )
    permission = models.ForeignKey(
        Permission, to_field="key", db_column="permission_key",
        on_delete=models.CASCADE, related_name="tenant_role_permissions",
    )
    granted = models.BooleanField(default=True)
    granted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="granted_tenant_role_permissions",
    )
    granted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["role", "permission"], name="uq_tenant_role_permission"),
        ]
        indexes = [
            models.Index(fields=["role", "granted"]),
            models.Index(fields=["permission", "granted"]),
        ]


class TenantRoleGroup(TimeStampedModel):
    role = models.ForeignKey(
        TenantRoleTemplate, on_delete=models.CASCADE, related_name="role_groups",
    )
    group = models.ForeignKey(
        PermissionGroup, on_delete=models.CASCADE, related_name="tenant_role_attachments",
    )
    attached_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="attached_tenant_role_groups",
    )
    attached_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["role", "group"], name="uq_tenant_role_group"),
        ]


class TenantUserRoleAssignment(TimeStampedModel):
    class AssignmentStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REVOKED = "REVOKED", "Revoked"

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="role_assignments",
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="tenant_role_assignments",
        null=True, blank=True,
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="tenant_role_assignments",
    )
    role = models.ForeignKey(
        TenantRoleTemplate, on_delete=models.PROTECT, related_name="user_assignments",
    )
    assignment_status = models.CharField(
        max_length=12, choices=AssignmentStatus.choices, default=AssignmentStatus.ACTIVE,
    )
    assigned_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_tenant_roles",
    )
    assigned_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="revoked_tenant_roles",
    )
    reason_note = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "user", "role"],
                condition=Q(assignment_status="ACTIVE"),
                name="uq_active_tenant_user_role",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "user", "assignment_status"]),
            models.Index(fields=["tenant", "role", "assignment_status"]),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.user_id and self.user.tenant_id != self.tenant_id:
            errors["user"] = "User must belong to the assignment tenant."
        if self.role_id and self.role.tenant_id != self.tenant_id:
            errors["role"] = "Role must belong to the assignment tenant."
        if self.branch_id and self.branch.school.tenant_id != self.tenant_id:
            errors["branch"] = "Branch must belong to the assignment tenant."
        if errors:
            raise ValidationError(errors)

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
# Unified tenant approval workflow: role permission-change requests
# -----------------------------------------------------------------------------
class TenantRoleChangeRequest(TimeStampedModel):
    """Tenant-scoped approval workflow for role permission edits.

    The canonical tenant-scoped role change workflow. The tenant boundary comes
    from ``tenant`` and
    the target role must belong to the same tenant.

    Attributes:
        tenant: Tenant that owns the request.
        requested_by: User initiating the change.
        target_role: ``TenantRoleTemplate`` being modified.
        status: State machine captured via ``Status`` choices.
        justification: Required explanation for the reviewer.
        reviewer/reviewer_notes: Outcome metadata once decided.
        submitted_at/decided_at: Audit timestamps.
        impact_summary: Cached diff to help the reviewer.

    Helper methods:
        mark_denied/mark_approved/mark_apply_failed: status transitions.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DENIED = "DENIED", "Denied"
        APPLY_FAILED = "APPLY_FAILED", "Apply Failed"

    tenant = models.ForeignKey(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="role_change_requests",
    )

    requested_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="tenant_role_change_requests_made",
    )

    target_role = models.ForeignKey(
        TenantRoleTemplate,
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
        related_name="tenant_role_change_requests_reviewed",
    )
    reviewer_notes = models.TextField(blank=True)

    submitted_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)

    impact_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "status", "submitted_at"]),
            models.Index(fields=["status", "submitted_at"]),
        ]

    def __str__(self) -> str:
        return f"TRCR:{self.id} ({self.status})"

    def clean(self):
        # Cross-tenant safety: target role must belong to same tenant.
        if self.target_role_id and self.tenant_id and self.target_role.tenant_id != self.tenant_id:
            raise ValidationError("Target role must belong to the same tenant as the request.")
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


class TenantRoleChangeDeltaItem(TimeStampedModel):
    """Normalized permission diff attached to a ``TenantRoleChangeRequest``.

    Attributes:
        request: Parent ``TenantRoleChangeRequest``.
        permission: Permission key being added or removed.
        operation: ``ADD`` or ``REMOVE`` to describe the action.
    """

    class Operation(models.TextChoices):
        ADD = "ADD", "Add"
        REMOVE = "REMOVE", "Remove"

    request = models.ForeignKey(
        TenantRoleChangeRequest,
        on_delete=models.CASCADE,
        related_name="delta_items",
    )

    permission = models.ForeignKey(
        Permission,
        to_field="key",
        db_column="permission_key",
        on_delete=models.PROTECT,
        related_name="tenant_delta_items",
    )

    operation = models.CharField(max_length=8, choices=Operation.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["request", "permission", "operation"],
                name="uq_tenant_request_permission_operation",
            )
        ]

    def __str__(self) -> str:
        return f"{self.request_id} {self.operation} {self.permission_id}"


# ---------------------------------------------------------------------------
# RBACAuditLog — authoritative, append-only audit for RBAC actions
# ---------------------------------------------------------------------------

class RBACAuditLog(models.Model):
    """Append-only audit log for RBAC actions (B21 hybrid-audit pattern).

    The central ``vs_audit.emit_audit_event`` is best-effort by contract — it
    swallows failures so it can never break business logic. That is the wrong
    durability contract for permission/role changes, which are security
    system-of-record events. This table is written transactionally with the
    action (a write failure rolls the action back too); the central audit
    trail is kept as a best-effort mirror for the platform-wide activity view.

    Immutable: rows can never be updated or deleted through the ORM.
    """

    action_type = models.CharField(max_length=40)
    severity = models.CharField(max_length=16, default="INFO")
    status = models.CharField(max_length=16, default="SUCCESS")

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="rbac_audit_entries",
    )
    # Loose school reference (slug) — survives school deletion, no FK cascade.
    school_id = models.CharField(max_length=80, blank=True, default="")

    entity_type = models.CharField(max_length=80)
    entity_id = models.CharField(max_length=180)
    entity_label = models.CharField(max_length=255, blank=True, default="")

    summary = models.TextField(blank=True, default="")
    before_data = models.JSONField(null=True, blank=True)
    diff_data = models.JSONField(null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["entity_type", "entity_id"]),
            models.Index(fields=["action_type", "created_at"]),
            models.Index(fields=["school_id", "created_at"]),
        ]
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("RBACAuditLog entries are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("RBACAuditLog entries cannot be deleted.")

    def __str__(self) -> str:
        return f"{self.action_type} {self.entity_type}:{self.entity_id} @ {self.created_at}"
