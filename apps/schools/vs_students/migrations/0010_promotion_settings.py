"""Declare a school's promotion settings.

Four school-scoped definitions are created:

* ``students.promotion.suspended``: what the end-of-year promotion does with a
  suspended pupil, HOLD or PROMOTE (default HOLD);
* ``students.promotion.not_placed``: what it does with a pupil who is
  confirmed but not placed, HOLD or PROMOTE (default HOLD);
* ``students.promotion.arms``: which of next year's classes a promoted pupil
  joins, SAME_ARM or SPREAD (default SAME_ARM);
* ``students.promotion.capacity_mode``: what it does with a class it would
  fill past capacity, FOLLOW_ENROLMENT, WARN, HARD or OFF (default
  FOLLOW_ENROLMENT).

**Every default is the behaviour every school had before it could choose**, so
this migration changes nothing for anybody until a school saves a setting.
Declared by the students module for the reason ``0006`` gives, and with the
same shape ``seed_config_catalogue`` gives them.

Reversible: the definitions and every school's values of them are removed, and
every school promotes by the defaults again.
"""

from django.db import migrations


SCHOOL = ["platform", "school"]

DEFINITIONS = [
    {
        "key": "students.promotion.suspended",
        "label": "Suspended Pupils At Promotion",
        "description": (
            "What the end-of-year promotion does with a suspended pupil. HOLD "
            "lists them as an exception and leaves them where they are; "
            "PROMOTE moves them up with their year group, still suspended."
        ),
        "value_type": "CHOICE", "default_value": "HOLD",
        "validation_rules": {"choices": ["HOLD", "PROMOTE"]},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "students.promotion.not_placed",
        "label": "Unplaced Pupils At Promotion",
        "description": (
            "What the end-of-year promotion does with a pupil who is confirmed "
            "but not placed and still holds a class in the year being left. "
            "HOLD leaves them there; PROMOTE moves them up with that class."
        ),
        "value_type": "CHOICE", "default_value": "HOLD",
        "validation_rules": {"choices": ["HOLD", "PROMOTE"]},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "students.promotion.arms",
        "label": "Arms At Promotion",
        "description": (
            "Which of next year's classes a promoted pupil joins. SAME_ARM "
            "keeps an arm together (JSS1 B to JSS2 B); SPREAD shares the "
            "pupils moving into a level evenly across its classes, emptiest "
            "first."
        ),
        "value_type": "CHOICE", "default_value": "SAME_ARM",
        "validation_rules": {"choices": ["SAME_ARM", "SPREAD"]},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "students.promotion.capacity_mode",
        "label": "Class Capacity At Promotion",
        "description": (
            "What the end-of-year promotion does when it would fill a class "
            "past its capacity. FOLLOW_ENROLMENT applies the Class Capacity "
            "Rule; WARN, HARD and OFF mean what they mean there, for the "
            "promotion alone."
        ),
        "value_type": "CHOICE", "default_value": "FOLLOW_ENROLMENT",
        "validation_rules": {
            "choices": ["FOLLOW_ENROLMENT", "WARN", "HARD", "OFF"],
        },
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

    The values go first, for the reason ``0006`` gives: an orphan would
    resurrect a school's choice if the key were declared again.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key__in=KEYS).delete()
    definition_model.objects.filter(key__in=KEYS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_students", "0009_confirm_documents_hold_enrolment"),
    ]

    operations = [migrations.RunPython(declare, withdraw)]
