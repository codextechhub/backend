"""Configuration audit events as a reader's Field Access lets them read them.

A configuration audit event keeps the value before and after a change
(``before_data`` and ``after_data``). Some of those values are a registered,
guarded field under another name: a setting stored as a configuration value
that restates a field another module guards with a Field Access switch, or the
subscription dates of a module grant a school's plan wrote. A reader who may
not read the field must not recover it from the trail of its changes.

Two rules decide what a reader gets:

* a **setting field** binds a configuration definition key to the Field Access
  key that guards the same value (:func:`register_setting_field`, called by
  the module that owns the field from its ``AppConfig.ready()``). An event
  about a value of that definition loses its ``value`` snapshot entry for a
  reader who may not read that field;
* the **subscription window** of a grant a plan wrote (``source`` is the
  package) loses ``starts_at`` and ``ends_at`` under the school's
  subscription switches (:mod:`vs_config.entitlement_access`).

Whether a reader may read is Field Access's ordinary answer in the reader's
own tenant, so a CodeX operator is judged for platform fields in CodeX's tenant.
The stored event is never touched: a reader gets a copy. The same filter serves
the event list, the detail and both CSV exports, and a queued export is
filtered as the person who asked for it.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable

from .entitlement_access import EXPIRES_KEY, STARTS_KEY
from .models import CapabilityEntitlement, ConfigurationDefinition, ConfigurationValue

_SETTING_FIELDS: dict[str, str] = {}


def register_setting_field(definition_key: str, field_key: str) -> None:
    """Bind the configuration definition *definition_key* to the Field Access key *field_key*."""
    _SETTING_FIELDS[definition_key] = field_key


def _uuids(values) -> list:
    """The values that are UUIDs, parsed; a target id that is not one names no row."""
    parsed = []
    for value in values:
        try:
            parsed.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return parsed


def setting_keys(events) -> dict[tuple[str, str], str]:
    """``{(target_type, target_id): definition key}`` for the value events in *events*.

    One query per target model however many events there are, and none when no
    definition is bound to a field.
    """
    if not _SETTING_FIELDS:
        return {}
    values, definitions = set(), set()
    for event in events:
        if event.target_type == "ConfigurationValue":
            values.add(str(event.target_id))
        elif event.target_type == "ConfigurationDefinition":
            definitions.add(str(event.target_id))
    keys = {}
    if values:
        for pk, key in ConfigurationValue.all_objects.filter(
            pk__in=_uuids(values),
        ).values_list("pk", "definition__key"):
            keys[("ConfigurationValue", str(pk))] = key
    if definitions:
        for pk, key in ConfigurationDefinition.objects.filter(
            pk__in=_uuids(definitions),
        ).values_list("pk", "key"):
            keys[("ConfigurationDefinition", str(pk))] = key
    return keys


def visible_snapshots(event, can_read: Callable[[str], bool], keys: dict) -> tuple:
    """``(before_data, after_data)`` of *event* without what the reader may not read.

    *keys* is :func:`setting_keys` for the batch the event belongs to.
    """
    snapshots = []
    field_key = _SETTING_FIELDS.get(keys.get((event.target_type, str(event.target_id)), ""))
    hide_value = field_key is not None and not can_read(field_key)
    for snapshot in (event.before_data, event.after_data):
        if not isinstance(snapshot, dict):
            snapshots.append(snapshot)
            continue
        shown = dict(snapshot)
        if hide_value:
            shown.pop("value", None)
        if shown.get("source") == CapabilityEntitlement.Source.PACKAGE:
            if not can_read(STARTS_KEY):
                shown.pop("starts_at", None)
            if not can_read(EXPIRES_KEY):
                shown.pop("ends_at", None)
        snapshots.append(shown)
    return snapshots[0], snapshots[1]
