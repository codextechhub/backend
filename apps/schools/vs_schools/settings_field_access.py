"""Field Access for school setting bodies assembled by several school apps.

Each view names the setting keys it owns. A full PUT can merge omitted values
from the current rules before validation, while this module judges only keys
the caller submitted. Response metadata names visible values the caller may
read but may not change.
"""
from __future__ import annotations

from collections.abc import Mapping

from vs_rbac.field_enforcement import assert_writable, can_read
from vs_rbac.field_evaluator import get_acting_field_access

RESOURCE = "school.settings"


def settings_body(request, body: Mapping, fields: Mapping[str, str]) -> dict:
    """Remove hidden values and name the visible settings this caller cannot write."""
    shown = dict(body)
    access = get_acting_field_access(request.user, tenant=getattr(request, "tenant", None))
    read_only = []
    for name, field in fields.items():
        if name not in shown:
            continue
        key = f"{RESOURCE}.{field}"
        if not can_read(request, key):
            shown.pop(name)
        elif not access.can_write(key):
            read_only.append(name)
    if read_only:
        shown["_read_only_fields"] = sorted(read_only)
    return shown


def assert_settings_write(
    request, submitted: Mapping, fields: Mapping[str, str], *, current: Mapping | None = None,
) -> None:
    """Judge submitted values under their setting switches before any save."""
    aliases = {name: field for name, field in fields.items() if name != field}
    assert_writable(
        request, RESOURCE, submitted, aliases=aliases, current=current,
    )
