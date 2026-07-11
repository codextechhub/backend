import re

from rest_framework import serializers

from .constants import VALID_SCOPES
from .models import (
    Capability,
    CapabilityEntitlement,
    CapabilityOverride,
    ConfigurationAuditEvent,
    ConfigurationDefinition,
    ConfigurationValue,
)
from .services.resolution import validate_value


class ActorSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    email = serializers.EmailField(read_only=True)
    full_name = serializers.CharField(read_only=True)


class ConfigurationDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConfigurationDefinition
        fields = [
            "id", "key", "label", "description", "value_type", "default_value",
            "validation_rules", "allowed_scopes", "sensitivity", "is_active",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

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
            "id", "definition", "key", "school", "branch", "value",
            "updated_by", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.definition.sensitivity == instance.definition.Sensitivity.SECRET_REFERENCE:
            data["value"] = "[REDACTED]"
        return data


class SetConfigurationValueSerializer(serializers.Serializer):
    # Scope (school/branch) is resolved once per request from the top-level
    # payload or query string — never per item, so it is not declared here.
    key = serializers.RegexField(
        r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$", max_length=200
    )
    value = serializers.JSONField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class CapabilitySerializer(serializers.ModelSerializer):
    dependencies = serializers.ListField(
        child=serializers.SlugField(max_length=100), required=False, write_only=True
    )

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
            "id", "capability", "capability_key", "school", "state", "source",
            "starts_at", "ends_at", "updated_by", "created_at", "updated_at",
        ]
        read_only_fields = fields


class SetEntitlementSerializer(serializers.Serializer):
    capability = serializers.SlugField(max_length=100)
    school = serializers.CharField(required=False, allow_null=True)
    state = serializers.ChoiceField(choices=CapabilityEntitlement.State.choices)
    source = serializers.ChoiceField(choices=CapabilityEntitlement.Source.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class CapabilityOverrideSerializer(serializers.ModelSerializer):
    capability_key = serializers.CharField(source="capability.key", read_only=True)

    class Meta:
        model = CapabilityOverride
        fields = [
            "id", "capability", "capability_key", "school", "branch", "state",
            "reason", "updated_by", "created_at", "updated_at",
        ]
        read_only_fields = fields


class SetOverrideSerializer(serializers.Serializer):
    capability = serializers.SlugField(max_length=100)
    school = serializers.CharField(required=False, allow_null=True)
    branch = serializers.CharField(required=False, allow_null=True)
    state = serializers.ChoiceField(choices=CapabilityOverride.State.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class ConfigurationAuditEventSerializer(serializers.ModelSerializer):
    actor = ActorSerializer(read_only=True)
    target_label = serializers.SerializerMethodField()

    class Meta:
        model = ConfigurationAuditEvent
        fields = [
            "id", "action", "target_type", "target_id", "target_label", "school",
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
        except (ValueError, TypeError):
            label = ""

        cache[key] = label
        return label
