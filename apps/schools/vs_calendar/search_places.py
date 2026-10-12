"""The calendar, timetable and exam screens and settings the search bar can route to.

Their permissions sit in the ``academics`` module, which is core
(:mod:`vs_rbac.capability_map`), so no place here names a capability. The
settings screen opens to ``school.settings.view``, the key the school's
settings area asks, and each of its fields is a ``vs_config`` definition
labelled in its own words.
"""
from __future__ import annotations

from vs_config.search_places import definition_setting_places
from vs_search.registry import ACTION, Place, register, register_contributor

from .constants import (
    CFG_CLOSES_SCHOOL_BY_TYPE,
    CFG_DEFAULT_PERIOD_MINUTES,
    CFG_INVIGILATOR_ROLES,
    CFG_ROOM_REQUIRED_TO_PUBLISH,
    CFG_TEACHER_DUTY_MATCH,
    CFG_TEACHING_DAYS,
    CFG_WEEK_STARTS_ON,
)

SETTINGS_SCREEN = "calendar.settings"
SETTINGS_GROUP = "Calendar and timetable settings"
SETTINGS_VIEW = "school.settings.view"
CALENDAR_VIEW = "academics.calendar.view"
TIMETABLE_VIEW = "academics.timetable.view"


def setting_places():
    """Each setting on the calendar and timetable settings screen."""
    return definition_setting_places(
        SETTINGS_SCREEN, SETTINGS_GROUP,
        {"teaching_days": CFG_TEACHING_DAYS, "week_starts_on": CFG_WEEK_STARTS_ON,
         "closes_school_by_type": CFG_CLOSES_SCHOOL_BY_TYPE,
         "room_required_to_publish": CFG_ROOM_REQUIRED_TO_PUBLISH,
         "teacher_duty_match": CFG_TEACHER_DUTY_MATCH,
         "invigilator_roles": CFG_INVIGILATOR_ROLES,
         "default_period_minutes": CFG_DEFAULT_PERIOD_MINUTES},
        permission=SETTINGS_VIEW, keywords=("school settings",),
    )


register_contributor(setting_places)

register(
    Place(
        kind=ACTION, key="calendar.events", label="School calendar",
        description="Holidays, events and term dates.", keywords=("holidays", "events"),
        screen="calendar.events", permission=CALENDAR_VIEW,
    ),
    Place(
        kind=ACTION, key="calendar.term_view", label="Term calendar",
        description="The current term week by week.", keywords=("term dates", "weeks"),
        screen="calendar.term_view", permission=CALENDAR_VIEW,
    ),
    Place(
        kind=ACTION, key="calendar.timetable", label="Class timetables",
        description="Each class's week of lessons.", keywords=("timetable", "lessons"),
        screen="calendar.timetable", permission=TIMETABLE_VIEW,
    ),
    Place(
        kind=ACTION, key="calendar.teacher_timetables", label="Teacher timetables",
        description="Each teacher's week of lessons.",
        screen="calendar.teacher_timetables", permission=TIMETABLE_VIEW,
    ),
    Place(
        kind=ACTION, key="calendar.rooms", label="Rooms",
        description="Classrooms, labs and halls lessons are held in.",
        screen="calendar.rooms", permission=TIMETABLE_VIEW,
    ),
    Place(
        kind=ACTION, key="calendar.bell_schedule", label="Bell schedule",
        description="The periods of the school day.", keywords=("periods", "bells"),
        screen="calendar.bell_schedule", permission=TIMETABLE_VIEW,
    ),
    Place(
        kind=ACTION, key="calendar.exams", label="Exams",
        screen="calendar.exams", permission="academics.exam.view",
    ),
    Place(
        kind=ACTION, key=SETTINGS_SCREEN, label=SETTINGS_GROUP,
        description="Teaching days, the start of the week, closures, lesson rules "
                    "and invigilators.",
        keywords=("school settings", "timetable"),
        screen=SETTINGS_SCREEN, permission=SETTINGS_VIEW,
    ),
)
