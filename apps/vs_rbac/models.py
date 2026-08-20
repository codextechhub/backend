from __future__ import annotations

import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from django.utils import timezone
from django.utils.text import slugify

from vs_tenants.models import Branch

from .managers import TenantAwareManager

User = settings.AUTH_USER_MODEL


# -----------------------------------------------------------------------------
# Permission scope: who is allowed to hold a key at all
# -----------------------------------------------------------------------------
class PermissionScope(models.TextChoices):
    """The audience a permission key may ever be granted to.

    This is the boundary the platform's security model rests on, stated as a
    declared field rather than inferred from the key's namespace. A dotted
    prefix is a naming convention: it is not checked anywhere, it cannot be
    queried, and a key that is renamed or seeded under a new module silently
    changes side. ``scope`` says the thing out loud, per key, and every grant
    path reads the same column.

    ``TENANT``
        Any tenant's role may hold it - a school's and the platform's alike.
        The platform tenant is a tenant too: ``xvs_consultant`` is a codex role
        that deliberately holds ``school.*`` view keys, so "tenant-safe" must
        not be read as "forbidden to CX".

    ``PLATFORM``
        Only a role on a ``Tenant.Kind.PLATFORM`` tenant may hold it. These are
        the keys whose surfaces are cross-tenant by construction: impersonation
        tiering, the global permission registry, the schools roster, CX team
        overrides, staff payroll and organogram, the requirements library,
        compliance rule management, and platform health's cross-tenant
        aggregates.

    The two are not the same split as the ``platform.`` / everything-else
    namespaces, and that is the point of storing it. ``platform.team.*`` and
    ``platform.audit.view`` / ``.export`` are ``TENANT``: the first is how a
    school adds its own staff through a tenant-filtered viewset, and the second
    belongs to audit officers working inside a tenant. Enforcing on the prefix
    would have locked both out. See ``seed_platform_permissions`` for the list
    and the evidence behind it.

    There is deliberately no third value. "Tenant-only, never platform" was
    considered and the evidence refutes it: platform roles legitimately hold
    tenant keys today.

    The field has **no default**. An unclassified key (empty scope) is not
    tenant-safe by omission - :func:`assert_tenant_may_hold` refuses it for a
    non-platform tenant and names it in the error, so a seeder that forgets to
    classify a new key fails closed and loudly instead of quietly handing a
    school something nobody decided it could have.
    """

    TENANT = "TENANT", "Tenant (any tenant may hold it)"
    PLATFORM = "PLATFORM", "Platform (CX staff only)"


def platform_only_keys(permission_keys) -> set:
    """Return the subset of *permission_keys* no tenant role may hold.

    Anything that is not explicitly ``TENANT`` counts, so an unclassified key
    is refused rather than assumed safe. One query, whatever the input size.
    """
    keys = {key for key in permission_keys if key}
    if not keys:
        return set()
    return set(
        Permission.objects.filter(key__in=keys)
        .exclude(scope=PermissionScope.TENANT)
        .values_list("key", flat=True)
    )


def tenant_is_platform(tenant) -> bool:
    from vs_tenants.models import Tenant

    return getattr(tenant, "kind", None) == Tenant.Kind.PLATFORM


def assert_tenant_may_hold(permission_keys, tenant, *, field="permission"):
    """Raise unless every key in *permission_keys* may be held inside *tenant*.

    A platform tenant may hold anything. Every other tenant may hold only keys
    declared ``TENANT``. Called from the grant models themselves - not from a
    serializer - so overrides, role permissions, group attachments, prebuilt
    defaults and role assignments are all covered by the same rule.
    """
    if tenant_is_platform(tenant):
        return
    offending = platform_only_keys(permission_keys)
    if not offending:
        return
    listed = ", ".join(sorted(offending))
    raise ValidationError({
        field: (
            f"Permission(s) {listed} are platform-scoped and cannot be granted "
            f"inside a tenant. If a key is missing a scope, classify it in the "
            f"seeder that registers it."
        ),
    })


