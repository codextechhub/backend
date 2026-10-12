"""The attendance screens and settings the search bar can route to.

Attendance answers to the ``attendance`` capability (:mod:`vs_rbac.capability_map`).
Each place takes the key its screen's read endpoint requires: the registers,
the settings and the reasons for excused absences read with
``attendance.register.view``, corrections with either correction key and the
reports with ``attendance.report.view``.

Each field of the settings screen is a ``vs_config`` definition, labelled in
its own words and anchored at the field name the screen's endpoint uses
(:data:`~.services.configuration.KEY_FOR_FIELD`), so a setting added to the
screen is searchable with it.
"""
from __future__ import annotations

from vs_config.search_places import definition_setting_places
from vs_search.registry import ACTION, Place, register, register_contributor

from .constants import (
    PERM_CORRECTION_APPROVE,
    PERM_CORRECTION_CREATE,
    PERM_REGISTER_VIEW,
    PERM_REPORT_VIEW,
)

CAPABILITY = "attendance"
SETTINGS_SCREEN = "attendance.settings"
SETTINGS_GROUP = "Attendance settings"


def setting_places():
    """Each setting on the attendance settings screen."""
    from .services.configuration import KEY_FOR_FIELD

    return definition_setting_places(
        SETTINGS_SCREEN, SETTINGS_GROUP, KEY_FOR_FIELD,
        permission=PERM_REGISTER_VIEW, capability=CAPABILITY,
        keywords=("attendance",),
    )


register_contributor(setting_places)

register(
    Place(
        kind=ACTION, key="attendance.today", label="Today's registers",
        description="Every class's register for today and which are still unmarked.",
        keywords=("attendance", "register", "roll call"),
        screen="attendance.today", permission=PERM_REGISTER_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="attendance.my_registers", label="My registers",
        description="The registers you mark today.",
        keywords=("attendance", "mark register", "roll call"),
        screen="attendance.my_registers", permission=PERM_REGISTER_VIEW,
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="attendance.corrections", label="Attendance corrections",
        description="Requests to change a mark on a closed register.",
        keywords=("attendance", "correct a mark"),
        screen="attendance.corrections",
        permission=(PERM_CORRECTION_APPROVE, PERM_CORRECTION_CREATE), capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="attendance.reports", label="Attendance reports",
        description="Attendance rates by class, pupil and term, and chronic absence.",
        keywords=("attendance", "absence", "absent"),
        screen="attendance.reports", permission=PERM_REPORT_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="attendance.reason_codes", label="Reasons for excused absences",
        description="The reasons a register may give for an excused absence.",
        keywords=("attendance", "absence reasons", "excused"),
        screen="attendance.reason_codes", permission=PERM_REGISTER_VIEW,
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key=SETTINGS_SCREEN, label=SETTINGS_GROUP,
        description="Absence notices, the chronic absence threshold, lesson marking "
                    "and the register edit window.",
        keywords=("attendance",),
        screen=SETTINGS_SCREEN, permission=PERM_REGISTER_VIEW, capability=CAPABILITY,
    ),
)
