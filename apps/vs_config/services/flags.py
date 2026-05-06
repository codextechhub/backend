# vs_config/services/flags.py
#
# FlagService — the single interface for reading and writing
# InstitutionFeatureFlag records.
#
# ALL other modules must use FlagService.is_enabled() to check whether
# a feature is active for an institution before executing any
# institution-specific workflow. Direct ORM queries on InstitutionFeatureFlag
# from outside vs_config are not permitted.
#
# is_enabled() is designed to be called frequently and cheaply.
# Cache layer can be added here in a future release without changing call sites.

from django.db import transaction

from ..models import BranchFeatureFlag, ConfigurationChangeLog
from ..constants import ChangeType, FLAG_REGISTRY
from ..validators import (
    validate_flag_key_exists,
    validate_flag_dependencies_met,
    validate_no_dependent_flags_enabled,
    validate_disable_reason_provided,
)
from .audit import write_audit_log, ConfigAuditActions


class FlagService:
    """
    Read and write branch feature flags.
    """

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    @staticmethod
    def is_enabled(branch, flag_key: str) -> bool:
        """
        Check whether a feature flag is enabled for a branch.

        Returns False (disabled) for any flag that has never been explicitly set.
        All platform modules must call this before executing branch workflows.

        Example:
            if not FlagService.is_enabled(branch, 'modules.finance'):
                raise PermissionDenied({'error_code': 'MODULE_NOT_ENABLED', ...})
        """
        validate_flag_key_exists(flag_key)

        try:
            flag = BranchFeatureFlag.objects.get(
                branch=branch,
                flag_key=flag_key,
            )
            return flag.is_enabled
        except BranchFeatureFlag.DoesNotExist:
            # Never-set flag defaults to disabled.
            return False

    @staticmethod
    def get_all_flags_for_branch(branch) -> list:
        """
        Return the state of ALL flags in FLAG_REGISTRY for a given branch.

        Each entry includes:
          - flag_key
          - label (human-readable from registry)
          - is_enabled
          - set_by (user who last toggled, or None if never set)
          - set_at (timestamp of last toggle, or None if never set)

        Flags that have never been explicitly set appear with is_enabled=False
        and set_by=None. The full registry is always returned — no gaps.
        """
        # Build a lookup dict from existing DB rows for this branch
        existing = {
            f.flag_key: f
            for f in BranchFeatureFlag.objects.filter(
                branch=branch
            ).select_related("set_by")
        }

        result = []
        for key, label in FLAG_REGISTRY.items():
            flag_obj = existing.get(key)
            result.append({
                "flag_key":   key,
                "label":      label,
                "is_enabled": flag_obj.is_enabled if flag_obj else False,
                "set_by":     flag_obj.set_by if flag_obj else None,
                "set_at":     flag_obj.set_at if flag_obj else None,
            })

        return result

    @staticmethod
    def get_flag_history(branch, flag_key: str = None):
        """
        Return ConfigurationChangeLog entries for flag changes on this branch.
        Optionally filter to a specific flag_key.
        """
        qs = ConfigurationChangeLog.objects.filter(
            branch=branch,
            change_type=ChangeType.FEATURE_FLAG,
        ).select_related("changed_by")

        if flag_key:
            validate_flag_key_exists(flag_key)
            qs = qs.filter(target_key=flag_key)

        return qs  # ordered by -changed_at via model Meta

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def toggle_flag(branch, flag_key: str, enable: bool,
                    actor, reason: str = "") -> BranchFeatureFlag:
        """
        Enable or disable a feature flag for a branch.

        Validation:
          - flag_key must exist in FLAG_REGISTRY
          - If enabling: all dependency flags must already be enabled
          - If disabling: no dependent flags may be currently enabled
          - If disabling a Live branch's flag: reason is required

        Writes:
          - BranchFeatureFlag record (create or update)
          - ConfigurationChangeLog entry
          - Module 5 platform audit log entry

        All three writes are inside a single atomic transaction. If any fails,
        all roll back and the flag state remains unchanged.
        """
        validate_flag_key_exists(flag_key)

        if enable:
            validate_flag_dependencies_met(flag_key, branch)
        else:
            validate_no_dependent_flags_enabled(flag_key, branch)
            validate_disable_reason_provided(reason, branch)

        # Get current state for change log
        previous_value = "false"
        try:
            existing = BranchFeatureFlag.objects.get(
                branch=branch,
                flag_key=flag_key,
            )
            previous_value = "true" if existing.is_enabled else "false"
        except BranchFeatureFlag.DoesNotExist:
            existing = None

        new_value = "true" if enable else "false"

        flag, _ = BranchFeatureFlag.objects.update_or_create(
            branch=branch,
            flag_key=flag_key,
            defaults={"is_enabled": enable, "set_by": actor},
        )

        ConfigurationChangeLog.objects.create(
            change_type=ChangeType.FEATURE_FLAG,
            target_key=flag_key,
            branch=branch,
            previous_value=previous_value,
            new_value=new_value,
            changed_by=actor,
            reason=reason,
        )

        audit_action = (
            ConfigAuditActions.FLAG_ENABLED if enable
            else ConfigAuditActions.FLAG_DISABLED
        )
        write_audit_log(
            actor=actor,
            action=audit_action,
            target_type="BranchFeatureFlag",
            target_id=str(flag.id),
            detail={
                "flag_key": flag_key,
                "label": FLAG_REGISTRY.get(flag_key, flag_key),
                "previous_value": previous_value,
                "new_value": new_value,
                "reason": reason,
            },
            branch=branch,
        )

        return flag

    @staticmethod
    def seed_default_flags(branch, actor=None) -> None:
        """
        Seed all-disabled flag records for a newly provisioned branch.
        Called from Module 1 (Institution Management) during provisioning.

        Creates a BranchFeatureFlag record for every key in FLAG_REGISTRY,
        all set to is_enabled=False. Uses get_or_create so it is safe to call
        multiple times (idempotent).

        No change log entries are written for seeding — default state is not a
        meaningful configuration change.
        """
        for flag_key in FLAG_REGISTRY:
            BranchFeatureFlag.objects.get_or_create(
                branch=branch,
                flag_key=flag_key,
                defaults={"is_enabled": False, "set_by": actor},
            )
