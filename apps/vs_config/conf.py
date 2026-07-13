"""Public read API for configuration and capability evaluation."""
from .models import Capability, ConfigurationDefinition
from .services.capabilities import effective_capability
from .services.resolution import resolve_value

__all__ = ["get_config", "is_capability_enabled"]


# Read the effective configuration value for callers that should not know resolution internals.
def get_config(key, default=None, *, tenant=None, branch=None):
    definition = ConfigurationDefinition.objects.filter(key=key, is_active=True).first()
    if definition is None:
        return default
    # Reuse the same inheritance path as the API so internal callers see identical values.
    value, _ = resolve_value(definition, tenant=tenant, branch=branch)
    return default if value is None else value


# Expose capability gates as a boolean API for feature checks across modules.
def is_capability_enabled(key, *, tenant=None, branch=None):
    capability = Capability.objects.filter(key=key, is_active=True).first()
    if capability is None:
        return False
    # Unknown or inactive gates fail closed so callers do not accidentally expose features.
    return effective_capability(capability, tenant=tenant, branch=branch)
