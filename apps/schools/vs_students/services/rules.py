"""A school's own enrolment rules: ages, documents, required fields, capacity.

Six ``vs_config`` definitions, all school-scoped, all read through
``resolve_value`` so a school's value, the platform's and the definition's
default are inherited exactly as every other setting is. Each default is the
behaviour every school had before it could choose, so a school that has set
nothing is treated exactly as before:

* a child is 2 to 25 years old (``ages.py``);
* the birth certificate is the one document prompted for;
* no optional field is required;
* a full class is refused until the person placing the child says they mean it
  (``WARN``);
* a new class with no capacity has no limit.

A value stored by hand at the platform layer can be anything the definition's
type allows, so every read is cleaned here rather than trusted: an unknown
document type or field is dropped, an unknown capacity mode reads as WARN, and
an age range that is upside down reads as the default. A bad stored value then
costs a school its own rule, never an enrolment.

Writes go through ``set_value`` and ``clear_value``, which check the
definition's scope and record ``config.value.updated`` and
``config.value.cleared`` in the audit trail like every other setting. A value
already in force is not written again, so saving the screen unchanged leaves
no audit rows behind.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from ..constants import (
    AGE_RULE_CEILING,
    AGE_RULE_FLOOR,
    CFG_AGE_MAX,
    CFG_AGE_MIN,
    CFG_CAPACITY_DEFAULT,
    CFG_CAPACITY_MODE,
    CFG_REQUIRED_DOCUMENTS,
    CFG_REQUIRED_FIELDS,
    DEFAULT_CAPACITY_MAX,
    DEFAULT_CAPACITY_MIN,
    REQUIRABLE_FIELDS,
    REQUIRED_DOCUMENTS,
    CapacityMode,
    DocumentType,
)
from ..exceptions import StudentSettingNotRegistered

#: The age range a school has until it sets its own. See ``ages.py``.
DEFAULT_MIN_AGE = 2
DEFAULT_MAX_AGE = 25

_RULE_KEYS = (
    CFG_AGE_MIN, CFG_AGE_MAX, CFG_REQUIRED_DOCUMENTS, CFG_REQUIRED_FIELDS,
    CFG_CAPACITY_MODE, CFG_CAPACITY_DEFAULT,
)


def resolve_many(keys, *, tenant, branch=None):
    """``{key: (value, row)}`` for each active definition among *keys*.

    One query for the definitions and one per key through ``resolve_value``,
    so the inheritance order is vs_config's own rather than a copy of it. A key
    with no active definition is absent from the answer; ``row`` is the
    ``ConfigurationValue`` that won, or None when the default applied.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import resolve_value

    definitions = ConfigurationDefinition.objects.filter(
        key__in=list(keys), is_active=True,
    )
    return {
        definition.key: resolve_value(definition, tenant=tenant, branch=branch)
        for definition in definitions
    }


def _int_or(value, fallback):
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def _documents(value) -> tuple:
    """The stored list, cleaned to known types, in the checklist's own order."""
    if not isinstance(value, (list, tuple)):
        return tuple(sorted(REQUIRED_DOCUMENTS))
    wanted = {str(v) for v in value}
    return tuple(v for v in DocumentType.values if v in wanted)


def _fields(value) -> tuple:
    """The stored list, cleaned to requirable fields, in the form's own order."""
    if not isinstance(value, (list, tuple)):
        return ()
    wanted = {str(v) for v in value}
    return tuple(f for f in REQUIRABLE_FIELDS if f in wanted)


def _mode(value) -> str:
    return str(value) if value in CapacityMode.values else CapacityMode.WARN.value


def _capacity(value):
    value = _int_or(value, None)
    if value is None or not DEFAULT_CAPACITY_MIN <= value <= DEFAULT_CAPACITY_MAX:
        return None
    return value


def _ages(minimum, maximum) -> tuple[int, int]:
    minimum = _int_or(minimum, DEFAULT_MIN_AGE)
    maximum = _int_or(maximum, DEFAULT_MAX_AGE)
    if not AGE_RULE_FLOOR <= minimum < maximum <= AGE_RULE_CEILING:
        return DEFAULT_MIN_AGE, DEFAULT_MAX_AGE
    return minimum, maximum


def _values(tenant, keys) -> dict:
    return {key: value for key, (value, _) in resolve_many(keys, tenant=tenant).items()}


def age_bounds(tenant) -> tuple[int, int]:
    """(youngest, oldest) in whole years, inclusive."""
    found = _values(tenant, (CFG_AGE_MIN, CFG_AGE_MAX))
    return _ages(
        found.get(CFG_AGE_MIN, DEFAULT_MIN_AGE),
        found.get(CFG_AGE_MAX, DEFAULT_MAX_AGE),
    )


