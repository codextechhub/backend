# =============================================================================
# vs_config / conf.py
#
# Public API for reading configuration and feature flags from other apps.
#
# Usage:
#
#   from vs_config.conf import get_config, get_config_for_branch, is_feature_enabled
#
#   # Read a global config value
#   timeout = get_config("auth.session_timeout_minutes", default="30")
#
#   # Read a config value for a specific branch (falls back to global if no override)
#   timezone = get_config_for_branch(branch, "institution.timezone", default="UTC")
#
#   # Check whether a feature module is enabled for a branch
#   if not is_feature_enabled(branch, "modules.finance"):
#       raise PermissionDenied(...)
#
# All valid flag keys are listed in vs_config/constants.py under FLAG_REGISTRY.
# All valid config keys are managed via the ConfigurationKey model.
# =============================================================================

from .services.config import ConfigurationService
from .services.flags import FlagService

__all__ = [
    "get_config",
    "get_config_for_branch",
    "is_feature_enabled",
]


def get_config(key: str, default=None):
    """
    Read the active global value for a configuration key.

    Returns `default` if the key does not exist or has been soft-deleted.
    Never query ConfigurationKey directly from outside vs_config — use this instead.

    Args:
        key:     Dot-notation config key, e.g. "auth.session_timeout_minutes".
        default: Value to return when the key is missing or inactive.

    Returns:
        The stored string value, or `default`.

    Example:
        max_retries = get_config("notification_email_max_retries", default="3")
    """
    return ConfigurationService.get(key, default)


def get_config_for_branch(branch, key: str, default=None):
    """
    Read a config value for a specific branch.

    Resolution order:
      1. BranchConfigOverride for this branch + key
      2. Global ConfigurationKey (active)
      3. caller-supplied default

    Use this instead of get_config() whenever the value may differ per branch
    (e.g. timezone, locale, or any branch-customisable setting).

    Args:
        branch:  The Branch instance to scope the lookup to.
        key:     Dot-notation config key, e.g. "institution.timezone".
        default: Value to return when neither override nor global key exists.

    Returns:
        The resolved string value, or `default`.

    Example:
        tz = get_config_for_branch(branch, "institution.timezone", default="UTC")
    """
    return ConfigurationService.get_for_branch(branch, key, default)


def is_feature_enabled(branch, flag_key: str) -> bool:
    """
    Check whether a feature flag is enabled for a branch.

    Returns False for any flag that has never been explicitly set.
    Call this before executing any branch-specific workflow that sits behind
    a feature flag gate.

    Args:
        branch:   The Branch instance to check the flag against.
        flag_key: The flag key string, e.g. "modules.finance".
                  Must exist in FLAG_REGISTRY (vs_config/constants.py).

    Returns:
        True if the flag is explicitly enabled for this branch, False otherwise.

    Raises:
        ValidationError: If flag_key is not in FLAG_REGISTRY.

    Example:
        if not is_feature_enabled(branch, "modules.finance"):
            raise PermissionDenied({"error_code": "MODULE_NOT_ENABLED"})
    """
    return FlagService.is_enabled(branch, flag_key)
