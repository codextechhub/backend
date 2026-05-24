# vs_config/validators.py
#
# All validation logic for the vs_config module lives here.
# Views and services call these functions before touching the database.
#
# Raises ValidationError (DRF) on failure so that views surface clean,
# structured error responses with consistent error_code fields.

import re
from zoneinfo import available_timezones

from rest_framework.exceptions import ValidationError

from .constants import (
    FLAG_REGISTRY,
    FLAG_DEPENDENCY_MAP,
    PERMITTED_SELF_SERVICE_KEYS,
    VALID_DATE_FORMATS,
)


# ---------------------------------------------------------------------------
# Config key name validation
# ---------------------------------------------------------------------------

# Only lowercase letters, digits, underscores, and dots. No spaces, no uppercase.
_KEY_PATTERN = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


def validate_config_key_format(key: str) -> None:
    """
    Validates that a config key name follows the dot-notation format:
    e.g. 'auth.session_timeout_minutes', 'branch.timezone'

    Raises ValidationError with error_code INVALID_KEY_FORMAT on failure.
    """
    if not key or not _KEY_PATTERN.match(key):
        raise ValidationError(
            {
                "error_code": "INVALID_KEY_FORMAT",
                "message": (
                    "Config key names must use lowercase letters, digits, underscores, and dots only. "
                    "At least one dot is required. Example: auth.session_timeout_minutes"
                ),
                "field": "key",
            }
        )


def validate_config_key_unique(key: str, exclude_id=None) -> None:
    """
    Validates that a config key name does not already exist — whether
    active or archived (soft-deleted). Archived key names are still
    reserved to prevent silent shadowing.

    Raises ValidationError with error_code DUPLICATE_KEY on failure.
    Includes a hint if the conflict is with an archived key.
    """
    from .models import ConfigurationKey

    qs = ConfigurationKey.objects.filter(key=key)
    if exclude_id:
        qs = qs.exclude(id=exclude_id)

    existing = qs.first()
    if existing:
        if not existing.is_active:
            raise ValidationError(
                {
                    "error_code": "DUPLICATE_KEY",
                    "message": (
                        f"A deleted key with the name '{key}' already exists. "
                        "Restore it instead of creating a new one."
                    ),
                    "field": "key",
                    "meta": {"existing_id": str(existing.id), "is_archived": True},
                }
            )
        raise ValidationError(
            {
                "error_code": "DUPLICATE_KEY",
                "message": f"A configuration key with the name '{key}' already exists.",
                "field": "key",
                "meta": {"existing_id": str(existing.id), "is_archived": False},
            }
        )


def validate_config_value_not_empty(value: str) -> None:
    """
    Config key values cannot be empty strings.

    Raises ValidationError with error_code VALUE_REQUIRED on failure.
    """
    if value is None or str(value).strip() == "":
        raise ValidationError(
            {
                "error_code": "VALUE_REQUIRED",
                "message": "Value cannot be empty. Enter a value for this configuration key.",
                "field": "value",
            }
        )


def validate_config_description_not_empty(description: str) -> None:
    """
    Description is required on key creation — prevents vague entries.

    Raises ValidationError with error_code DESCRIPTION_REQUIRED on failure.
    """
    if not description or str(description).strip() == "":
        raise ValidationError(
            {
                "error_code": "DESCRIPTION_REQUIRED",
                "message": "A description is required. Explain what this configuration key controls.",
                "field": "description",
            }
        )


def validate_key_not_in_use_by_overrides(config_key_instance) -> None:
    """
    Prevents soft-deletion of a global config key that is actively referenced
    by one or more BranchConfigOverride records.

    Raises ValidationError with error_code KEY_IN_USE on failure.
    """
    from .models import BranchConfigOverride

    overrides = BranchConfigOverride.objects.filter(key=config_key_instance.key)
    count = overrides.count()
    if count > 0:
        raise ValidationError(
            {
                "error_code": "KEY_IN_USE",
                "message": (
                    f"This key is currently overridden by {count} branch(es). "
                    "Remove those overrides before deleting this key."
                ),
                "field": None,
                "meta": {"override_count": count},
            }
        )


# ---------------------------------------------------------------------------
# Feature flag validation
# ---------------------------------------------------------------------------

def validate_flag_key_exists(flag_key: str) -> None:
    """
    Validates that a flag_key is present in FLAG_REGISTRY.
    Unknown keys are rejected — only registered flags are accepted.

    Raises ValidationError with error_code INVALID_FLAG_KEY on failure.
    """
    if flag_key not in FLAG_REGISTRY:
        raise ValidationError(
            {
                "error_code": "INVALID_FLAG_KEY",
                "message": (
                    f"'{flag_key}' is not a recognised feature flag. "
                    f"Valid flags: {', '.join(sorted(FLAG_REGISTRY.keys()))}"
                ),
                "field": "flag_key",
            }
        )


