"""Field Access on the finance settings screens, shared by every section.

Every value on Finance settings is a ``finance.settings`` field with its own
Read and Write switch (see :mod:`vs_finance.field_access`). A section's write
is judged by :func:`vs_rbac.field_enforcement.assert_writable` against what
the section stores now, so a value sent back unchanged is not a write, and a
value that does not validate counts as a change and is refused before the
caller is told what is wrong with it.
"""
from __future__ import annotations

from rest_framework.exceptions import ValidationError

from vs_rbac.field_enforcement import UNPARSED

SETTINGS_RESOURCE = "finance.settings"


def plain(value):
    """A choice as its code: an unsaved row holds the member, a stored one the string."""
    return getattr(value, "value", value)


def stored_values(record, fields) -> dict:
    """The named settings of *record* as its section's update stores them."""
    return {field: plain(getattr(record, field)) for field in fields}


def normalised_submission(data, fields, validate) -> dict:
    """Each submitted setting as ``validate({name: value})`` would store it.

    A value that does not validate becomes ``UNPARSED``. A name that is not one
    of *fields* is passed on untouched for the section's own update to refuse.
    """
    submitted = {}
    for name, value in (data or {}).items():
        if name not in fields:
            submitted[name] = value
            continue
        try:
            submitted[name] = validate({name: value})[name]
        except ValidationError:
            submitted[name] = UNPARSED
    return submitted
