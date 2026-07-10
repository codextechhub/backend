"""Typed configuration, capability, entitlement, override, and audit models.

The app is built from two parallel systems that share one scoping contract:

Configuration (settings with values)
    ConfigurationDefinition declares WHAT a setting is (its key, type,
    validation rules, and where it may be set). ConfigurationValue stores
    one concrete value per definition per scope. Reads resolve through the
    precedence chain: branch value -> school value -> platform value ->
    definition default.

Capabilities (features that are on or off)
    Capability declares a switchable unit of product functionality (a
    module or a feature). CapabilityEntitlement records whether a school
    is ALLOWED to have it (the commercial grant). CapabilityOverride
    records whether it is actually TURNED ON at runtime (platform, school,
    or branch scope). CapabilityDependency links capabilities that require
    other capabilities. An override can never switch on something that is
    not entitled.

Both systems write every mutation to ConfigurationAuditEvent, an immutable
append-only log enforced in Python and by database triggers.

Scoping: models that support platform/school/branch placement inherit
ScopedModel, which normalizes the scope into a single ``scope_key`` string
("platform", "school:<id>", or "branch:<id>") used in unique constraints
and precedence lookups.
"""

import uuid
from django.core.exceptions import ValidationError
from django.db import models
from django.conf import settings

from vs_rbac.managers import TenantAwareManager


