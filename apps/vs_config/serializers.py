# vs_config/serializers.py
#
# Serializers are split by purpose:
#   List serializers      — minimal fields, used in paginated list views
#   Detail serializers    — full fields, used in single-record retrieval
#   Write serializers     — used for POST and PATCH; validate input
#   Read-only serializers — change log, flag history; never accept writes
#
# The `key` field on ConfigurationKey is IMMUTABLE after creation.
# Write serializers must never expose it as an editable field on PATCH.

from rest_framework import serializers

from .models import (
    ConfigurationKey,
    BranchFeatureFlag,
    BranchConfigOverride,
    ConfigurationChangeLog,
)
from .constants import FLAG_REGISTRY, PERMITTED_SELF_SERVICE_KEYS


# ---------------------------------------------------------------------------
# Shared nested serializers
# ---------------------------------------------------------------------------

class ActorSummarySerializer(serializers.Serializer):
    """
    Minimal representation of a user actor used inside other serializers.
    Avoids importing the full UserAccount serializer from vs_users.
    """
    id       = serializers.UUIDField(read_only=True)
    email    = serializers.EmailField(read_only=True)
    # full_name is a property on UserAccount; adjust field name if different
    full_name = serializers.CharField(source="get_full_name", read_only=True)


# ---------------------------------------------------------------------------
# ConfigurationKey serializers
# ---------------------------------------------------------------------------

class ConfigurationKeyListSerializer(serializers.ModelSerializer):
    """
    Used in list views. Minimal fields — no created_by detail.
    """
    class Meta:
        model  = ConfigurationKey
        fields = ["id", "key", "description", "is_active", "updated_at"]
        read_only_fields = fields


class ConfigurationKeyDetailSerializer(serializers.ModelSerializer):
    """
    Used in single-record GET. Full fields including creator.
    """
    created_by = ActorSummarySerializer(read_only=True)

    class Meta:
        model  = ConfigurationKey
        fields = [
            "id", "key", "value", "description", "is_active",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = fields


class ConfigurationKeyCreateSerializer(serializers.Serializer):
    """
    Used for POST /config/keys/.
    The `key` field is accepted only on creation — never on update.
    Validation is delegated to vs_config.validators via the service layer.
    """
    key         = serializers.CharField(max_length=200)
    value       = serializers.CharField()
    description = serializers.CharField()

    def validate_key(self, value):
        # Normalise to lowercase. Format and uniqueness checked in service.
        return value.lower().strip()

    def validate_value(self, value):
        if not value or value.strip() == "":
            raise serializers.ValidationError("Value cannot be empty.")
        return value

    def validate_description(self, value):
        if not value or value.strip() == "":
            raise serializers.ValidationError("Description cannot be empty.")
        return value


class ConfigurationKeyUpdateSerializer(serializers.Serializer):
    """
    Used for PATCH /config/keys/{key}/.
    Only value and description are updatable. The key field is ignored.
    """
    value       = serializers.CharField(required=False, allow_blank=False)
    description = serializers.CharField(required=False, allow_blank=False)

    def validate(self, data):
        if not data:
            raise serializers.ValidationError(
                "At least one of 'value' or 'description' must be provided."
            )
        return data


# ---------------------------------------------------------------------------
# Feature flag serializers
# ---------------------------------------------------------------------------

class BranchFeatureFlagSerializer(serializers.Serializer):
    """
    Read serializer for a single flag annotated with registry metadata.
    Used inside BranchFlagBulkSerializer.
    """
    flag_key   = serializers.CharField(read_only=True)
    label      = serializers.CharField(read_only=True)
    is_enabled = serializers.BooleanField(read_only=True)
    set_by     = ActorSummarySerializer(read_only=True)
    set_at     = serializers.DateTimeField(read_only=True, allow_null=True)


class BranchFlagBulkSerializer(serializers.Serializer):
    """
    Returns the full FLAG_REGISTRY annotated with branch-specific state.
    Used for GET /config/branches/{id}/flags/.
    Always returns all flags — no gaps for never-set flags.
    """
    flags = BranchFeatureFlagSerializer(many=True, read_only=True)


class FlagToggleSerializer(serializers.Serializer):
    """
    Used for PATCH /config/branches/{id}/flags/{flag_key}/.
    Accepts is_enabled and optional reason.
    reason is required at the service level when disabling a Live branch's flag.
    """
    is_enabled = serializers.BooleanField()
    reason     = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Required when disabling a flag for a Live branch.",
    )


# ---------------------------------------------------------------------------
# Branch config override serializers
# ---------------------------------------------------------------------------

class BranchConfigOverrideSerializer(serializers.ModelSerializer):
    """
    Read serializer for a single override record.
    Used in list and detail views for Branch Admin self-service.
    """
    updated_by = ActorSummarySerializer(read_only=True)

    class Meta:
        model  = BranchConfigOverride
        fields = ["id", "key", "value", "updated_by", "updated_at"]
        read_only_fields = fields


class BranchOverrideBulkUpdateSerializer(serializers.Serializer):
    """
    Used for PATCH /config/my-branch/overrides/.
    Accepts a dict of key → value pairs.
    Each key must be in PERMITTED_SELF_SERVICE_KEYS.
    Value validation (timezone, locale, date_format) is delegated to service.

    Example input:
        {
            "overrides": {
                "institution.timezone": "Africa/Lagos",
                "institution.date_format": "DD/MM/YYYY"
            }
        }
    """
    overrides = serializers.DictField(
        child=serializers.CharField(),
        help_text="Map of permitted config keys to their new values.",
    )

    def validate_overrides(self, data):
        if not data:
            raise serializers.ValidationError("At least one override key must be provided.")

        unknown_keys = [k for k in data if k not in PERMITTED_SELF_SERVICE_KEYS]
        if unknown_keys:
            raise serializers.ValidationError(
                {
                    "error_code": "KEY_NOT_PERMITTED",
                    "message": (
                        f"The following keys are not permitted: {', '.join(unknown_keys)}. "
                        f"Allowed keys: {', '.join(PERMITTED_SELF_SERVICE_KEYS)}"
                    ),
                }
            )
        return data


# ---------------------------------------------------------------------------
# Change log / history serializers (read-only)
# ---------------------------------------------------------------------------

class ConfigurationChangeLogSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for ConfigurationChangeLog.
    Used for all history/audit views within this module.
    Rows are never created, updated, or deleted via this serializer.
    """
    changed_by = ActorSummarySerializer(read_only=True)

    class Meta:
        model  = ConfigurationChangeLog
        fields = [
            "id",
            "change_type",
            "target_key",
            "institution",
            "branch",
            "previous_value",
            "new_value",
            "changed_by",
            "changed_at",
            "reason",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Export serializer
# ---------------------------------------------------------------------------

class ConfigExportSerializer(serializers.Serializer):
    """
    Used for the GET /config/export/ response.
    Structures global config keys and branch flag snapshots.
    """
    global_config = ConfigurationKeyListSerializer(many=True, read_only=True)
    branch_flags  = serializers.DictField(
        child=BranchFeatureFlagSerializer(many=True),
        read_only=True,
        help_text="Branch slug → list of flag states.",
    )
