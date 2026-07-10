from decimal import Decimal, InvalidOperation

from django.db import transaction

from ..constants import BRANCH_SCOPE, PLATFORM_SCOPE, SCHOOL_SCOPE
from ..exceptions import InvalidConfigurationScope, InvalidConfigurationValue
from ..models import ConfigurationDefinition, ConfigurationValue
from .audit import record_configuration_event
from .scopes import normalize_scope, scope_name


def _redacted(definition, value):
    if definition.sensitivity == ConfigurationDefinition.Sensitivity.SECRET_REFERENCE:
        return "[REDACTED]" if value is not None else None
    return value


def validate_value(definition, value):
    kind = definition.value_type
    try:
        if kind in {definition.ValueType.STRING, definition.ValueType.SECRET_REFERENCE}:
            if not isinstance(value, str) or not value.strip():
                raise ValueError
        elif kind == definition.ValueType.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError
        elif kind == definition.ValueType.DECIMAL:
            Decimal(str(value))
        elif kind == definition.ValueType.BOOLEAN and not isinstance(value, bool):
            raise ValueError
        elif kind == definition.ValueType.CHOICE:
            if value not in definition.validation_rules.get("choices", []):
                raise ValueError
        elif kind == definition.ValueType.JSON and not isinstance(value, (dict, list)):
            raise ValueError
    except (ValueError, TypeError, InvalidOperation):
        raise InvalidConfigurationValue(
            f"Value for '{definition.key}' is not a valid {definition.get_value_type_display().lower()}.",
            extra={"key": definition.key},
        )

    rules = definition.validation_rules or {}
    if "min" in rules and value < rules["min"]:
        raise InvalidConfigurationValue(f"Value for '{definition.key}' is below the minimum.")
    if "max" in rules and value > rules["max"]:
        raise InvalidConfigurationValue(f"Value for '{definition.key}' exceeds the maximum.")
    return value


def resolve_value(definition, *, school=None, branch=None):
    school, branch = normalize_scope(school=school, branch=branch)
    scopes = []
    if branch is not None:
        scopes.append(f"branch:{branch.pk}")
    if school is not None:
        scopes.append(f"school:{school.pk}")
    scopes.append("platform")
    rows = {
        row.scope_key: row
        for row in ConfigurationValue.all_objects.filter(
            definition=definition, scope_key__in=scopes
        )
    }
    for key in scopes:
        if key in rows:
            return rows[key].value, rows[key]
    return definition.default_value, None


@transaction.atomic
def set_value(*, definition, value, actor, school=None, branch=None, reason=""):
    school, branch = normalize_scope(school=school, branch=branch)
    requested_scope = scope_name(school, branch)
    if requested_scope not in set(definition.allowed_scopes or []):
        raise InvalidConfigurationScope(
            f"'{definition.key}' cannot be configured at {requested_scope} scope."
        )
    validate_value(definition, value)
    scope_key = (
        f"branch:{branch.pk}" if branch else f"school:{school.pk}" if school else "platform"
    )
    current = ConfigurationValue.all_objects.filter(
        definition=definition, scope_key=scope_key
    ).first()
    before = current.value if current else None
    row, _ = ConfigurationValue.all_objects.update_or_create(
        definition=definition,
        scope_key=scope_key,
        defaults={"school": school, "branch": branch, "value": value, "updated_by": actor},
    )
    record_configuration_event(
        action="config.value.updated",
        target=row,
        actor=actor,
        school=school,
        branch=branch,
        before={"value": _redacted(definition, before)},
        after={"value": _redacted(definition, value)},
        reason=reason,
    )
    return row
