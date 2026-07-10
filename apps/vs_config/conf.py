"""Public read API for configuration and capability evaluation."""
from .models import Capability, ConfigurationDefinition
from .services.capabilities import effective_capability
from .services.resolution import resolve_value

__all__ = ["get_config", "is_capability_enabled"]


def get_config(key, default=None, *, school=None, branch=None):
    definition = ConfigurationDefinition.objects.filter(key=key, is_active=True).first()
    if definition is None:
        return default
    value, _ = resolve_value(definition, school=school, branch=branch)
    return default if value is None else value


def is_capability_enabled(key, *, school=None, branch=None):
    capability = Capability.objects.filter(key=key, is_active=True).first()
    if capability is None:
        return False
    return effective_capability(capability, school=school, branch=branch)