class ConfigurationDefinition(models.Model):
    """Declare one typed setting: its key, type, rules, and where it may be set.

    This is the SCHEMA half of the configuration system. A definition does
    not hold any live value itself (other than the fallback default) — it
    describes a setting so that ConfigurationValue rows can be validated
    against it and application code can read it through
    ``vs_config.conf.get_config(key)``.

    Only platform (CX_STAFF) users may create, update, or archive
    definitions; school users can at most read them and write values where
    ``allowed_scopes`` permits.

    Fields:
        id: Stable UUID primary key.
        key: Immutable dotted machine key, e.g. ``security.retry_limit``.
            Lowercase dot notation enforced by the serializer; immutable
            after creation because application code and stored values
            reference it. Unique across the platform.
        label: Human-readable name shown in admin UIs.
        description: Explanation of the behavior the setting controls.
        value_type: One of STRING, INTEGER, DECIMAL, BOOLEAN, JSON, CHOICE,
            or SECRET_REFERENCE. Drives ``validate_value()`` type checks for
            both the default and every scoped value.
        default_value: Fallback JSON value returned by resolution when no
            platform/school/branch value exists. May be null.
        validation_rules: Type-specific constraints as JSON — ``choices``
            (list) for CHOICE, ``min``/``max`` bounds for numeric types.
        allowed_scopes: List of scope names ("platform", "school",
            "branch") where a value may be written. ``set_value()`` rejects
            writes at any scope not in this list.
        sensitivity: PUBLIC or INTERNAL values are shown as stored;
            SECRET_REFERENCE marks the setting as pointing at a secret
            (e.g. ``env://PAYMENTS_SECRET``) and every serializer, effective
            read, and audit snapshot redacts it to "[REDACTED]".
        is_active: Soft-archive flag. Inactive definitions are hidden from
            default listings and never resolve — ``get_config`` returns the
            caller's default instead. Archive is the DELETE verb; rows are
            never hard-deleted because values and audit history point here.
        created_by: User who created the definition (nullable history;
            SET_NULL on user deletion).
        created_at: Creation timestamp.
        updated_at: Timestamp of the most recent definition change.
    """

    class ValueType(models.TextChoices):
        STRING = "STRING", "String"
        INTEGER = "INTEGER", "Integer"
        DECIMAL = "DECIMAL", "Decimal"
        BOOLEAN = "BOOLEAN", "Boolean"
        JSON = "JSON", "JSON"
        CHOICE = "CHOICE", "Choice"
        SECRET_REFERENCE = "SECRET_REFERENCE", "Secret reference"

    class Sensitivity(models.TextChoices):
        PUBLIC = "PUBLIC", "Public"
        INTERNAL = "INTERNAL", "Internal"
        SECRET_REFERENCE = "SECRET_REFERENCE", "Secret reference"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=200, unique=True, db_index=True)
    label = models.CharField(max_length=160)
    description = models.TextField()
    value_type = models.CharField(max_length=24, choices=ValueType.choices)
    default_value = models.JSONField(null=True, blank=True)
    validation_rules = models.JSONField(default=dict, blank=True)
    allowed_scopes = models.JSONField(default=list)
    sensitivity = models.CharField(
        max_length=24, choices=Sensitivity.choices, default=Sensitivity.INTERNAL
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="configuration_definitions_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.key


class ScopedModel(models.Model):
    """Abstract base providing the shared platform/school/branch scope contract.

    Any model that can be placed at one of the three scopes inherits this
    (ConfigurationValue, CapabilityOverride, ConfigurationAuditEvent).
    The scope of a row is defined by its two nullable FKs:

        school NULL, branch NULL  -> platform scope (applies everywhere)
        school set,  branch NULL  -> school scope
        branch set                -> branch scope (school auto-filled)

    ``scope_key`` is a denormalized string form of that placement —
    "platform", "school:<uuid>", or "branch:<uuid>" — computed on every
    save. It exists so that:

    * unique constraints can express "one row per definition per scope"
      without tripping over NULLs (SQL treats NULL != NULL, so a plain
      unique on (definition, school, branch) would allow duplicate
      platform rows), and
    * precedence lookups can fetch all candidate rows for a scope chain
      in one query (``scope_key__in=["branch:x", "school:y", "platform"]``).

    Fields:
        school: Optional school boundary. Null together with branch means
            platform scope.
        branch: Optional branch boundary. When set it must belong to
            ``school``; if school is omitted the branch's own school is
            filled in automatically (enforced in ``clean()``).
        scope_key: Normalized scope string described above. Not editable;
            recomputed by ``save()``.

    Behavior:
        ``save()`` always runs ``clean()`` (school/branch consistency) and
        ``set_scope_key()`` first, so rows can never be persisted with a
        scope_key that disagrees with their FKs. Abstract — creates no
        table of its own.
    """

    school = models.ForeignKey(
        "vs_schools.School", on_delete=models.CASCADE, null=True, blank=True,
        related_name="+",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.CASCADE, null=True, blank=True,
        related_name="+",
    )
    scope_key = models.CharField(max_length=80, editable=False, db_index=True)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.branch_id:
            branch_school_id = self.branch.school_id
            if self.school_id is None:
                self.school_id = branch_school_id
            elif self.school_id != branch_school_id:
                raise ValidationError({"branch": "Branch must belong to the selected school."})

    def set_scope_key(self):
        if self.branch_id:
            self.scope_key = f"branch:{self.branch_id}"
        elif self.school_id:
            self.scope_key = f"school:{self.school_id}"
        else:
            self.scope_key = "platform"

    def save(self, *args, **kwargs):
        self.clean()
        self.set_scope_key()
        return super().save(*args, **kwargs)


class ConfigurationValue(ScopedModel):
    """Store one concrete value for a definition at exactly one scope.

    This is the DATA half of the configuration system. At most one row may
    exist per (definition, scope) — enforced by the ``uniq_config_value_scope``
    constraint on (definition, scope_key) — so writes are upserts
    (``services.resolution.set_value``) and reads walk the precedence chain
    (``services.resolution.resolve_value``):

        branch row -> school row -> platform row -> definition.default_value

    Values are never written directly through the ORM by views; they go
    through ``set_value()``, which checks the definition's allowed_scopes,
    validates the value against its type and rules, and records an audit
    event in the same transaction.

    Fields:
        id: Stable UUID primary key.
        definition: The ConfigurationDefinition this value belongs to.
            CASCADE — values die with their definition.
        value: The JSON-native payload. Its shape is guaranteed by
            ``validate_value()`` to match ``definition.value_type`` and
            ``definition.validation_rules`` at write time.
        school / branch / scope_key: Scope placement inherited from
            :class:`ScopedModel`.
        updated_by: User who most recently set the value (nullable,
            SET_NULL on user deletion).
        created_at: Creation timestamp.
        updated_at: Timestamp of the most recent value change.

    Managers:
        ``objects`` is tenant-aware (``include_global=True``): school-scoped
        requests see their own rows plus platform rows automatically.
        ``all_objects`` is the unscoped escape hatch used by the resolution
        service and by views that have already authorized an explicit scope.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(
        ConfigurationDefinition, on_delete=models.CASCADE, related_name="values"
    )
    value = models.JSONField()
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="configuration_values_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager(include_global=True)
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            models.UniqueConstraint(
                fields=["definition", "scope_key"], name="uniq_config_value_scope"
            )
        ]
        indexes = [models.Index(fields=["school", "branch"])]


class Capability(models.Model):
    """One switchable unit of product functionality in the unified catalogue.

    Replaces the legacy XVSModules + BranchFeatureFlag split: a capability
    is either a whole product MODULE (finance, attendance, student_portal)
    or a smaller FEATURE (bulk_import, email_alerts) — the ``kind`` field
    is the only distinction. Whether a capability is ON for a given
    school/branch is never stored here; it is computed by
    ``services.capabilities.effective_capability`` from three inputs:

        1. entitlement — is the school allowed to have it?
           (CapabilityEntitlement; skipped when ``requires_entitlement``
           is False)
        2. dependencies — are all prerequisite capabilities effective?
           (CapabilityDependency)
        3. override — has an operator toggled it at branch, school, or
           platform scope? (CapabilityOverride; most specific scope wins,
           falling back to ``default_enabled``)

    Application code asks ``vs_config.conf.is_capability_enabled(key, ...)``;
    the frontend reads GET /v1/config/effective-capabilities/.

    Fields:
        id: Stable UUID primary key.
        key: Immutable unique slug (e.g. ``finance``). Referenced by
            package setups, runtime checks, and URLs, so it never changes
            after creation.
        label: Human-readable name shown to administrators.
        description: Functional description of what the capability unlocks.
        kind: MODULE (sellable product area) or FEATURE (smaller toggle).
        requires_entitlement: When True (typical for modules), the school
            must hold a GRANTED entitlement before the capability can ever
            be effective. When False (typical for features), only
            dependencies and overrides apply.
        default_enabled: The runtime state used when no override exists at
            any scope in the chain.
        is_active: Catalogue lifecycle flag. Inactive capabilities never
            resolve as enabled and are hidden from default listings;
            archiving is the DELETE verb (no hard deletes — entitlements,
            overrides, and audit history reference this row).
        metadata: Free-form, non-authoritative JSON for display or
            integration hints (icons, ordering, docs links). Never used in
            enablement decisions.
        created_at: Creation timestamp.
        updated_at: Timestamp of the most recent catalogue change.
    """

    class Kind(models.TextChoices):
        MODULE = "MODULE", "Module"
        FEATURE = "FEATURE", "Feature"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.SlugField(max_length=100, unique=True)
    label = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.MODULE)
    requires_entitlement = models.BooleanField(default=True)
    default_enabled = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "label"]

    def __str__(self):
        return self.label


class CapabilityDependency(models.Model):
    """A prerequisite edge: ``capability`` cannot be ON unless ``requires`` is ON.

    Forms a directed acyclic graph over the catalogue. During evaluation,
    ``effective_capability`` recursively checks every ``requires`` edge in
    the SAME scope as the capability being evaluated — so enabling
    ``procurement`` at a branch demands that ``finance`` also resolves as
    effective for that branch (entitlement, overrides and all), not merely
    that it exists.

    Example (seeded): procurement -> finance, parent_portal -> student_portal.

    Fields:
        capability: The dependent capability being evaluated (CASCADE).
        requires: The prerequisite that must itself resolve as enabled
            (CASCADE).

    Integrity:
        Three layers keep the graph sane — a unique constraint rejects
        duplicate edges, a DB check constraint rejects self-references, and
        ``clean()`` (always run via ``save()``) walks the existing graph to
        reject any edge that would create a cycle, since a cycle would make
        evaluation non-terminating.
    """

    capability = models.ForeignKey(
        Capability, on_delete=models.CASCADE, related_name="dependency_links"
    )
    requires = models.ForeignKey(
        Capability, on_delete=models.CASCADE, related_name="dependent_links"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["capability", "requires"], name="uniq_capability_dependency"
            ),
            models.CheckConstraint(
                condition=~models.Q(capability=models.F("requires")),
                name="capability_cannot_require_itself",
            ),
        ]

    def clean(self):
        super().clean()
        if not self.capability_id or not self.requires_id:
            return
        if self.capability_id == self.requires_id:
            raise ValidationError("A capability cannot depend on itself.")
        pending = [self.requires_id]
        seen = set()
        while pending:
            current = pending.pop()
            if current == self.capability_id:
                raise ValidationError("Capability dependency would create a cycle.")
            if current in seen:
                continue
            seen.add(current)
            pending.extend(
                CapabilityDependency.objects.filter(capability_id=current)
                .values_list("requires_id", flat=True)
            )

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class CapabilityEntitlement(models.Model):
    """The commercial/administrative grant: is a school ALLOWED this capability?

    Answers "did they buy it / were they given it", never "is it switched
    on". Runtime state lives in CapabilityOverride; the two are kept apart
    so a school can temporarily disable a module it pays for without losing
    the grant, and so no branch toggle can ever enable something that was
    never granted (``set_override`` refuses ENABLED without an active
    entitlement here).

    Scoping is deliberately narrower than ScopedModel: entitlements exist
    only at school or platform level (a NULL school means "every school"),
    because branches don't buy modules — schools do. Hence this model
    carries its own school FK + scope_key rather than inheriting the
    three-level contract. A school-specific row always beats the platform
    row during evaluation, which lets a platform-wide grant carry
    school-level DENIED exceptions.

    Rows are written by school package setup (source=PACKAGE), platform
    admins (MANUAL/PLATFORM), or data migration (IMPORT), always through
    ``services.capabilities.set_entitlement`` so every change is audited.

    Fields:
        id: Stable UUID primary key.
        capability: The capability being granted or denied (CASCADE).
        school: The school the decision applies to; NULL = platform-wide.
        scope_key: "school:<id>" or "platform", computed on save; unique
            together with capability, so there is exactly one decision per
            capability per scope and writes are upserts.
        state: GRANTED or DENIED. An explicit DENIED row at school level
            overrides a platform-wide GRANTED.
        source: Where the decision came from — PACKAGE (school package
            setup), PLATFORM, MANUAL, or IMPORT (legacy migration).
        starts_at: Optional activation time; the grant is inert before it.
        ends_at: Optional exclusive expiry (subscription end); the grant is
            inert from this moment on, flipping the capability off without
            any data change.
        updated_by: User who most recently changed the entitlement
            (nullable, SET_NULL).
        created_at: Creation timestamp.
        updated_at: Timestamp of the most recent entitlement change.

    Managers:
        ``objects`` is tenant-aware (schools see their own rows plus
        platform-wide ones); ``all_objects`` is the unscoped escape hatch
        used by the evaluation service.
    """

    class State(models.TextChoices):
        GRANTED = "GRANTED", "Granted"
        DENIED = "DENIED", "Denied"

    class Source(models.TextChoices):
        PACKAGE = "PACKAGE", "Package"
        PLATFORM = "PLATFORM", "Platform"
        MANUAL = "MANUAL", "Manual"
        IMPORT = "IMPORT", "Import"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capability = models.ForeignKey(
        Capability, on_delete=models.CASCADE, related_name="entitlements"
    )
    school = models.ForeignKey(
        "vs_schools.School", on_delete=models.CASCADE, null=True, blank=True,
        related_name="capability_entitlements",
    )
    scope_key = models.CharField(max_length=80, editable=False, db_index=True)
    state = models.CharField(max_length=12, choices=State.choices)
    source = models.CharField(max_length=12, choices=Source.choices)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="capability_entitlements_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager(include_global=True)
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            models.UniqueConstraint(
                fields=["capability", "scope_key"], name="uniq_capability_entitlement_scope",
            )
        ]

    def save(self, *args, **kwargs):
        self.scope_key = f"school:{self.school_id}" if self.school_id else "platform"
        return super().save(*args, **kwargs)


class CapabilityOverride(ScopedModel):
    """The runtime toggle: is an entitled capability actually switched ON here?

    The operational half of the entitlement/override pair — this is what
    replaced the legacy BranchFeatureFlag. Overrides may sit at any of the
    three scopes, and evaluation reads the MOST SPECIFIC non-INHERIT row:

        branch override -> school override -> platform override
        -> capability.default_enabled

    A DISABLED override anywhere in that chain switches the capability off
    for that scope even though the school remains fully entitled (e.g. a
    school pausing its parent portal during exams). The reverse is blocked:
    ``set_override`` raises CapabilityNotEntitled when asked to write
    ENABLED for a capability the scope's school is not entitled to, so
    runtime toggles can never widen commercial access.

    At most one override exists per (capability, scope) — the
    ``uniq_capability_override_scope`` constraint — so writes are upserts
    through ``services.capabilities.set_override``, which audits every
    change.

    Fields:
        id: Stable UUID primary key.
        capability: The capability whose runtime state is overridden
            (CASCADE).
        school / branch / scope_key: Scope placement inherited from
            :class:`ScopedModel`.
        state: ENABLED, DISABLED, or INHERIT. INHERIT rows exist to
            explicitly hand the decision back up the chain (equivalent to
            no row at this scope) while preserving who set it and why.
        reason: Operator explanation, stored on the row and in the audit
            event.
        updated_by: User who most recently changed the override (nullable,
            SET_NULL).
        created_at: Creation timestamp.
        updated_at: Timestamp of the most recent override change.

    Managers:
        ``objects`` is tenant-aware (schools see their own rows plus
        platform rows); ``all_objects`` is the unscoped escape hatch used
        by the evaluation service.
    """

    class State(models.TextChoices):
        INHERIT = "INHERIT", "Inherit"
        ENABLED = "ENABLED", "Enabled"
        DISABLED = "DISABLED", "Disabled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capability = models.ForeignKey(
        Capability, on_delete=models.CASCADE, related_name="overrides"
    )
    state = models.CharField(max_length=12, choices=State.choices)
    reason = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="capability_overrides_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager(include_global=True)
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            models.UniqueConstraint(
                fields=["capability", "scope_key"], name="uniq_capability_override_scope"
            )
        ]


class ConfigurationAuditEvent(ScopedModel):
    """Append-only record of every configuration and capability mutation.

    Every write in this app — definition changes, value writes, capability
    catalogue edits, entitlement and override changes — creates exactly one
    of these rows inside the same transaction, via
    ``services.audit.record_configuration_event`` (which also mirrors the
    event to the platform-wide vs_audit trail; THIS table is the
    authoritative local history).

    Immutability is enforced twice: ``save()`` refuses to update an
    existing row and ``delete()`` always raises (Python guard, covers
    SQLite tests), and migration 0006 installs BEFORE UPDATE / BEFORE
    DELETE triggers that reject the same operations at the database level
    on PostgreSQL and MySQL, so even raw SQL and bulk querysets cannot
    rewrite history.

    Snapshots are redacted BEFORE they are stored: secret-reference values
    arrive here as "[REDACTED]", so the audit trail can be exposed to
    config.audit.view holders without leaking secrets.

    Fields:
        id: Stable UUID primary key.
        action: Stable dotted action key, e.g. ``config.value.updated``,
            ``config.capability.archived``; ``legacy.*`` actions come from
            the 0004 data migration. Indexed for filtering.
        target_type: Class name of the mutated record (e.g.
            "ConfigurationValue"). A loose string, not a content-type FK,
            so history survives model renames and deletions.
        target_id: String primary key of the mutated record. Indexed.
        actor: User responsible; NULL for system/migration work
            (SET_NULL so history outlives user accounts).
        school / branch / scope_key: Scope the mutation applied to,
            inherited from :class:`ScopedModel`. School-scoped audit
            listings filter on these; platform events (both NULL) are
            visible only to platform staff.
        before_data: Redacted JSON snapshot of the state before the change
            (empty dict for creations).
        after_data: Redacted JSON snapshot of the state after the change.
        reason: Operator-provided justification, when given.
        metadata: Extra non-secret context (request info, legacy change
            ids from migration).
        created_at: Event timestamp; indexed, and the default ordering is
            newest first.

    Managers:
        ``objects`` is tenant-aware (schools see their own events plus
        global ones); ``all_objects`` is the unscoped escape hatch used by
        the immutability guard and platform views.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action = models.CharField(max_length=80, db_index=True)
    target_type = models.CharField(max_length=80)
    target_id = models.CharField(max_length=200, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="configuration_audit_events",
    )
    before_data = models.JSONField(default=dict, blank=True)
    after_data = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = TenantAwareManager(include_global=True)
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["school", "branch", "-created_at"])]

    def save(self, *args, **kwargs):
        if self.pk and ConfigurationAuditEvent.all_objects.filter(pk=self.pk).exists():
            raise ValueError("ConfigurationAuditEvent rows are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("ConfigurationAuditEvent rows are immutable.")
