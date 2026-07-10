"""Typed configuration, capability, entitlement, override, and audit models."""

import uuid
from django.core.exceptions import ValidationError
from django.db import models
from django.conf import settings

from vs_rbac.managers import TenantAwareManager


class ConfigurationDefinition(models.Model):
    """Declare one typed setting and the scopes where it may be overridden.

    Args:
        id: Stable UUID identifier.
        key: Immutable dotted machine key used by APIs and application code.
        label: Human-readable setting name.
        description: Explanation of the behavior controlled by the setting.
        value_type: Expected data type for defaults and scoped values.
        default_value: Fallback JSON value used when no scoped value exists.
        validation_rules: Type-specific rules such as choices, minimum, or maximum.
        allowed_scopes: Any combination of platform, school, and branch.
        sensitivity: Visibility classification and secret-reference behavior.
        is_active: Soft lifecycle flag; inactive definitions do not resolve.
        created_by: User who created the definition, retained as nullable history.
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
    """Provide the shared platform, school, and branch scope contract.

    Args:
        school: Optional school boundary; null with branch null means platform scope.
        branch: Optional branch boundary; when set it must belong to ``school``.
        scope_key: Normalized platform/school/branch key used in unique constraints.

    Behavior:
        A branch automatically supplies its owning school when school is omitted.
        This model is abstract and creates no table of its own.
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
    """Store one definition value at platform, school, or branch scope.

    Args:
        id: Stable UUID identifier.
        definition: Typed definition that validates and describes this value.
        value: JSON-native value validated against the definition before writing.
        school: Optional school scope inherited from :class:`ScopedModel`.
        branch: Optional branch scope inherited from :class:`ScopedModel`.
        scope_key: Normalized scope identity inherited from :class:`ScopedModel`.
        updated_by: User who most recently set the value.
        created_at: Creation timestamp.
        updated_at: Timestamp of the most recent value change.
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
    """Represent one module or feature in the unified capability catalogue.

    Args:
        id: Stable UUID identifier.
        key: Immutable slug used by packages, APIs, and runtime checks.
        label: Human-readable capability name.
        description: Functional description shown to administrators.
        kind: Distinguishes a product module from a feature.
        requires_entitlement: Whether runtime enablement requires a grant.
        default_enabled: Runtime state when no override applies.
        is_active: Catalogue lifecycle flag; inactive capabilities never resolve on.
        metadata: Extensible non-authoritative display or integration metadata.
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
    """Require one capability to be effective before another can be effective.

    Args:
        capability: Dependent capability being evaluated.
        requires: Prerequisite capability that must resolve as enabled.

    Behavior:
        Duplicate edges, self-references, and dependency cycles are rejected.
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
    """Record commercial or administrative access to a capability.

    Args:
        id: Stable UUID identifier.
        capability: Capability being granted or denied.
        school: Optional school; null represents a platform-wide entitlement.
        scope_key: Normalized platform or school key used for uniqueness.
        state: Explicit granted or denied decision.
        source: Origin of the decision, such as a package or manual change.
        starts_at: Optional time from which the decision becomes effective.
        ends_at: Optional exclusive expiry time.
        updated_by: User who most recently changed the entitlement.
        created_at: Creation timestamp.
        updated_at: Timestamp of the most recent entitlement change.
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
    """Control runtime activation without changing commercial entitlement.

    Args:
        id: Stable UUID identifier.
        capability: Capability whose runtime state is overridden.
        school: Optional school scope inherited from :class:`ScopedModel`.
        branch: Optional branch scope inherited from :class:`ScopedModel`.
        scope_key: Normalized scope identity inherited from :class:`ScopedModel`.
        state: Inherit, enabled, or disabled runtime decision.
        reason: Operator explanation for the override.
        updated_by: User who most recently changed the override.
        created_at: Creation timestamp.
        updated_at: Timestamp of the most recent override change.

    Behavior:
        An enabled override cannot bypass a denied or missing entitlement.
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
    """Persist an immutable configuration or capability mutation event.

    Args:
        id: Stable UUID identifier.
        action: Stable action key describing the mutation.
        target_type: Model or logical resource type that changed.
        target_id: String identifier of the changed record.
        actor: User responsible for the mutation, nullable for system work.
        school: Optional school scope inherited from :class:`ScopedModel`.
        branch: Optional branch scope inherited from :class:`ScopedModel`.
        scope_key: Normalized scope identity inherited from :class:`ScopedModel`.
        before_data: Redacted JSON snapshot before the mutation.
        after_data: Redacted JSON snapshot after the mutation.
        reason: Operator-provided explanation.
        metadata: Additional non-secret request or migration context.
        created_at: Immutable event timestamp.

    Behavior:
        Python model guards and database triggers reject updates and deletes.
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
