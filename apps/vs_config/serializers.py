import re
from collections import defaultdict
from uuid import UUID

from django.utils import timezone
from rest_framework import serializers

from .constants import VALID_SCOPES
from .models import (
    Capability,
    CapabilityEntitlement,
    CapabilityOverride,
    ConfigurationAuditEvent,
    ConfigurationAuditExportJob,
    ConfigurationAuditSavedView,
    ConfigurationDefinition,
    ConfigurationValue,
)
from .services.resolution import validate_value
from vs_schools.models import Currency, OwnershipType, TermStructure


class ActorSerializer(serializers.Serializer):
    id = serializers.CharField(read_only=True)
    email = serializers.EmailField(read_only=True)
    full_name = serializers.CharField(read_only=True)


class ConfigurationDefinitionSerializer(serializers.ModelSerializer):
    consumer = serializers.SerializerMethodField()

    class Meta:
        model = ConfigurationDefinition
        fields = [
            "id", "key", "label", "description", "value_type", "default_value",
            "validation_rules", "allowed_scopes", "sensitivity", "is_active",
            "consumer", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def get_consumer(self, obj):
        from .runtime_settings import get_setting_consumer

        return get_setting_consumer(obj.key)

    def validate_allowed_scopes(self, value):
        scopes = list(dict.fromkeys(value))
        invalid = set(scopes) - VALID_SCOPES
        if invalid or not scopes:
            raise serializers.ValidationError("Use one or more of: platform, school, branch.")
        return scopes

    def validate_key(self, value):
        if self.instance and value != self.instance.key:
            raise serializers.ValidationError("Configuration keys are immutable.")
        if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*", value):
            raise serializers.ValidationError(
                "Use lowercase dot notation, for example 'security.retry_limit'."
            )
        return value

    def validate(self, attrs):
        value_type = attrs.get("value_type", getattr(self.instance, "value_type", None))
        sensitivity = attrs.get("sensitivity", getattr(self.instance, "sensitivity", None))
        if value_type == ConfigurationDefinition.ValueType.SECRET_REFERENCE:
            attrs["sensitivity"] = ConfigurationDefinition.Sensitivity.SECRET_REFERENCE
        elif sensitivity == ConfigurationDefinition.Sensitivity.SECRET_REFERENCE:
            raise serializers.ValidationError(
                {"sensitivity": "Secret-reference sensitivity requires SECRET_REFERENCE value type."}
            )
        probe = ConfigurationDefinition(
            key=attrs.get("key", getattr(self.instance, "key", "value")),
            value_type=value_type,
            validation_rules=attrs.get(
                "validation_rules", getattr(self.instance, "validation_rules", {})
            ),
        )
        default = attrs.get("default_value", serializers.empty)
        if default is not serializers.empty and default is not None:
            validate_value(probe, default)
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.sensitivity == instance.Sensitivity.SECRET_REFERENCE:
            data["default_value"] = "[REDACTED]" if instance.default_value else None
        return data


class ConfigurationValueSerializer(serializers.ModelSerializer):
    key = serializers.CharField(source="definition.key", read_only=True)
    updated_by = ActorSerializer(read_only=True)

    class Meta:
        model = ConfigurationValue
        fields = [
            "id", "definition", "key", "tenant", "branch", "value",
            "updated_by", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.definition.sensitivity == instance.definition.Sensitivity.SECRET_REFERENCE:
            data["value"] = "[REDACTED]"
        return data


class SetConfigurationValueSerializer(serializers.Serializer):
    # Scope (tenant/branch) is resolved once per request from request.tenant
    # and the branch query/payload - never per item, so it is not declared here.
    key = serializers.RegexField(
        r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$", max_length=200
    )
    value = serializers.JSONField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class PlatformProfileSettingsSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=160, required=False)
    tagline = serializers.CharField(max_length=255, required=False, allow_blank=True)
    address = serializers.CharField(max_length=255, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=80, required=False, allow_blank=True)
    website = serializers.URLField(required=False, allow_blank=True)
    logo_url = serializers.URLField(required=False, allow_blank=True)


class SchoolOnboardingSettingsSerializer(serializers.Serializer):
    ownership_type = serializers.ChoiceField(choices=OwnershipType.choices, required=False)
    term_structure = serializers.ChoiceField(choices=TermStructure.choices, required=False)
    currency = serializers.ChoiceField(choices=Currency.choices, required=False)
    branch_country = serializers.CharField(max_length=80, required=False)


class PlatformSettingsUpdateSerializer(serializers.Serializer):
    profile = PlatformProfileSettingsSerializer(required=False)
    onboarding = SchoolOnboardingSettingsSerializer(required=False)
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if not attrs.get("profile") and not attrs.get("onboarding"):
            raise serializers.ValidationError(
                "Provide at least one platform profile or onboarding setting."
            )
        return attrs


class SecuritySettingsUpdateSerializer(serializers.Serializer):
    failed_login_threshold = serializers.IntegerField(
        min_value=3, max_value=20, required=False, allow_null=True
    )
    account_lock_minutes = serializers.IntegerField(
        min_value=5, max_value=1440, required=False, allow_null=True
    )
    self_reset_expiry_hours = serializers.IntegerField(
        min_value=1, max_value=24, required=False, allow_null=True
    )
    admin_reset_expiry_hours = serializers.IntegerField(
        min_value=1, max_value=168, required=False, allow_null=True
    )
    invitation_expiry_days = serializers.IntegerField(
        min_value=1, max_value=30, required=False, allow_null=True
    )
    proxy_idle_timeout_minutes = serializers.IntegerField(
        min_value=5, max_value=120, required=False, allow_null=True
    )
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if not any(field in attrs for field in self.fields if field != "reason"):
            raise serializers.ValidationError("Provide at least one security setting.")
        return attrs


class IntegrationSettingsUpdateSerializer(serializers.Serializer):
    email_sender_name = serializers.CharField(
        max_length=160, required=False, allow_null=True
    )
    email_sender_address = serializers.EmailField(required=False, allow_null=True)
    email_max_retries = serializers.IntegerField(
        min_value=0, max_value=10, required=False, allow_null=True
    )
    email_retry_backoff_seconds = serializers.IntegerField(
        min_value=1, max_value=3600, required=False, allow_null=True
    )
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_email_sender_name(self, value):
        if value is not None and not value.strip():
            raise serializers.ValidationError("Use a sender name or reset to deployment default.")
        return value.strip() if value is not None else None

    def validate(self, attrs):
        if not any(field in attrs for field in self.fields if field != "reason"):
            raise serializers.ValidationError("Provide at least one integration setting.")
        return attrs


class IntegrationConnectionTestSerializer(serializers.Serializer):
    connection = serializers.ChoiceField(choices=["email", "payments"])


class ClearConfigurationValueSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class CapabilitySerializer(serializers.ModelSerializer):
    dependencies = serializers.ListField(
        child=serializers.SlugField(max_length=100), required=False, write_only=True
    )

    def to_representation(self, instance):
        # Read side of the write-only dependencies list: the required keys.
        # A capability with an unmet dependency resolves OFF regardless of
        # grants/overrides, so clients need this to explain the state.
        data = super().to_representation(instance)
        data["dependencies"] = [
            link.requires.key
            for link in instance.dependency_links.select_related("requires").all()
        ]
        return data

    class Meta:
        model = Capability
        fields = [
            "id", "key", "label", "description", "kind", "requires_entitlement",
            "default_enabled", "is_active", "metadata", "dependencies",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_dependencies(self, keys):
        keys = list(dict.fromkeys(keys))
        own_key = self.initial_data.get("key") or getattr(self.instance, "key", None)
        if own_key in keys:
            raise serializers.ValidationError("A capability cannot depend on itself.")
        found = set(Capability.objects.filter(key__in=keys).values_list("key", flat=True))
        missing = set(keys) - found
        if missing:
            raise serializers.ValidationError(
                f"Unknown capabilities: {', '.join(sorted(missing))}."
            )
        return keys

    def validate_key(self, value):
        if self.instance and value != self.instance.key:
            raise serializers.ValidationError("Capability keys are immutable.")
        return value

    def _set_dependencies(self, capability, keys):
        from .models import CapabilityDependency

        requirements = list(Capability.objects.filter(key__in=keys))
        CapabilityDependency.objects.filter(capability=capability).exclude(
            requires__in=requirements
        ).delete()
        for requirement in requirements:
            CapabilityDependency.objects.get_or_create(
                capability=capability, requires=requirement
            )

    def create(self, validated_data):
        dependencies = validated_data.pop("dependencies", [])
        capability = super().create(validated_data)
        self._set_dependencies(capability, dependencies)
        return capability

    def update(self, instance, validated_data):
        dependencies = validated_data.pop("dependencies", None)
        capability = super().update(instance, validated_data)
        if dependencies is not None:
            self._set_dependencies(capability, dependencies)
        return capability

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["dependencies"] = list(
            instance.dependency_links.order_by("requires__key")
            .values_list("requires__key", flat=True)
        )
        return data


class CapabilityEntitlementSerializer(serializers.ModelSerializer):
    capability_key = serializers.CharField(source="capability.key", read_only=True)

    class Meta:
        model = CapabilityEntitlement
        fields = [
            "id", "capability", "capability_key", "tenant", "state", "source",
            "starts_at", "ends_at", "updated_by", "created_at", "updated_at",
        ]
        read_only_fields = fields


class SetEntitlementSerializer(serializers.Serializer):
    # Scope is resolved from request.tenant, never from the payload.
    capability = serializers.SlugField(max_length=100)
    state = serializers.ChoiceField(choices=CapabilityEntitlement.State.choices)
    source = serializers.ChoiceField(choices=CapabilityEntitlement.Source.choices)
    starts_at = serializers.DateTimeField(required=False, allow_null=True)
    ends_at = serializers.DateTimeField(required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        starts_at = attrs.get("starts_at")
        ends_at = attrs.get("ends_at")
        if attrs.get("state") == CapabilityEntitlement.State.DENIED and (starts_at or ends_at):
            raise serializers.ValidationError(
                "Scheduling applies to granted entitlements. Clear the dates for a denial."
            )
        if ends_at and ends_at <= timezone.now():
            raise serializers.ValidationError({"ends_at": "Expiry must be in the future."})
        if starts_at and ends_at and starts_at >= ends_at:
            raise serializers.ValidationError({"ends_at": "Expiry must be after activation."})
        return attrs


class BulkEntitlementTargetSerializer(serializers.Serializer):
    capability = serializers.SlugField(max_length=100)
    tenant = serializers.SlugField(max_length=80, required=False, allow_blank=True)


class BulkSetEntitlementSerializer(serializers.Serializer):
    items = BulkEntitlementTargetSerializer(many=True, min_length=1, max_length=100)
    starts_at = serializers.DateTimeField(required=False, allow_null=True)
    ends_at = serializers.DateTimeField(required=False, allow_null=True)
    reason = serializers.CharField(min_length=3, max_length=500)

    def validate(self, attrs):
        if "starts_at" not in self.initial_data and "ends_at" not in self.initial_data:
            raise serializers.ValidationError(
                "Provide an activation time, an expiry time, or both."
            )
        starts_at = attrs.get("starts_at")
        ends_at = attrs.get("ends_at")
        if ends_at and ends_at <= timezone.now():
            raise serializers.ValidationError({"ends_at": "Expiry must be in the future."})
        if starts_at and ends_at and starts_at >= ends_at:
            raise serializers.ValidationError({"ends_at": "Expiry must be after activation."})
        keys = [
            (item["capability"], item.get("tenant", ""))
            for item in attrs["items"]
        ]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError({"items": "Select each entitlement once."})
        return attrs


class CapabilityOverrideSerializer(serializers.ModelSerializer):
    capability_key = serializers.CharField(source="capability.key", read_only=True)

    class Meta:
        model = CapabilityOverride
        fields = [
            "id", "capability", "capability_key", "tenant", "branch", "state",
            "reason", "updated_by", "created_at", "updated_at",
        ]
        read_only_fields = fields


class SetOverrideSerializer(serializers.Serializer):
    # Tenant scope is resolved from request.tenant; only ``branch`` (read from
    # the request by resolve_request_scope) narrows within that tenant.
    capability = serializers.SlugField(max_length=100)
    branch = serializers.CharField(required=False, allow_null=True)
    state = serializers.ChoiceField(choices=CapabilityOverride.State.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class ConfigurationAuditFilterSerializer(serializers.Serializer):
    action = serializers.CharField(required=False, allow_blank=True, max_length=80)
    target_type = serializers.CharField(required=False, allow_blank=True, max_length=80)
    target_id = serializers.CharField(required=False, allow_blank=True, max_length=200)
    actor = serializers.CharField(required=False, allow_blank=True, max_length=80)
    created_after = serializers.DateTimeField(required=False, allow_null=True)
    created_before = serializers.DateTimeField(required=False, allow_null=True)

    def validate_actor(self, value):
        if not value:
            return value
        valid = value.isdigit() and int(value) > 0
        if not valid:
            try:
                UUID(value)
                valid = True
            except (ValueError, TypeError):
                valid = False
        if not valid:
            raise serializers.ValidationError("Use a valid actor ID.")
        return value

    def validate(self, attrs):
        after = attrs.get("created_after")
        before = attrs.get("created_before")
        if after and before and after >= before:
            raise serializers.ValidationError({
                "created_before": "The end of the window must be after the start."
            })
        return attrs


class ConfigurationAuditSavedFiltersSerializer(serializers.Serializer):
    window_days = serializers.ChoiceField(
        choices=[7, 30, 90, "all"], required=False, default=30,
    )
    action = serializers.CharField(required=False, allow_blank=True, max_length=80)
    actor = serializers.CharField(required=False, allow_blank=True, max_length=80)
    target_type = serializers.CharField(required=False, allow_blank=True, max_length=80)
    target_id = serializers.CharField(required=False, allow_blank=True, max_length=200)

    def validate_actor(self, value):
        return ConfigurationAuditFilterSerializer().validate_actor(value)

    def validate(self, attrs):
        if bool(attrs.get("target_type")) != bool(attrs.get("target_id")):
            raise serializers.ValidationError(
                "Target type and target id must be saved together."
            )
        return attrs


class ConfigurationAuditSavedViewWriteSerializer(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80, trim_whitespace=True)
    filters = ConfigurationAuditSavedFiltersSerializer()


class ConfigurationAuditSavedViewSerializer(serializers.ModelSerializer):
    tenant_slug = serializers.CharField(source="tenant.slug", read_only=True, allow_null=True)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True, allow_null=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True, allow_null=True)

    class Meta:
        model = ConfigurationAuditSavedView
        fields = [
            "id", "name", "filters", "scope_key", "tenant_slug", "tenant_name",
            "branch", "branch_name", "created_at", "updated_at",
        ]
        read_only_fields = fields


class ConfigurationAuditExportCreateSerializer(serializers.Serializer):
    filters = ConfigurationAuditFilterSerializer(required=False, default=dict)
    client_key = serializers.CharField(required=False, allow_blank=True, max_length=64)


class ConfigurationAuditExportJobSerializer(serializers.ModelSerializer):
    tenant_slug = serializers.CharField(source="tenant.slug", read_only=True, allow_null=True)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True, allow_null=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True, allow_null=True)
    download_available = serializers.SerializerMethodField()

    class Meta:
        model = ConfigurationAuditExportJob
        fields = [
            "id", "status", "filters", "scope_key", "tenant_slug", "tenant_name",
            "branch", "branch_name", "file_name", "row_count", "failure_message",
            "requested_at", "started_at", "completed_at", "available_until",
            "download_available",
        ]
        read_only_fields = fields

    def get_download_available(self, obj):
        return bool(
            obj.status == obj.Status.COMPLETED
            and obj.storage_name
            and obj.available_until
            and obj.available_until > timezone.now()
        )


class ConfigurationAuditEventSerializer(serializers.ModelSerializer):
    actor = ActorSerializer(read_only=True)
    target_label = serializers.SerializerMethodField()

    class Meta:
        model = ConfigurationAuditEvent
        fields = [
            "id", "action", "target_type", "target_id", "target_label", "tenant",
            "branch", "actor", "before_data", "after_data", "reason", "metadata",
            "created_at",
        ]
        read_only_fields = fields

    def get_target_label(self, obj):
        """Human name for the audited object so raw ids never reach the UI.

        Resolved at read time (event rows are immutable, so a label can't be
        backfilled onto old events). Deleted targets resolve to "" and the
        client falls back to the action wording. A per-request memo in the
        serializer context dedupes lookups across the page.
        """
        cache = self.context.setdefault("_target_labels", {})
        key = (obj.target_type, obj.target_id)
        if key in cache:
            return cache[key]

        label = ""
        try:
            if obj.target_type == "ConfigurationDefinition":
                row = ConfigurationDefinition.objects.filter(pk=obj.target_id).first()
                label = row.label if row else ""
            elif obj.target_type == "ConfigurationValue":
                row = (
                    ConfigurationValue.all_objects.select_related("definition")
                    .filter(pk=obj.target_id).first()
                )
                label = row.definition.label if row else ""
            elif obj.target_type == "Capability":
                row = Capability.objects.filter(pk=obj.target_id).first()
                label = row.label if row else ""
            elif obj.target_type == "CapabilityEntitlement":
                row = (
                    CapabilityEntitlement.all_objects.select_related("capability")
                    .filter(pk=obj.target_id).first()
                )
                label = row.capability.label if row else ""
            elif obj.target_type == "CapabilityOverride":
                row = (
                    CapabilityOverride.all_objects.select_related("capability")
                    .filter(pk=obj.target_id).first()
                )
                label = row.capability.label if row else ""
            elif obj.target_type == "IntegrationConnection":
                label = f"{obj.target_id.title()} connection"
            elif obj.target_type == "ConfigurationAuditExportJob":
                row = ConfigurationAuditExportJob.objects.filter(pk=obj.target_id).first()
                label = row.file_name or "Queued audit export" if row else ""
        except (ValueError, TypeError):
            label = ""

        cache[key] = label
        return label


def build_configuration_target_labels(events):
    """Resolve audit target labels in a bounded number of queries.

    Audit lists, facets and CSV exports can contain hundreds or thousands of
    rows. Grouping immutable target references by model avoids one lookup per
    event while preserving the serializer's deleted-target fallback.
    """
    grouped = defaultdict(set)
    labels = {}
    for event in events:
        key = (event.target_type, event.target_id)
        labels[key] = ""
        if event.target_type == "IntegrationConnection":
            labels[key] = f"{event.target_id.title()} connection"
        else:
            grouped[event.target_type].add(event.target_id)

    model_queries = {
        "ConfigurationDefinition": (
            ConfigurationDefinition.objects,
            lambda row: row.label,
        ),
        "ConfigurationValue": (
            ConfigurationValue.all_objects.select_related("definition"),
            lambda row: row.definition.label,
        ),
        "Capability": (
            Capability.objects,
            lambda row: row.label,
        ),
        "CapabilityEntitlement": (
            CapabilityEntitlement.all_objects.select_related("capability"),
            lambda row: row.capability.label,
        ),
        "CapabilityOverride": (
            CapabilityOverride.all_objects.select_related("capability"),
            lambda row: row.capability.label,
        ),
        "ConfigurationAuditExportJob": (
            ConfigurationAuditExportJob.objects,
            lambda row: row.file_name or "Queued audit export",
        ),
    }
    for target_type, raw_ids in grouped.items():
        query = model_queries.get(target_type)
        if not query:
            continue
        valid_ids = []
        canonical_to_raw = defaultdict(list)
        for raw_id in raw_ids:
            try:
                canonical = str(UUID(str(raw_id)))
            except (ValueError, TypeError, AttributeError):
                continue
            valid_ids.append(canonical)
            canonical_to_raw[canonical].append(raw_id)
        if not valid_ids:
            continue
        manager, label_for = query
        for row in manager.filter(pk__in=valid_ids):
            for raw_id in canonical_to_raw[str(row.pk)]:
                labels[(target_type, raw_id)] = label_for(row)
    return labels
