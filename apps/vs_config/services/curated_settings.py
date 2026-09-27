"""Saving a curated settings form: several fixed keys written as one change.

A curated form (Security Settings, Integration Settings) is a small set of named
fields, each mapped to one configuration key. The form is saved field by field
through :func:`~vs_config.services.resolution.set_value` and
:func:`~vs_config.services.resolution.clear_value`, so every field keeps the
definition's own validation, its write guard and its audit event. What this
module adds is the mapping from field names to keys, ``None`` meaning "reset to
the parent", and a per-field refusal keyed on the form's own field name.

More than one endpoint saves the security form - the platform's configuration
console and a tenant's own settings screen - and both must apply exactly the
same rules, so the save lives here rather than in either view.
"""
from django.db import transaction
from rest_framework.exceptions import ValidationError

from ..models import ConfigurationDefinition
from ..runtime_settings import (
    SECURITY_FIELDS,
    resolve_security_settings,
    validate_security_compliance,
)
from .resolution import clear_value, set_value


@transaction.atomic
def save_curated_values(
    *, field_map, validated_data, actor, reason, tenant=None, branch=None,
    compliance_validator=None,
):
    """Write every mapped field present in *validated_data* at one scope.

    ``None`` clears that scope's value, so the field falls back to its parent
    layer. A field refused by *compliance_validator* raises a ``ValidationError``
    keyed on the field name, and the atomic block discards the fields already
    written, so a form is saved whole or not at all.
    """
    submitted = {
        field_map[field]: value
        for field, value in validated_data.items()
        if field in field_map
    }
    definitions = {
        item.key: item
        for item in ConfigurationDefinition.objects.filter(key__in=submitted, is_active=True)
    }
    missing = sorted(set(submitted) - set(definitions))
    if missing:
        raise ValidationError({"settings": f"Missing definitions: {', '.join(missing)}."})
    for key, value in submitted.items():
        if value is None:
            clear_value(
                definition=definitions[key], actor=actor, tenant=tenant,
                branch=branch, reason=reason,
            )
        else:
            if compliance_validator:
                field = next(name for name, mapped_key in field_map.items() if mapped_key == key)
                try:
                    compliance_validator(field, value, tenant=tenant, branch=branch)
                except ValueError as exc:
                    raise ValidationError({field: str(exc)})
            set_value(
                definition=definitions[key], value=value, actor=actor,
                tenant=tenant, branch=branch, reason=reason,
            )


def save_security_settings(*, validated_data, actor, reason, tenant=None, branch=None):
    """Save the security form at one scope and return the refreshed settings.

    A tenant or branch value may only be as strict as its parent, or stricter:
    :func:`~vs_config.runtime_settings.validate_security_compliance` refuses a
    weaker one with a field-keyed 400. The return value is
    :func:`~vs_config.runtime_settings.resolve_security_settings` for the same
    scope, which is what every caller answers with.
    """
    save_curated_values(
        field_map=SECURITY_FIELDS,
        validated_data=validated_data,
        actor=actor,
        reason=reason,
        tenant=tenant,
        branch=branch,
        compliance_validator=validate_security_compliance,
    )
    return resolve_security_settings(tenant=tenant, branch=branch)
