"""Declare a school's attendance settings.

Four school-scoped definitions are created:

* ``attendance.guardian_notices``: whether submitting a register emails the
  primary guardian of each absent child (default on);
* ``attendance.chronic_threshold``: the attendance rate, 50 to 100 percent,
  below which a student is listed as chronically absent (default 90);
* ``attendance.lesson_marking_levels``: the codes of the levels whose classes
  take a register per lesson as well as one a day (default none);
* ``attendance.edit_window_days``: how many days after its date the person who
  took a register may still change a mark, 0 to 14 (default 0, the same day).

Every default is what a school gets before it chooses, and nothing here changes
a school that has not saved a setting. Declared by the attendance module for
the reason ``vs_calendar`` ``0002`` gives, with the shape
``seed_config_catalogue`` gives them.

Reversible: the definitions and every school's values of them are removed, and
every school reads the defaults again.
"""

from django.db import migrations


SCHOOL = ["platform", "school"]

DEFINITIONS = [
    {
        "key": "attendance.guardian_notices",
        "label": "Absence Notices To Guardians",
        "description": (
            "Whether submitting a register emails the primary guardian of each "
            "absent child. Late and excused children are never emailed."
        ),
        "value_type": "BOOLEAN", "default_value": True,
        "validation_rules": {},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "attendance.chronic_threshold",
        "label": "Chronic Absence Threshold",
        "description": (
            "The attendance rate, as a percentage from 50 to 100, below which "
            "a student is listed as chronically absent."
        ),
        "value_type": "INTEGER", "default_value": 90,
        "validation_rules": {"min": 50, "max": 100},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "attendance.lesson_marking_levels",
        "label": "Levels That Mark Each Lesson",
        "description": (
            "The codes of the levels whose classes take a register for every "
            "lesson as well as one a day. Empty means every class has one "
            "register a day."
        ),
        "value_type": "JSON", "default_value": [],
        "validation_rules": {},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "attendance.edit_window_days",
        "label": "Register Edit Window",
        "description": (
            "How many days after a register's date the person who took it may "
            "still change a mark, 0 to 14. 0 means the same day only. After "
            "the window a mark changes only through a correction request."
        ),
        "value_type": "INTEGER", "default_value": 0,
        "validation_rules": {"min": 0, "max": 14},
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

    The values go first, so an orphan cannot resurrect a school's choice if the
    key is declared again.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key__in=KEYS).delete()
    definition_model.objects.filter(key__in=KEYS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_attendance", "0001_initial"),
        ("vs_config", "0016_held_tolerance_in_naira"),
    ]

    operations = [migrations.RunPython(declare, withdraw)]
