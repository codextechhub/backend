"""Declare a school's calendar and timetable settings.

Seven school-scoped definitions are created:

* ``calendar.teaching_days``: the ISO weekdays the school teaches (default
  Monday to Friday);
* ``calendar.week_starts_on``: 1 (Monday, the default) or 7 (Sunday);
* ``calendar.closes_school_by_type``: whether each kind of calendar entry
  closes the school when it is created without saying (default: a public
  holiday and the half-term break do, the other four do not);
* ``timetable.room_required_to_publish``: whether a lesson needs a room before
  its class timetable can be published (default true);
* ``timetable.teacher_duty_match``: OFF, WARN or REFUSE for a lesson whose
  teacher holds no teaching duty for it (default OFF);
* ``exams.invigilator_roles``: the role keys whose holders may invigilate
  (default ``["teacher"]``);
* ``timetable.default_period_minutes``: 10 to 240, or null for no default (the
  default).

**Every default but one is what a school already had**, so this migration
changes nothing for anybody until a school saves a setting. The exception is
the closes-school default, which only fills in an event created without
saying and never touches an event that exists. Declared by the calendar module
for the reason ``vs_students`` ``0006`` gives, and with the same shape
``seed_config_catalogue`` gives them.

Reversible: the definitions and every school's values of them are removed, and
every school reads the defaults again.
"""

from django.db import migrations


SCHOOL = ["platform", "school"]

DEFINITIONS = [
    {
        "key": "calendar.teaching_days",
        "label": "Teaching Days",
        "description": (
            "The weekdays the school teaches, as ISO numbers (Monday is 1, "
            "Sunday is 7), at least one. They are the day columns of every "
            "timetable, the days a period or a lesson may be placed on, and "
            "the days the calendar counts as taught."
        ),
        "value_type": "JSON", "default_value": [1, 2, 3, 4, 5],
        "validation_rules": {},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "calendar.week_starts_on",
        "label": "Week Starts On",
        "description": (
            "The day the school's week starts on: 1 for Monday, 7 for Sunday. "
            "Calendars and timetables start their week on it."
        ),
        "value_type": "CHOICE", "default_value": 1,
        "validation_rules": {"choices": [1, 7]},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "calendar.closes_school_by_type",
        "label": "Entries That Close The School",
        "description": (
            "For each kind of calendar entry, whether an entry of that kind "
            "closes the school when it is created without saying. An existing "
            "entry never changes."
        ),
        "value_type": "JSON",
        "default_value": {
            "HOLIDAY": True, "MIDTERM_BREAK": True, "EXAM_PERIOD": False,
            "SCHOOL_EVENT": False, "PTA": False, "SPORTS": False,
        },
        "validation_rules": {},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "timetable.room_required_to_publish",
        "label": "Lessons Need A Room To Publish",
        "description": (
            "Whether every lesson needs a room before a class timetable can "
            "be published. A lesson always needs a teacher."
        ),
        "value_type": "BOOLEAN", "default_value": True,
        "validation_rules": {},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "timetable.teacher_duty_match",
        "label": "Teacher Must Hold The Teaching Duty",
        "description": (
            "What happens to a lesson whose teacher has no teaching duty for "
            "its class and subject: OFF allows it, WARN saves it with a "
            "warning, REFUSE refuses it and blocks publishing while one "
            "remains."
        ),
        "value_type": "CHOICE", "default_value": "OFF",
        "validation_rules": {"choices": ["OFF", "WARN", "REFUSE"]},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "exams.invigilator_roles",
        "label": "Roles That May Invigilate",
        "description": (
            "The keys of the school's roles whose active holders may "
            "invigilate an exam paper."
        ),
        "value_type": "JSON", "default_value": ["teacher"],
        "validation_rules": {},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "timetable.default_period_minutes",
        "label": "Default Period Length",
        "description": (
            "The minutes a new period lasts unless its end time is changed, "
            "10 to 240. Empty means no default."
        ),
        "value_type": "INTEGER", "default_value": None,
        "validation_rules": {"min": 10, "max": 240},
        "allowed_scopes": SCHOOL,
    },
]

KEYS = [row["key"] for row in DEFINITIONS]


def declare(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    for row in DEFINITIONS:
        fields = {k: v for k, v in row.items() if k != "key"}
        definition_model.objects.get_or_create(
            key=row["key"],
            defaults={**fields, "sensitivity": "INTERNAL", "is_active": True},
        )


def withdraw(apps, schema_editor):
    """Remove the definitions and every value of them.

    The values go first, for the reason ``vs_students`` ``0006`` gives: an
    orphan would resurrect a school's choice if the key were declared again.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key__in=KEYS).delete()
    definition_model.objects.filter(key__in=KEYS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_calendar", "0001_initial"),
        ("vs_config", "0013_seed_display_timezone"),
    ]

    operations = [migrations.RunPython(declare, withdraw)]
