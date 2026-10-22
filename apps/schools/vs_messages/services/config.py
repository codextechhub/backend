"""The school settings that decide who may write to whom."""
from __future__ import annotations

from dataclasses import dataclass

from ..constants import (
    CFG_FAMILIES_ENABLED,
    CFG_PUPILS_ENABLED,
    CFG_READ_RECEIPTS,
    CFG_SUBJECT_TEACHERS,
)


@dataclass(frozen=True)
class MessagingSettings:
    """The whole-school switches, each defaulting to on.

    ``families_enabled`` and ``pupils_enabled`` stop new family and pupil
    conversations and writing in existing ones; reading is never switched off.
    ``subject_teachers`` lets a family reach every teacher of the child's class
    this year, not only the class teacher and the school office.
    ``read_receipts`` shows each sender whether their messages have been read;
    off, no receipt is computed or served to anybody.
    """

    families_enabled: bool = True
    pupils_enabled: bool = True
    subject_teachers: bool = True
    read_receipts: bool = True


def load_settings(tenant) -> MessagingSettings:
    """Read the settings for *tenant* in one pass."""
    from schools.vs_students.services.rules import resolve_many

    keys = (CFG_FAMILIES_ENABLED, CFG_PUPILS_ENABLED, CFG_SUBJECT_TEACHERS, CFG_READ_RECEIPTS)
    found = resolve_many(keys, tenant=tenant)

    def on(key):
        value, _ = found.get(key, (True, None))
        return value is not False

    return MessagingSettings(
        families_enabled=on(CFG_FAMILIES_ENABLED),
        pupils_enabled=on(CFG_PUPILS_ENABLED),
        subject_teachers=on(CFG_SUBJECT_TEACHERS),
        read_receipts=on(CFG_READ_RECEIPTS),
    )


class LazySettings:
    """The settings of *tenant*, read from the database the first time one is used.

    A staff inbox of groups and direct conversations never needs them, so a
    request that touches no family or pupil conversation costs no setting
    lookups at all. Quacks like :class:`MessagingSettings`.
    """

    def __init__(self, tenant):
        self._tenant = tenant
        self._loaded = None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if self._loaded is None:
            self._loaded = load_settings(self._tenant)
        return getattr(self._loaded, name)
