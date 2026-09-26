from __future__ import annotations

import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from django.utils import timezone
from django.utils.text import slugify

from vs_history.queryset import VersionedManager
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


class ScopeGuardedManager(VersionedManager):
    """Manager whose ``bulk_create`` honours the per-row scope guard.

    ``bulk_create`` bypasses ``save()`` and ``clean()`` entirely, and it is how
    the role serializers write permission sets - so without this the model
    guard would be decorative on the exact path an attacker uses.

    Its queryset keeps the record history of the models that declare one (role
    grants and permission exceptions, see ``vs_rbac.history``) through
    ``update()`` and the bulk writes; for the rest it changes nothing.

    A model whose guard is one scope lookup per key may declare
    ``assert_scope_allowed_bulk(objs)``, which answers the whole batch in one
    query and refuses exactly what the per-row guard would. Without it, every
    row is checked on its own, which is a query per row: copying a prebuilt
    role of 200 permissions into a new school cost 200 lookups of the same
    table.
    """

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        bulk_guard = getattr(self.model, "assert_scope_allowed_bulk", None)
        if bulk_guard is not None:
            bulk_guard(objs)
        else:
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


class PermissionRegistryRevision(models.Model):
    """Durable cache generation for permission-registry policy changes.

    Effective permissions are memoised on the request's user instance.  A
    registry deactivation is an emergency revocation, so an already-warm user
    object must not keep the old authority.  This singleton generation lives
    in PostgreSQL rather than process memory, which makes invalidation visible
    to every application worker after the registry write commits.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    revision = models.PositiveBigIntegerField(default=1)

    @classmethod
    def current(cls) -> int:
        return (
            cls.objects.filter(pk=1).values_list("revision", flat=True).first()
            or 0
        )

    @classmethod
    def bump(cls) -> None:
        cls.objects.filter(pk=1).update(revision=models.F("revision") + 1)


# -----------------------------------------------------------------------------
# Permission vocabulary (Vision-owned, admin-manageable)
# -----------------------------------------------------------------------------

def sentence_label(text: str) -> str:
    """*text* with its first letter capitalised and the rest left as written.

    ``str.capitalize`` lowercases everything after the first letter, which
    turns "FX rates" into "Fx rates". Seeds carry wording a person already
    chose, so only the first letter is touched.
    """
    text = (text or "").strip()
    return text[:1].upper() + text[1:]


def display_label(stored: str, slug: str) -> str:
    """The name a person reads for a module, resource or field.

    The stored label when an administrator or a seed has set one, otherwise
    the slug read as words: ``virtual_account`` becomes "Virtual account".
    Every surface that names a tree node goes through this, so a screen and an
    API response can never disagree about what a node is called.
    """
    stored = (stored or "").strip()
    if stored:
        return stored
    return sentence_label((slug or "").replace("_", " "))


class PermissionModule(TimeStampedModel):
    """Top-level module bucket, e.g. 'finance', 'students'.

    ``label`` is the readable name ("Procurement"). Blank is allowed and reads
    as the slug through :func:`display_label`.
    """

    name = models.SlugField(max_length=64, primary_key=True)
    label = models.CharField(max_length=80, blank=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-updated_at", "name"]

    def __str__(self) -> str:
        return self.name


class PermissionResource(TimeStampedModel):
    """Resource scoped to a module, e.g. 'invoice' under 'finance'.

    ``label`` is the readable name ("Vendors"). Blank is allowed and reads as
    the slug through :func:`display_label`.
    """

    module = models.ForeignKey(
        PermissionModule,
        on_delete=models.CASCADE,
        related_name="resources",
    )
    name = models.SlugField(max_length=64)
    label = models.CharField(max_length=120, blank=True)
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

    #: The product capability a school must reach before this key does
    #: anything, or null when the key is core and every school has it.
    #:
    #: Two vocabularies meet here, and they are not the same list. A
    #: permission module is a namespace a key is filed under; a capability is
    #: a thing a school is sold. They were joined by a hardcoded map, which
    #: could only answer at module and resource level - so it could say
    #: "Finance", never "the bulk generation inside Finance". Depth is sold at
    #: the second granularity, so the join moved onto the row.
    #:
    #: Null is the safe direction and deliberate: an unclassified key stays
    #: available to everybody. The opposite default would hide working routes
    #: from paying schools the day a new module ships.
    #:
    #: Some keys must never be filled in. ``school.user_overrides.create``
    #: grants one person an exception to their role; who inside a school may
    #: do that is a role decision, and selling it by tier would be
    #: indefensible. ``vs_rbac.permission_bands.NEVER_BAND`` is the list, with
    #: the reason each stays out.
    capability = models.ForeignKey(
        "vs_config.Capability",
        on_delete=models.SET_NULL,
        db_constraint=False,
        null=True,
        blank=True,
        related_name="permissions",
    )

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

    @property
    def readable_label(self) -> str:
        """Return the backend wording a permission picker shows to people.

        Seeders may supply exact wording in ``description``. A definition with
        no description still reads naturally because its action and resource
        are the same parts from which its stable key is composed.
        """
        if description := self.description.strip():
            return description
        action = self.action_id.replace("_", " ")
        resource = self.resource.name.replace("_", " ")
        return f"{action} {resource}".capitalize()


class FieldDefinition(TimeStampedModel):
    """One field of a resource that an administrator may restrict per role.

    A permission says whether a person may open a record at all; a field
    definition names one piece of that record (a vendor's bank account number,
    a pupil's allergies) so access to it can be decided separately. Field
    definitions sit under the same :class:`PermissionResource` rows as
    permissions, so both appear in one Module, Resource tree.

    Code owns this table. Each app declares its fields in its own
    ``field_access.py`` through :func:`vs_rbac.field_registry.register_fields`,
    and ``manage.py sync_field_registry`` writes the declarations here. Nothing
    else creates or edits rows: a field an administrator could invent would
    have no serializer honouring it. A declaration that disappears from code
    leaves its row behind with ``is_active=False``, so anything that refers to
    the key keeps a target.

    ``name`` is the registry name and the last segment of ``key``.
    ``api_names`` are the names clients actually receive, which differ when one
    field reaches a response under several names (``invited_by`` travels as
    ``invited_by_id`` and ``invited_by_name``). An empty list is stored as
    ``[name]``.

    ``sensitive`` decides the default a role gets before anybody sets a switch:
    closed for sensitive fields, open for the rest. ``writable`` is false for
    values no API path can set (computed, provider-issued or derived), and such
    a field never offers a write switch.

    ``scope`` carries the same meaning and the same guard as
    :attr:`Permission.scope`, and has no default for the same reason: an
    unclassified field is refused by the sync rather than assumed safe for
    every tenant.

    ``open_on_create`` marks a field set freely while a record is being
    created, so the write switch governs only later changes to it. It is what
    keeps a role that may create a record, but not correct that field
    afterwards, able to create one at all.
    """

    key = models.CharField(max_length=200, primary_key=True)
    resource = models.ForeignKey(
        PermissionResource,
        on_delete=models.PROTECT,
        related_name="fields",
    )
    name = models.SlugField(max_length=64)
    api_names = models.JSONField(default=list, blank=True)
    label = models.CharField(max_length=120)
    group = models.CharField(max_length=60, blank=True)
    description = models.TextField(blank=True)
    sensitive = models.BooleanField(default=False)
    writable = models.BooleanField(default=True)
    open_on_create = models.BooleanField(
        default=False,
        help_text="Set freely while the record is created; the write switch governs later changes only.",
    )
    scope = models.CharField(
        max_length=16,
        choices=PermissionScope.choices,
        blank=True,
        help_text="Who may be granted this field: TENANT (any tenant) or PLATFORM (CX only).",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["resource", "name"], name="rbac_field_unique_per_resource",
            ),
        ]
        indexes = [
            models.Index(
                fields=["resource", "is_active"], name="rbac_field_resource_active",
            ),
        ]
        ordering = ["key"]

    def save(self, *args, **kwargs):
        if not kwargs.get("update_fields"):
            self.key = f"{self.resource.module_id}.{self.resource.name}.{self.name}"
        if not self.api_names:
            self.api_names = [self.name]
        super().save(*args, **kwargs)

    @property
    def default_access(self) -> dict:
        """Read and write for a role that has no switch set on this field."""
        return {
            "read": not self.sensitive,
            "write": (not self.sensitive) and self.writable,
        }

    def __str__(self) -> str:
        return self.key


def _field_scope_refusal(field, tenant):
    """The error for a field *tenant* may not be granted, or ``None``.

    A platform tenant may be granted any field. Every other tenant may be
    granted only fields declared ``TENANT``: a ``PLATFORM`` field and an
    unclassified one are refused alike, the same rule
    :func:`assert_tenant_may_hold` applies to permission keys.
    """
    if field is None or tenant_is_platform(tenant):
        return None
    if field.scope == PermissionScope.TENANT:
        return None
    return ValidationError({
        "field": (
            f"Field '{field.key}' is platform-scoped and cannot be set inside a tenant."
        ),
    })


def _field_write_refusal(field, *, attribute="can_write"):
    """The error for a write switch on a field no API path can write, or ``None``."""
    if field is None or field.writable:
        return None
    return ValidationError({
        attribute: f"Field '{field.key}' is not writable, so it has no write switch.",
    })


class RoleFieldAccess(TimeStampedModel):
    """One role's Read and Write switches on one registered field.

    A missing row means the field's default (:attr:`FieldDefinition.default_access`):
    open for a normal field, closed for a sensitive one. Resetting a field to its
    default deletes the row, so a stored row is always a decision somebody made,
    even when it happens to equal the default.

    Write implies Read. The database enforces it with a check constraint, so no
    write path (a serializer, a bulk update, a shell session) can store a role
    that may change a value it cannot see.

    The guard refuses two rows that could never be honoured: a field a
    non-platform tenant may not hold, and a write switch on a field that is not
    writable. It runs from ``clean()``, ``save()`` and
    :class:`ScopeGuardedManager.bulk_create`, the paths the ORM writes through.
    A queryset ``update()`` bypasses it, as it does every grant guard here.

    Rows are tenant-owned through their role. Changes are made through the
    field access endpoints, which lock, audit and bump ``role.version``.
    """

    role = models.ForeignKey(
        "TenantRoleTemplate", on_delete=models.CASCADE, related_name="field_access",
    )
    field = models.ForeignKey(
        FieldDefinition,
        to_field="key",
        db_column="field_key",
        on_delete=models.CASCADE,
        related_name="role_access",
    )
    can_read = models.BooleanField(default=False)
    can_write = models.BooleanField(default=False)
    set_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="set_role_field_access",
    )
    set_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["role", "field"], name="uq_role_field_access",
            ),
            models.CheckConstraint(
                condition=Q(can_write=False) | Q(can_read=True),
                name="ck_role_field_write_implies_read",
            ),
        ]
        indexes = [
            models.Index(fields=["field", "can_read"], name="rbac_rfa_field_read"),
        ]

    objects = ScopeGuardedManager()

    def assert_scope_allowed(self):
        if not self.field_id:
            return
        field = self.field
        tenant = self.role.tenant if self.role_id else None
        refusal = _field_scope_refusal(field, tenant)
        if refusal is None and self.can_write:
            refusal = _field_write_refusal(field)
        if refusal is not None:
            raise refusal

    def clean(self):
        super().clean()
        self.assert_scope_allowed()

    def save(self, *args, **kwargs):
        self.assert_scope_allowed()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.role_id}:{self.field_id}"


class PrebuiltRoleFieldAccess(models.Model):
    """Codex's default Read and Write switches for a prebuilt role.

    Copied onto a tenant's role when that role is provisioned from the
    prebuilt template, beside :class:`PrebuiltRolePermission`. Later edits here
    do not rewrite roles already provisioned, the same as prebuilt permissions.

    Prebuilt roles are tenant blueprints with no platform counterpart, so a
    platform-scoped field has no legitimate reading here and is refused, as a
    platform key is on :class:`PrebuiltRolePermission`. A write switch on a
    field that is not writable is refused too. Write implies Read, enforced in
    the database.
    """

    prebuilt_role = models.ForeignKey(
        "PrebuiltRoleTemplate",
        on_delete=models.CASCADE,
        related_name="default_field_access",
    )
    field = models.ForeignKey(
        FieldDefinition,
        to_field="key",
        db_column="field_key",
        on_delete=models.CASCADE,
        related_name="prebuilt_role_access",
    )
    can_read = models.BooleanField(default=False)
    can_write = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prebuilt_role", "field"], name="uq_prebuilt_role_field_access",
            ),
            models.CheckConstraint(
                condition=Q(can_write=False) | Q(can_read=True),
                name="ck_prebuilt_role_field_write_implies_read",
            ),
        ]
        verbose_name = "Prebuilt Role Field Access"
        verbose_name_plural = "Prebuilt Role Field Access"

    objects = ScopeGuardedManager()

    def assert_scope_allowed(self):
        if not self.field_id:
            return
        field = self.field
        if field.scope != PermissionScope.TENANT:
            raise ValidationError({
                "field": (
                    f"Field '{field.key}' is platform-scoped and cannot be a "
                    f"default on a prebuilt tenant role."
                ),
            })
        if self.can_write and (refusal := _field_write_refusal(field)) is not None:
            raise refusal

    def clean(self):
        super().clean()
        self.assert_scope_allowed()

    def save(self, *args, **kwargs):
        self.assert_scope_allowed()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.prebuilt_role_id}:{self.field_id}"


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
            declaration cannot drift from the contents. No group may contain a
            restricted key because attachment takes effect without approval.
        is_system: Legacy marker used only while old backend-created groups are
            converted to direct role grants and removed. API-created groups
            are always False.
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
        if self.permission_id and self.permission.is_restricted:
            raise ValidationError({
                "permission": (
                    f"'{self.permission_id}' is restricted and cannot be placed "
                    "in a permission group. Grant it through an approved role "
                    "change request instead."
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

    @classmethod
    def assert_scope_allowed_bulk(cls, objs):
        """The per-row guard for a whole batch, in one lookup."""
        refused = sorted(platform_only_keys(obj.permission_id for obj in objs))
        if refused:
            listed = ", ".join(f"'{key}'" for key in refused)
            verb = "is" if len(refused) == 1 else "are"
            raise ValidationError({
                "permission": (
                    f"{listed} {verb} platform-scoped and cannot be a default on "
                    f"a prebuilt tenant role."
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
    """Role blueprint with school-wide or selected branch reach.

    An empty branch set means school-wide. The first selected branch stays in
    ``branch`` for callers that still read that column; the remaining branches
    live in ``additional_branches``. All selected branches have equal reach.
    """

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
    additional_branches = models.ManyToManyField(
        Branch, blank=True, related_name="additional_tenant_role_templates",
    )

    @property
    def branch_ids(self):
        """Every selected branch id, empty for a school-wide role."""
        other_ids = [branch.pk for branch in self.additional_branches.all()]
        return ([self.branch_id] if self.branch_id else []) + other_ids
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

    @classmethod
    def assert_scope_allowed_bulk(cls, objs):
        """The per-row guard for a whole batch, one lookup per tenant."""
        keys_by_tenant = {}
        for obj in objs:
            if not obj.granted or not obj.permission_id:
                continue
            tenant = getattr(obj.role, "tenant", None) if obj.role_id else None
            entry = keys_by_tenant.setdefault(
                getattr(tenant, "pk", None), (tenant, set()),
            )
            entry[1].add(obj.permission_id)
        for tenant, keys in keys_by_tenant.values():
            assert_tenant_may_hold(keys, tenant)

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
        restricted = sorted(
            GroupPermission.objects.filter(
                group_id=self.group_id, permission__is_restricted=True,
            ).values_list("permission_id", flat=True)
        )
        if restricted:
            raise ValidationError({
                "group": (
                    "Permission groups cannot grant restricted permissions: "
                    f"{', '.join(restricted)}."
                ),
            })

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
            # Split in two on purpose. One constraint over (tenant, user, role) makes
            # the same role at two branches unstorable, so "Storekeeper at Ikeja" and
            # "Storekeeper at Lekki" - the arrangement a single User.branch cannot
            # express, and the reason branch scope is a set of grants - could not be
            # recorded at all. Two constraints keep both guarantees: at most one active
            # whole-tenant grant of a role per person, and at most one active grant of
            # a role per person per branch.
            #
            # A single constraint including branch would not do: PostgreSQL treats
            # NULLs as distinct, so it would permit duplicate whole-tenant grants.
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

    # ---------------------------------------------------------------- #
    # The rule the two constraints above spell out, said once in Python #
    # ---------------------------------------------------------------- #
    @classmethod
    def conflicting_active_grants(cls, *, tenant, user, role, branch, exclude_pk=None):
        """Active grants that a grant of *role* at *branch* would collide with.

        The Python half of the split constraints, and it has to say exactly what
        they say: a whole-tenant grant conflicts only with another whole-tenant
        grant of the same role, and a branch-pinned grant conflicts only with
        another grant of that role *at that same branch*. Both API write paths
        ask this one question, so neither can drift from the schema or from the
        other. They both used to ask a branch-blind one instead, which refused
        Mr Eze his second Teacher grant at Lekki: the schema stored the
        arrangement happily and the API would not let anybody create it.

        ``branch`` is the branch itself or ``None`` for a whole-tenant grant,
        never a bare id, because the two cases are different lookups.
        """
        qs = cls.objects.filter(
            tenant=tenant,
            user=user,
            role=role,
            assignment_status=cls.AssignmentStatus.ACTIVE,
        )
        # The NULL trap the constraint comment warns about, in its ORM form.
        # ``branch=branch`` for both cases works only because Django rewrites
        # ``= None`` into ``IS NULL``, and the first caller to pass an id-or-None
        # through the same expression loses the whole-tenant guarantee. Keeping
        # "no branch" a lookup rather than a value makes that unavailable.
        qs = qs.filter(branch__isnull=True) if branch is None else qs.filter(branch=branch)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs

    @staticmethod
    def duplicate_grant_message(role, branch):
        """What to tell the administrator whose grant was just refused.

        This renders under a field on the assign-role form, so it says what is
        actually refused rather than the old "already has an active assignment
        for this role", which stopped being true the moment branch mattered:
        they may well hold the role elsewhere, legitimately. Where the refusal
        is about one branch, the message names it, because "at Ikeja" is the
        difference between a mistake and a puzzle.

        The way out is the same sentence on both write paths deliberately.
        "Pick another branch" would read well on the assign form and be useless
        on the replace endpoint, which keeps the branch it was given and offers
        no choice of one - and useless again to a school with a single branch,
        where the branch control is not on screen at all.
        """
        role_name = getattr(role, "name", None) or getattr(role, "key", None) or "this"
        where = "across the whole organisation" if branch is None else f"at {branch.name}"
        return (
            f"This user already holds the {role_name} role {where}. "
            f"Revoke that assignment first."
        )

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

    There is deliberately **no approval workflow** for ordinary overrides
    (owner decision, rev 2): accountability comes from the required ``reason``
    and the ``RBACAuditLog`` trail. A restricted ALLOW is refused entirely and
    must be granted to a role through the reviewed change-request path.

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
        if self.permission.is_restricted:
            raise ValidationError({
                "permission": (
                    f"'{self.permission_id}' is restricted and cannot be granted "
                    "through a per-user override. Use an approved role change "
                    "request instead."
                ),
            })

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


class UserFieldAccessOverride(TimeStampedModel):
    """A one-person exception to what their roles say about one field.

    The field counterpart of :class:`UserPermissionOverride`, with the same
    rules: a required reason, an optional expiry that is evaluated lazily (an
    expired row stops matching, nothing sweeps it), never on yourself, and a
    new exception on the same field and access replaces the old one rather
    than stacking. The unique constraint on ``(user, field, access)`` is what
    makes replacement the only option.

    ``access`` names the switch and ``mode`` the direction. Evaluation lives in
    :func:`vs_rbac.field_evaluator.get_field_access`: ``ALLOW WRITE`` also
    grants Read, ``DENY READ`` also removes Write, and a DENY beats both a role
    and an ALLOW.

    There is no restricted field. Field switches take effect without approval,
    so any active field the tenant may hold can be allowed. The guard refuses
    only what could never be honoured: any exception on a field the tenant may
    not hold, and ``ALLOW WRITE`` on a field that is not writable.
    """

    class Access(models.TextChoices):
        READ = "READ", "Read"
        WRITE = "WRITE", "Write"

    class Mode(models.TextChoices):
        ALLOW = "ALLOW", "Allow (extra access)"
        DENY = "DENY", "Deny (exception)"

    tenant = models.ForeignKey(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="user_field_access_overrides",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="field_access_overrides",
    )
    field = models.ForeignKey(
        FieldDefinition,
        to_field="key",
        db_column="field_key",
        on_delete=models.PROTECT,
        related_name="user_overrides",
    )
    access = models.CharField(max_length=8, choices=Access.choices)
    mode = models.CharField(max_length=8, choices=Mode.choices)
    reason = models.TextField()
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_field_access_overrides",
    )
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "field", "access"],
                name="uq_user_field_access_override",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "user", "expires_at"], name="rbac_ufao_tenant_user_exp",
            ),
        ]
        ordering = ["-created_at"]

    objects = ScopeGuardedManager()

    def __str__(self) -> str:
        return f"{self.user_id}:{self.mode}:{self.access}:{self.field_id}"

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    def assert_scope_allowed(self):
        if not self.field_id:
            return
        field = self.field
        refusal = _field_scope_refusal(field, self.tenant if self.tenant_id else None)
        if (
            refusal is None
            and self.mode == self.Mode.ALLOW
            and self.access == self.Access.WRITE
        ):
            refusal = _field_write_refusal(field, attribute="access")
        if refusal is not None:
            raise refusal

    def clean(self):
        super().clean()
        errors = {}
        if self.user_id and self.tenant_id and self.user.tenant_id != self.tenant_id:
            errors["user"] = "User must belong to the exception tenant."
        if not (self.reason or "").strip():
            errors["reason"] = "A reason is required for a field access exception."
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

    **Who decides one is the workflow engine's answer, not this model's.** A
    request is submitted to a ``rbac.role_change`` ladder the moment it is
    raised, and the engine resolves the approvers, records their votes and fires
    the callback that applies the delta. The statuses here are the roles
    screen's summary of that - PENDING while the ladder runs, APPROVED once it
    completed, DENIED if it did not - and the instance holds the detail: which
    stage, who was eligible, who acted and when. See
    :mod:`vs_rbac.workflow_handlers`.
    """

    #: Routes through the approval engine, like a refund or a purchase order.
    workflow_document_type = "rbac.role_change"

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