def required_documents(tenant) -> tuple:
    found = _values(tenant, (CFG_REQUIRED_DOCUMENTS,))
    return _documents(found.get(CFG_REQUIRED_DOCUMENTS, sorted(REQUIRED_DOCUMENTS)))


def required_fields(tenant) -> tuple:
    found = _values(tenant, (CFG_REQUIRED_FIELDS,))
    return _fields(found.get(CFG_REQUIRED_FIELDS, []))


def capacity_mode(tenant) -> str:
    found = _values(tenant, (CFG_CAPACITY_MODE,))
    return _mode(found.get(CFG_CAPACITY_MODE, CapacityMode.WARN))


def default_capacity(tenant):
    """Seats a new class gets when it is created without a capacity, or None."""
    found = _values(tenant, (CFG_CAPACITY_DEFAULT,))
    return _capacity(found.get(CFG_CAPACITY_DEFAULT))


@dataclass(frozen=True)
class EnrolmentRules:
    min_age_years: int = DEFAULT_MIN_AGE
    max_age_years: int = DEFAULT_MAX_AGE
    required_documents: tuple = tuple(sorted(REQUIRED_DOCUMENTS))
    required_fields: tuple = ()
    capacity_mode: str = CapacityMode.WARN.value
    default_capacity: int | None = None

    def as_dict(self) -> dict:
        """The body of ``GET /v1/students/enrolment-rules/``.

        ``document_types`` and ``optional_fields`` are the choices the screen
        offers, so it never carries its own copy of either list.
        """
        return {
            "min_age_years": self.min_age_years,
            "max_age_years": self.max_age_years,
            "required_documents": list(self.required_documents),
            "document_types": [
                {"value": value, "label": label}
                for value, label in DocumentType.choices
            ],
            "required_fields": list(self.required_fields),
            "optional_fields": [
                {"value": value, "label": label}
                for value, label in REQUIRABLE_FIELDS.items()
            ],
            "capacity_mode": self.capacity_mode,
            "default_capacity": self.default_capacity,
        }


def read_rules(tenant) -> EnrolmentRules:
    """All six rules in one read: seven queries whatever the school."""
    found = _values(tenant, _RULE_KEYS)
    minimum, maximum = _ages(
        found.get(CFG_AGE_MIN, DEFAULT_MIN_AGE),
        found.get(CFG_AGE_MAX, DEFAULT_MAX_AGE),
    )
    return EnrolmentRules(
        min_age_years=minimum,
        max_age_years=maximum,
        required_documents=_documents(
            found.get(CFG_REQUIRED_DOCUMENTS, sorted(REQUIRED_DOCUMENTS)),
        ),
        required_fields=_fields(found.get(CFG_REQUIRED_FIELDS, [])),
        capacity_mode=_mode(found.get(CFG_CAPACITY_MODE, CapacityMode.WARN)),
        default_capacity=_capacity(found.get(CFG_CAPACITY_DEFAULT)),
    )


@transaction.atomic
def write_rules(
    tenant, actor, *, min_age_years, max_age_years, required_documents,
    required_fields, capacity_mode, default_capacity, reason="",
) -> EnrolmentRules:
    """Store the school's rules. The caller has already validated them.

    A null default capacity removes the school's value rather than storing
    one, because vs_config holds no null: the value above the school's then
    applies, which is no limit unless the platform has set one. Lists are stored in the order the screen offers
    them, so the same choice is always the same stored value.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import clear_value, resolve_value, set_value

    wanted = {
        CFG_AGE_MIN: min_age_years,
        CFG_AGE_MAX: max_age_years,
        CFG_REQUIRED_DOCUMENTS: list(_documents(required_documents)),
        CFG_REQUIRED_FIELDS: list(_fields(required_fields)),
        CFG_CAPACITY_MODE: capacity_mode,
        CFG_CAPACITY_DEFAULT: default_capacity,
    }
    definitions = {
        d.key: d for d in ConfigurationDefinition.objects.filter(
            key__in=list(wanted), is_active=True,
        )
    }
    for key in wanted:
        if key not in definitions:
            raise StudentSettingNotRegistered(key=key)

    why = (reason or "").strip() or "Enrolment rules set from Student settings."
    for key, value in wanted.items():
        definition = definitions[key]
        current, row = resolve_value(definition, tenant=tenant)
        if value is None:
            if row is not None and row.scope_key == f"tenant:{tenant.pk}":
                clear_value(
                    definition=definition, actor=actor, tenant=tenant, reason=why,
                )
            continue
        if current == value:
            continue
        set_value(
            definition=definition, value=value, actor=actor, tenant=tenant,
            reason=why,
        )
    return read_rules(tenant)