def validate_flag_dependencies_met(flag_key: str, branch) -> None:
    """
    When enabling a flag, checks that all required dependency flags are
    already enabled for this branch.

    Example: enabling modules.procurement requires modules.finance to be on.

    Raises ValidationError with error_code FLAG_DEPENDENCY_UNMET on failure,
    listing every unmet dependency by key and label.
    """
    from .models import BranchFeatureFlag

    required_keys = FLAG_DEPENDENCY_MAP.get(flag_key, [])
    if not required_keys:
        return

    unmet = []
    for dep_key in required_keys:
        try:
            flag = BranchFeatureFlag.objects.get(
                branch=branch,
                flag_key=dep_key,
            )
            if not flag.is_enabled:
                unmet.append(dep_key)
        except BranchFeatureFlag.DoesNotExist:
            # Never-set flag defaults to disabled.
            unmet.append(dep_key)

    if unmet:
        unmet_labels = [f"'{k}' ({FLAG_REGISTRY.get(k, k)})" for k in unmet]
        raise ValidationError(
            {
                "error_code": "FLAG_DEPENDENCY_UNMET",
                "message": (
                    f"Cannot enable '{flag_key}'. The following flags must be enabled first: "
                    + ", ".join(unmet_labels)
                ),
                "field": "flag_key",
                "meta": {"unmet_dependencies": unmet},
            }
        )


def validate_no_dependent_flags_enabled(flag_key: str, branch) -> None:
    """
    When disabling a flag, checks that no currently enabled flag depends on it.

    Example: cannot disable modules.finance while modules.procurement is on.

    Raises ValidationError with error_code FLAG_HAS_DEPENDENTS on failure,
    listing every blocking dependent flag.
    """
    from .models import BranchFeatureFlag

    # Build reverse map: dep_key → list of flags that require it
    dependents = [
        key
        for key, deps in FLAG_DEPENDENCY_MAP.items()
        if flag_key in deps
    ]
    if not dependents:
        return

    # Check which of those dependents are currently enabled for this branch
    blocking = []
    for dep_key in dependents:
        try:
            flag = BranchFeatureFlag.objects.get(
                branch=branch,
                flag_key=dep_key,
            )
            if flag.is_enabled:
                blocking.append(dep_key)
        except BranchFeatureFlag.DoesNotExist:
            pass  # Not set → disabled → not blocking

    if blocking:
        blocking_labels = [f"'{k}' ({FLAG_REGISTRY.get(k, k)})" for k in blocking]
        raise ValidationError(
            {
                "error_code": "FLAG_HAS_DEPENDENTS",
                "message": (
                    f"Cannot disable '{flag_key}'. The following enabled flags depend on it: "
                    + ", ".join(blocking_labels)
                    + ". Disable those flags first."
                ),
                "field": "flag_key",
                "meta": {"blocking_dependents": blocking},
            }
        )


def validate_disable_reason_provided(reason: str, branch) -> None:
    """
    When disabling a flag for an active branch, a reason is required.
    Checks branch.status == BranchStatus.ACTIVE.

    Raises ValidationError with error_code REASON_REQUIRED on failure.
    """
    from vs_schools.models import BranchStatus

    if branch.status == BranchStatus.ACTIVE:
        if not reason or str(reason).strip() == "":
            raise ValidationError(
                {
                    "error_code": "REASON_REQUIRED",
                    "message": (
                        "A reason is required when disabling a feature flag for an active branch."
                    ),
                    "field": "reason",
                }
            )


# ---------------------------------------------------------------------------
# Branch self-service override validation
# ---------------------------------------------------------------------------

def validate_override_key_permitted(key: str) -> None:
    """
    Validates that the key being set by a Branch Admin is in the
    permitted self-service list.

    Raises ValidationError with error_code KEY_NOT_PERMITTED on failure.
    """
    if key not in PERMITTED_SELF_SERVICE_KEYS:
        raise ValidationError(
            {
                "error_code": "KEY_NOT_PERMITTED",
                "message": (
                    f"'{key}' is not a permitted self-service configuration key. "
                    f"Allowed keys: {', '.join(PERMITTED_SELF_SERVICE_KEYS)}"
                ),
                "field": "key",
            }
        )


def validate_override_value(key: str, value: str) -> None:
    """
    Validates the value of a permitted override key based on its expected format.

    Covers:
      - branch.timezone         → must be a valid IANA timezone
      - branch.locale           → must be non-empty (loose check in V1)
      - branch.date_format      → must be in VALID_DATE_FORMATS
      - branch.currency_display → must be non-empty string

    Raises ValidationError with a specific error_code per key type on failure.
    """
    if not value or str(value).strip() == "":
        raise ValidationError(
            {
                "error_code": "VALUE_REQUIRED",
                "message": "Override value cannot be empty.",
                "field": "value",
            }
        )

    if key == "branch.timezone":
        if value not in available_timezones():
            raise ValidationError(
                {
                    "error_code": "INVALID_TIMEZONE",
                    "message": (
                        f"'{value}' is not a recognised IANA timezone. "
                        "Use a valid timezone string such as 'Africa/Lagos' or 'Europe/London'."
                    ),
                    "field": "value",
                }
            )

    elif key == "branch.date_format":
        if value not in VALID_DATE_FORMATS:
            raise ValidationError(
                {
                    "error_code": "INVALID_DATE_FORMAT",
                    "message": (
                        f"'{value}' is not a supported date format. "
                        f"Supported formats: {', '.join(VALID_DATE_FORMATS)}"
                    ),
                    "field": "value",
                }
            )

    elif key == "branch.locale":
        # Loose validation in V1: must match a basic locale pattern like en-NG
        locale_pattern = re.compile(r"^[a-z]{2,3}(-[A-Z]{2,3})?$")
        if not locale_pattern.match(value):
            raise ValidationError(
                {
                    "error_code": "INVALID_LOCALE",
                    "message": (
                        f"'{value}' is not a valid locale string. "
                        "Use a format like 'en-NG', 'en-GB', or 'fr-FR'."
                    ),
                    "field": "value",
                }
            )

    # branch.currency_display: any non-empty string is acceptable in V1
