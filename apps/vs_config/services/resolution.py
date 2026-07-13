from decimal import Decimal, InvalidOperation

from django.db import transaction

from ..constants import BRANCH_SCOPE, PLATFORM_SCOPE, SCHOOL_SCOPE
from ..exceptions import InvalidConfigurationScope, InvalidConfigurationValue
from ..models import ConfigurationDefinition, ConfigurationValue
from .audit import record_configuration_event
from .scopes import normalize_scope, scope_name


# Hide secret-reference values from audit payloads and effective-value responses.
def _redacted(definition, value):
    if definition.sensitivity == ConfigurationDefinition.Sensitivity.SECRET_REFERENCE:
        return "[REDACTED]" if value is not None else None
    return value


# Enforce the type and rule contract stored on a configuration definition.
def validate_value(definition, value):
    kind = definition.value_type
    try:
        if kind in {definition.ValueType.STRING, definition.ValueType.SECRET_REFERENCE}:
            # Empty strings are treated as unset because config values drive runtime behavior.
            if not isinstance(value, str) or not value.strip():
                raise ValueError
        elif kind == definition.ValueType.INTEGER:
            # bool is an int subclass in Python, but it is not a valid integer config value.
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError
        elif kind == definition.ValueType.DECIMAL:
            Decimal(str(value))
        elif kind == definition.ValueType.BOOLEAN and not isinstance(value, bool):
            raise ValueError
        elif kind == definition.ValueType.CHOICE:
            # Choices are definition-owned so admins can evolve enumerations without code changes.
            if value not in definition.validation_rules.get("choices", []):
                raise ValueError
        elif kind == definition.ValueType.JSON and not isinstance(value, (dict, list)):
            raise ValueError
    except (ValueError, TypeError, InvalidOperation):
        raise InvalidConfigurationValue(
            f"Value for '{definition.key}' is not a valid {definition.get_value_type_display().lower()}.",
            extra={"key": definition.key},
        )

    # Bounds are definition-specific and run after type coercion succeeds.
    rules = definition.validation_rules or {}
    if "min" in rules or "max" in rules:
        # DECIMAL values may arrive as strings; compare numerically, and turn
        # any type mismatch against the rule bounds into a 422 instead of a 500.
        is_decimal = kind == definition.ValueType.DECIMAL
        comparable = Decimal(str(value)) if is_decimal else value
        try:
            if "min" in rules:
                minimum = Decimal(str(rules["min"])) if is_decimal else rules["min"]
                if comparable < minimum:
                    raise InvalidConfigurationValue(
                        f"Value for '{definition.key}' is below the minimum."
                    )
            if "max" in rules:
                maximum = Decimal(str(rules["max"])) if is_decimal else rules["max"]
                if comparable > maximum:
                    raise InvalidConfigurationValue(
                        f"Value for '{definition.key}' exceeds the maximum."
                    )
        except TypeError:
            raise InvalidConfigurationValue(
                f"Value for '{definition.key}' cannot be compared with its configured bounds."
            )
    return value


# Resolve the effective value using branch, tenant, then platform inheritance.
def resolve_value(definition, *, tenant=None, branch=None):
    tenant, branch = normalize_scope(tenant=tenant, branch=branch)
    # Search scopes from most specific to least specific.
    scopes = []
    if branch is not None:
        scopes.append(f"branch:{branch.pk}")
    if tenant is not None:
        scopes.append(f"tenant:{tenant.pk}")
    scopes.append("platform")
    rows = {
        row.scope_key: row
        for row in ConfigurationValue.all_objects.filter(
            definition=definition, scope_key__in=scopes
        )
    }
    for key in scopes:
        if key in rows:
            # Return the first physical row in inheritance order as the source of truth.
            return rows[key].value, rows[key]
    # No override exists at any layer, so the definition default is effective.
    return definition.default_value, None


# Persist a scoped configuration value and record its redacted audit trail.
@transaction.atomic
def set_value(*, definition, value, actor, tenant=None, branch=None, reason=""):
    tenant, branch = normalize_scope(tenant=tenant, branch=branch)
    requested_scope = scope_name(tenant, branch)
    # Definitions explicitly control which tenant level may override them.
    if requested_scope not in set(definition.allowed_scopes or []):
        raise InvalidConfigurationScope(
            f"'{definition.key}' cannot be configured at {requested_scope} scope."
        )
    validate_value(definition, value)
    # The persisted scope_key mirrors resolve_value's inheritance keys.
    scope_key = (
        f"branch:{branch.pk}" if branch else f"tenant:{tenant.pk}" if tenant else "platform"
    )
    current = ConfigurationValue.all_objects.filter(
        definition=definition, scope_key=scope_key
    ).first()
    # Capture the prior value before overwriting so audit entries show the change.
    before = current.value if current else None
    row, _ = ConfigurationValue.all_objects.update_or_create(
        definition=definition,
        scope_key=scope_key,
        defaults={"tenant": tenant, "branch": branch, "value": value, "updated_by": actor},
    )
    record_configuration_event(
        action="config.value.updated",
        target=row,
        actor=actor,
        tenant=tenant,
        branch=branch,
        before={"value": _redacted(definition, before)},
        after={"value": _redacted(definition, value)},
        reason=reason,
    )
    return row
