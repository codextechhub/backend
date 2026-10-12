"""A school's attendance settings.

Four ``vs_config`` definitions, all school-scoped, read through ``resolve_many``
so a school's value, the platform's and the definition's default are inherited
exactly as every other setting is:

* ``attendance.guardian_notices``: whether submitting a register emails the
  primary guardian of each absent child. Default on.
* ``attendance.chronic_threshold``: the attendance rate, as a percentage from
  50 to 100, below which a student is listed as chronically absent. Default 90.
* ``attendance.lesson_marking_levels``: the codes of the levels whose classes
  take a register per lesson as well as one a day. Empty by default. Stored
  by level code because a ``Level`` row belongs to one school year, and a
  switch on the row would reset every year.
* ``attendance.edit_window_days``: how many days after a register's date the
  person who took it may still change a mark, 0 to 14. Default 0, which is the
  same day only. Once the window has passed a mark changes only through a
  correction request.

A value stored by hand at the platform layer can be anything its type allows,
so every read is cleaned here rather than trusted: a value that breaks a rule
the settings screen enforces reads as the default. A bad stored value then
costs a school its own choice, never a register.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..constants import (
    CFG_CHRONIC_THRESHOLD,
    CFG_EDIT_WINDOW_DAYS,
    CFG_GUARDIAN_NOTICES,
    CFG_LESSON_MARKING_LEVELS,
    CHRONIC_THRESHOLD_MAX,
    CHRONIC_THRESHOLD_MIN,
    DEFAULT_CHRONIC_THRESHOLD,
    DEFAULT_EDIT_WINDOW_DAYS,
    DEFAULT_GUARDIAN_NOTICES,
    EDIT_WINDOW_MAX,
    EDIT_WINDOW_MIN,
)

RULE_KEYS = (
    CFG_GUARDIAN_NOTICES,
    CFG_CHRONIC_THRESHOLD,
    CFG_LESSON_MARKING_LEVELS,
    CFG_EDIT_WINDOW_DAYS,
)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_threshold(value) -> int:
    if _is_int(value) and CHRONIC_THRESHOLD_MIN <= value <= CHRONIC_THRESHOLD_MAX:
        return value
    return DEFAULT_CHRONIC_THRESHOLD


def _clean_window(value) -> int:
    if _is_int(value) and EDIT_WINDOW_MIN <= value <= EDIT_WINDOW_MAX:
        return value
    return DEFAULT_EDIT_WINDOW_DAYS


def _clean_levels(value) -> tuple:
    if not isinstance(value, (list, tuple)):
        return ()
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return ()
        if item.strip() not in out:
            out.append(item.strip())
    return tuple(out)


@dataclass(frozen=True)
class AttendanceRules:
    """The four settings, cleaned, with every default a school starts on."""

    guardian_notices: bool = DEFAULT_GUARDIAN_NOTICES
    chronic_threshold: int = DEFAULT_CHRONIC_THRESHOLD
    lesson_marking_levels: tuple = ()
    edit_window_days: int = DEFAULT_EDIT_WINDOW_DAYS


def read_attendance_rules(tenant, keys=RULE_KEYS) -> AttendanceRules:
    """The settings named in *keys*, cleaned; every other one at its default.

    One query for the definitions and one per key, so a caller needing one
    setting asks for that one. With no tenant, the defaults.
    """
    if tenant is None:
        return AttendanceRules()
    from schools.vs_academics.services.academic_rules import resolve_many

    found = resolve_many(keys, tenant=tenant)
    values = {}
    if CFG_GUARDIAN_NOTICES in keys:
        stored = found.get(CFG_GUARDIAN_NOTICES)
        values["guardian_notices"] = (
            stored if isinstance(stored, bool) else DEFAULT_GUARDIAN_NOTICES
        )
    if CFG_CHRONIC_THRESHOLD in keys:
        values["chronic_threshold"] = _clean_threshold(found.get(CFG_CHRONIC_THRESHOLD))
    if CFG_LESSON_MARKING_LEVELS in keys:
        values["lesson_marking_levels"] = _clean_levels(
            found.get(CFG_LESSON_MARKING_LEVELS),
        )
    if CFG_EDIT_WINDOW_DAYS in keys:
        values["edit_window_days"] = _clean_window(found.get(CFG_EDIT_WINDOW_DAYS))
    return AttendanceRules(**values)


def read_guardian_notices(tenant) -> bool:
    return read_attendance_rules(tenant, (CFG_GUARDIAN_NOTICES,)).guardian_notices


def read_edit_window_days(tenant) -> int:
    return read_attendance_rules(tenant, (CFG_EDIT_WINDOW_DAYS,)).edit_window_days
