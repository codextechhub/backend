# vs_config/services/config.py
#
# ConfigurationService — the single interface for reading and writing
# global configuration keys and institution-level overrides.
#
# ALL other modules must use ConfigurationService.get() and
# ConfigurationService.get_for_institution() to read config values.
# Direct ORM queries on ConfigurationKey or InstitutionConfigOverride
# from outside vs_config are not permitted.
#
# Write operations (create, update, soft-delete, restore, set override)
# atomically update the model AND write a ConfigurationChangeLog entry
# AND dispatch to the Module 5 audit log.

from django.db import transaction

from ..models import ConfigurationKey, BranchConfigOverride, ConfigurationChangeLog
from ..constants import ChangeType, PERMITTED_SELF_SERVICE_KEYS
from ..validators import (
    validate_config_key_format,
    validate_config_key_unique,
    validate_config_value_not_empty,
    validate_config_description_not_empty,
    validate_key_not_in_use_by_overrides,
    validate_override_key_permitted,
    validate_override_value,
)
from .audit import write_audit_log, ConfigAuditActions


class ConfigurationService:
    """
    Read and write global configuration keys and institution overrides.
    """

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    @staticmethod
    def get(key: str, default=None):
        """
        Read the active global value for a given key.
        Returns `default` if the key does not exist or is inactive.

        Used by ALL platform modules to read config values.
        Never query ConfigurationKey directly from outside vs_config.

        Example:
            timeout = ConfigurationService.get('auth.session_timeout_minutes', default='30')
        """
        try:
            obj = ConfigurationKey.objects.get(key=key, is_active=True)
            return obj.value
        except ConfigurationKey.DoesNotExist:
            return default

    @staticmethod
    def get_for_branch(branch, key: str, default=None):
        """
        Read a config value for a specific branch.

        Resolution order:
          1. BranchConfigOverride for this branch + key
          2. Global ConfigurationKey (active)
          3. caller-supplied default

        Example:
            tz = ConfigurationService.get_for_branch(branch, 'institution.timezone', 'UTC')
        """
        # 1. Check branch-level override first
        try:
            override = BranchConfigOverride.objects.get(
                branch=branch,
                key=key,
            )
            return override.value
        except BranchConfigOverride.DoesNotExist:
            pass

        # 2. Fall back to global key
        return ConfigurationService.get(key, default)

    @staticmethod
    def list_active_keys(include_inactive: bool = False):
        """
        Return a queryset of ConfigurationKey records.
        By default, only active (non-deleted) keys are returned.
        Pass include_inactive=True to include soft-deleted keys (Super Admin only).
        """
        qs = ConfigurationKey.objects.all()
        if not include_inactive:
            qs = qs.filter(is_active=True)
        return qs.select_related("created_by")

    # ------------------------------------------------------------------
    # Write operations — all wrapped in atomic transactions
    # ------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def create_key(key: str, value: str, description: str, actor) -> ConfigurationKey:
        """
        Create a new global configuration key.

        Validates:
          - key format (dot-notation, lowercase)
          - key uniqueness (active and archived)
          - value is not empty
          - description is not empty

        Writes:
          - ConfigurationKey record
          - ConfigurationChangeLog entry (previous_value='', new_value=value)
          - Module 5 platform audit log entry
        """
        validate_config_key_format(key)
        validate_config_key_unique(key)
        validate_config_value_not_empty(value)
        validate_config_description_not_empty(description)

        config_key = ConfigurationKey.objects.create(
            key=key,
            value=value,
            description=description,
            created_by=actor,
        )

        ConfigurationChangeLog.objects.create(
            change_type=ChangeType.GLOBAL_CONFIG,
            target_key=key,
            institution=None,
            previous_value="",
            new_value=value,
            changed_by=actor,
            reason="Key created.",
        )

        write_audit_log(
            actor=actor,
            action=ConfigAuditActions.KEY_CREATED,
            target_type="ConfigurationKey",
            target_id=str(config_key.id),
            detail={"key": key, "value": value},
        )

        return config_key

    @staticmethod
    @transaction.atomic
    def update_key(config_key: ConfigurationKey, value: str = None,
                   description: str = None, actor=None) -> ConfigurationKey:
        """
        Update the value and/or description of an existing config key.
        The `key` field (the dot-notation name) is immutable and is ignored here.

        Writes a change log entry only if value changed.
        """
        previous_value = config_key.value
        updated_fields = []

        if value is not None:
            validate_config_value_not_empty(value)
            config_key.value = value
            updated_fields.append("value")

        if description is not None:
            config_key.description = description
            updated_fields.append("description")

        if not updated_fields:
            return config_key  # Nothing to do

        config_key.save(update_fields=updated_fields + ["updated_at"])

        if "value" in updated_fields:
            ConfigurationChangeLog.objects.create(
                change_type=ChangeType.GLOBAL_CONFIG,
                target_key=config_key.key,
                institution=None,
                previous_value=previous_value,
                new_value=config_key.value,
                changed_by=actor,
            )

            write_audit_log(
                actor=actor,
                action=ConfigAuditActions.KEY_UPDATED,
                target_type="ConfigurationKey",
                target_id=str(config_key.id),
                detail={
                    "key": config_key.key,
                    "previous_value": previous_value,
                    "new_value": config_key.value,
                },
            )

        return config_key

    @staticmethod
    @transaction.atomic
    def soft_delete_key(config_key: ConfigurationKey, actor) -> ConfigurationKey:
        """
        Soft-delete a configuration key (sets is_active=False).
        Blocked if any InstitutionConfigOverride references this key.

        The key and its full history are retained — hard deletion is not supported.
        """
        validate_key_not_in_use_by_overrides(config_key)

        previous_value = config_key.value
        config_key.is_active = False
        config_key.save(update_fields=["is_active", "updated_at"])

        ConfigurationChangeLog.objects.create(
            change_type=ChangeType.GLOBAL_CONFIG,
            target_key=config_key.key,
            institution=None,
            previous_value=previous_value,
            new_value="[DELETED]",
            changed_by=actor,
            reason="Key soft-deleted.",
        )

        write_audit_log(
            actor=actor,
            action=ConfigAuditActions.KEY_DELETED,
            target_type="ConfigurationKey",
            target_id=str(config_key.id),
            detail={"key": config_key.key},
        )

        return config_key

    @staticmethod
    @transaction.atomic
    def restore_key(config_key: ConfigurationKey, actor) -> ConfigurationKey:
        """
        Restore a soft-deleted configuration key (sets is_active=True).
        """
        config_key.is_active = True
        config_key.save(update_fields=["is_active", "updated_at"])

        ConfigurationChangeLog.objects.create(
            change_type=ChangeType.GLOBAL_CONFIG,
            target_key=config_key.key,
            institution=None,
            previous_value="[DELETED]",
            new_value=config_key.value,
            changed_by=actor,
            reason="Key restored.",
        )

        write_audit_log(
            actor=actor,
            action=ConfigAuditActions.KEY_RESTORED,
            target_type="ConfigurationKey",
            target_id=str(config_key.id),
            detail={"key": config_key.key},
        )

        return config_key

    # ------------------------------------------------------------------
    # Institution override operations
    # ------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def set_branch_override(branch, key: str, value: str, actor) -> BranchConfigOverride:
        """
        Create or update a branch-level config override.
        Only keys in PERMITTED_SELF_SERVICE_KEYS are accepted.

        Writes:
          - BranchConfigOverride record (create or update)
          - ConfigurationChangeLog entry
          - Module 5 platform audit log entry
        """
        validate_override_key_permitted(key)
        validate_override_value(key, value)

        # Get existing value for change log (empty string if first time set)
        previous_value = ""
        try:
            existing = BranchConfigOverride.objects.get(
                branch=branch,
                key=key,
            )
            previous_value = existing.value
        except BranchConfigOverride.DoesNotExist:
            existing = None

        override, _ = BranchConfigOverride.objects.update_or_create(
            branch=branch,
            key=key,
            defaults={"value": value, "updated_by": actor},
        )

        ConfigurationChangeLog.objects.create(
            change_type=ChangeType.BRANCH_OVERRIDE,
            target_key=key,
            branch=branch,
            previous_value=previous_value,
            new_value=value,
            changed_by=actor,
        )

        write_audit_log(
            actor=actor,
            action=ConfigAuditActions.OVERRIDE_SET,
            target_type="BranchConfigOverride",
            target_id=str(override.id),
            detail={
                "key": key,
                "previous_value": previous_value,
                "new_value": value,
            },
            branch=branch,
        )

        return override

    @staticmethod
    def list_branch_overrides(branch):
        """
        Return all BranchConfigOverride records for a branch.
        """
        return BranchConfigOverride.objects.filter(
            branch=branch
        ).select_related("updated_by")

    @staticmethod
    def list_override_history(branch):
        """
        Return change log entries for all branch overrides,
        ordered most recent first.
        """
        return ConfigurationChangeLog.objects.filter(
            branch=branch,
            change_type=ChangeType.BRANCH_OVERRIDE,
        ).select_related("changed_by")