class ScopeGuardedManager(models.Manager):
    """Manager whose ``bulk_create`` honours the per-row scope guard.

    ``bulk_create`` bypasses ``save()`` and ``clean()`` entirely, and it is how
    the role serializers write permission sets - so without this the model
    guard would be decorative on the exact path an attacker uses.
    """

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        for obj in objs:
            obj.assert_scope_allowed()
        return super().bulk_create(objs, *args, **kwargs)


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
        scope: Who may hold the key at all - see :class:`PermissionScope`.
            Distinct from ``sensitivity_level`` and ``is_restricted``, which
            grade how dangerous a key is *within* an audience; ``scope`` says
            which audience exists in the first place.
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

    # No default, deliberately: see PermissionScope. An unset scope is an
    # unclassified key, and the grant guard refuses it for any tenant that is
    # not the platform.
    scope = models.CharField(
        max_length=16,
        choices=PermissionScope.choices,
        blank=True,
        db_index=True,
        help_text="Who may hold this key: TENANT (any tenant) or PLATFORM (CX only).",
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
# Permission Groups (shared - attachable to both school and platform roles)
# -----------------------------------------------------------------------------
class PermissionGroup(TimeStampedModel):
    """Named, reusable bundle of permissions.

    Groups are containers only - they grant nothing on their own. Role
    templates (school and platform) can attach one or more groups and the
    runtime evaluator flattens group permissions into the effective set.

    Attributes:
        name: Human-readable group label (case-insensitive unique).
        description: Purpose and intended audience for the group.
        scope: Who may hold the bundle - see :class:`PermissionScope`. A group
            is a grant path in its own right (attach it to a role and every key
            inside it lands in the effective set), so it carries the same
            declaration a single permission does. A ``TENANT`` group may only
            contain ``TENANT`` keys; ``GroupPermission`` enforces that, so the
            declaration cannot drift from the contents.
        is_system: True for Vision-seeded groups; False for custom groups.
        is_active: Soft-delete / hide toggle.
        permissions: M2M to ``Permission`` via ``GroupPermission``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)

    # No default, for the same reason Permission.scope has none.
    scope = models.CharField(
        max_length=16,
        choices=PermissionScope.choices,
        blank=True,
        db_index=True,
        help_text="Who may hold this bundle: TENANT (any tenant) or PLATFORM (CX only).",
    )

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

    objects = ScopeGuardedManager()

    def assert_scope_allowed(self):
        """Keep a group's declared scope honest about what it contains.

        A ``TENANT`` group is attachable to any school role, so a platform key
        dropped inside one would travel straight through
        :class:`TenantRoleGroup` into a school's effective set.
        """
        if self.group_id and self.group.scope == PermissionScope.PLATFORM:
            return  # A platform group may carry anything; only CX can attach it.
        if self.permission_id and platform_only_keys([self.permission_id]):
            raise ValidationError({
                "permission": (
                    f"'{self.permission_id}' is platform-scoped and cannot be placed "
                    f"in a tenant-scoped permission group."
                ),
            })

    def clean(self):
        super().clean()
        self.assert_scope_allowed()

    def save(self, *args, **kwargs):
        self.assert_scope_allowed()
        return super().save(*args, **kwargs)

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

    objects = ScopeGuardedManager()

    def assert_scope_allowed(self):
        """Prebuilt roles are tenant blueprints, so their defaults are too.

        Every prebuilt template that exists (``school_admin``, ``branch_admin``,
        ``teacher``) is provisioned into a tenant's own roles, and a default
        attached here is copied into every school that adopts it. There is no
        platform prebuilt role, so a platform key here has no legitimate
        reading - it would be a fleet-wide grant.
        """
        if self.permission_id and platform_only_keys([self.permission_id]):
            raise ValidationError({
                "permission": (
                    f"'{self.permission_id}' is platform-scoped and cannot be a "
                    f"default on a prebuilt tenant role."
                ),
            })

    def clean(self):
        super().clean()
        self.assert_scope_allowed()

    def save(self, *args, **kwargs):
        self.assert_scope_allowed()
        return super().save(*args, **kwargs)

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
        if self.branch_id and self.branch.tenant_id != self.tenant_id:
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

    objects = ScopeGuardedManager()

    def assert_scope_allowed(self):
        """A tenant's role may only carry keys that tenant is allowed to hold.

        An explicit DENY (``granted=False``) is exempt: taking a key away from
        a role is never an escalation, and refusing it would make an existing
        deny row unsaveable.
        """
        if not self.granted or not self.permission_id:
            return
        tenant = getattr(self.role, "tenant", None) if self.role_id else None
        assert_tenant_may_hold([self.permission_id], tenant)

    def clean(self):
        super().clean()
        self.assert_scope_allowed()

    def save(self, *args, **kwargs):
        self.assert_scope_allowed()
        return super().save(*args, **kwargs)


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

    objects = ScopeGuardedManager()

    def assert_scope_allowed(self):
        """Attaching a bundle grants everything in it, so check the contents.

        The group's declared scope is checked *and* its actual members, because
        a group seeded before this field existed could be declared TENANT while
        holding something it should not.
        """
        if not self.group_id or not self.role_id:
            return
        tenant = getattr(self.role, "tenant", None)
        if tenant_is_platform(tenant):
            return
        if self.group.scope != PermissionScope.TENANT:
            raise ValidationError({
                "group": (
                    f"Permission group '{self.group}' is not tenant-scoped and cannot "
                    f"be attached to a role inside a tenant."
                ),
            })
        member_keys = GroupPermission.objects.filter(
            group_id=self.group_id,
        ).values_list("permission_id", flat=True)
        assert_tenant_may_hold(member_keys, tenant, field="group")

    def clean(self):
        super().clean()
        self.assert_scope_allowed()

    def save(self, *args, **kwargs):
        self.assert_scope_allowed()
        return super().save(*args, **kwargs)


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
            # Split in two on purpose. One constraint over (tenant, user, role)
            # made the same role at two sites unstorable, so "Storekeeper at
            # Ikeja" *and* "Storekeeper at Lekki" - the arrangement a single
            # ``User.branch`` cannot express, and the reason branch scope is a
            # set of grants - could not be recorded at all. Splitting keeps both
            # guarantees intact rather than trading one away: at most one active
            # whole-tenant grant of a role per person, and at most one active
            # grant of a role per person per branch.
            #
            # A single constraint including ``branch`` would not do: PostgreSQL
            # treats NULLs as distinct, so it would silently permit duplicate
            # whole-tenant grants that are refused today.
            models.UniqueConstraint(
                fields=["tenant", "user", "role"],
                condition=Q(assignment_status="ACTIVE", branch__isnull=True),
                name="uq_active_tenant_user_role",
            ),
            models.UniqueConstraint(
                fields=["tenant", "user", "role", "branch"],
                condition=Q(assignment_status="ACTIVE", branch__isnull=False),
                name="uq_active_tenant_user_role_branch",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "user", "assignment_status"]),
            models.Index(fields=["tenant", "role", "assignment_status"]),
        ]

    objects = ScopeGuardedManager()

    def assert_scope_allowed(self):
        """Refuse to hand a person a role carrying keys their tenant may not hold.

        ``clean()`` already pins the role to the assignment's tenant, so this
        cannot normally fire - the role's own rows are guarded as they are
        written. It is here for the row that predates the guard: a role that
        already carries a platform key stops being *assignable* as well as
        stopping being effective, so the grant cannot be revived by re-issuing
        it to somebody new.
        """
        if not self.role_id or self.assignment_status != self.AssignmentStatus.ACTIVE:
            return
        tenant = self.tenant if self.tenant_id else None
        if tenant_is_platform(tenant):
            return
        keys = TenantRolePermission.objects.filter(
            role_id=self.role_id, granted=True,
        ).values_list("permission_id", flat=True)
        assert_tenant_may_hold(keys, tenant, field="role")

    def save(self, *args, **kwargs):
        self.assert_scope_allowed()
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        self.assert_scope_allowed()
        errors = {}
        if self.user_id and self.user.tenant_id != self.tenant_id:
            errors["user"] = "User must belong to the assignment tenant."
        if self.role_id and self.role.tenant_id != self.tenant_id:
            errors["role"] = "Role must belong to the assignment tenant."
        if self.branch_id and self.branch.tenant_id != self.tenant_id:
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
# Per-user permission overrides (exceptions layered on top of role grants)
# -----------------------------------------------------------------------------
class UserPermissionOverride(TimeStampedModel):
    """A single permission exception pinned to one user inside one tenant.

    Roles remain the way access is *designed*; this table is the escape hatch
    for the two cases a role edit cannot express without collateral damage:

    * ``DENY`` - take one key away from one person while their role keeps it
      for everyone else.
    * ``ALLOW`` - hand one extra key to one person without minting a role.

    Evaluation order lives in :func:`vs_rbac.evaluator.get_effective_permissions`
    and is *later wins*::

        (role_granted - role_denied) | user_allows - user_denies

    so a personal DENY beats everything, including a personal ALLOW. Expiry is
    lazy: an expired row simply stops matching the evaluator's filter, so no
    cron is required to make it stop applying.

    There is deliberately **no approval workflow** (owner decision, rev 2):
    accountability comes from the required ``reason``, the ``RBACAuditLog``
    trail, and the fact that the ``*.overrides.manage`` key is CRITICAL and
    restricted.

    Attributes:
        tenant: Tenant that owns both the override and the user.
        user: The person the exception applies to.
        permission: Permission key (``to_field="key"`` - ``permission_id`` IS
            the dotted key, matching every other RBAC link table).
        mode: ``ALLOW`` or ``DENY``.
        reason: Required justification, surfaced in the audit trail and UI.
        created_by: Actor who wrote the override.
        expires_at: Optional expiry; ``null`` means permanent.
    """

    class Mode(models.TextChoices):
        ALLOW = "ALLOW", "Allow (extra grant)"
        DENY = "DENY", "Deny (exception)"

    tenant = models.ForeignKey(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="user_permission_overrides",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="permission_overrides",
    )
    permission = models.ForeignKey(
        Permission,
        to_field="key",
        db_column="permission_key",
        on_delete=models.PROTECT,
        related_name="user_overrides",
    )
    mode = models.CharField(max_length=8, choices=Mode.choices)
    reason = models.TextField()
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_permission_overrides",
    )
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # One override per key per user: a new override REPLACES the old one
            # (delete + create, both audited) instead of stacking.
            models.UniqueConstraint(
                fields=["user", "permission"],
                name="uq_user_permission_override",
            ),
        ]
        indexes = [
            # The evaluator's hot path: rows for one (tenant, user), filtered by
            # expiry.
            models.Index(fields=["tenant", "user", "expires_at"]),
            models.Index(fields=["permission", "mode"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.mode}:{self.permission_id}"

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    objects = ScopeGuardedManager()

    def assert_scope_allowed(self):
        """An ALLOW override is a grant, so it obeys the same scope rule.

        This is the path the escalation used: the override serializer offers
        every active key, and tenant membership was the only thing checked. A
        DENY is exempt - removing a key from one person cannot escalate them.
        """
        if self.mode != self.Mode.ALLOW or not self.permission_id:
            return
        assert_tenant_may_hold([self.permission_id], self.tenant if self.tenant_id else None)

    def clean(self):
        super().clean()
        errors = {}
        if self.user_id and self.tenant_id and self.user.tenant_id != self.tenant_id:
            errors["user"] = "User must belong to the override tenant."
        if not (self.reason or "").strip():
            errors["reason"] = "A reason is required for a permission override."
        if errors:
            raise ValidationError(errors)
        self.assert_scope_allowed()

    def save(self, *args, **kwargs):
        self.assert_scope_allowed()
        return super().save(*args, **kwargs)


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
# RBACAuditLog - authoritative, append-only audit for RBAC actions
# ---------------------------------------------------------------------------

class RBACAuditLog(models.Model):
    """Append-only audit log for RBAC actions (B21 hybrid-audit pattern).

    The central ``vs_audit.emit_audit_event`` is best-effort by contract - it
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
    # Loose school reference (slug) - survives school deletion, no FK cascade.
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
