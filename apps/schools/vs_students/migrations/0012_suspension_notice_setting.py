"""Declare who a school tells when a pupil is suspended.

One school-scoped definition is created, ``students.suspension.notice``:
PRIMARY_GUARDIAN, ALL_GUARDIANS or NOBODY, defaulting to PRIMARY_GUARDIAN.

The default is the narrowest audience that still tells the family, so a school
that has saved nothing writes to the one guardian marked as the pupil's main
contact. NOBODY is reachable only by a school choosing it: the absence of a
stored value is not an instruction to stay silent. Declared by the students
module for the reason ``0006`` gives, and with the same shape
``seed_config_catalogue`` gives it.

Reversible: the definition and every school's value of it are removed, and
every school writes to the primary guardian again.
"""

from django.db import migrations


SCHOOL = ["platform", "school"]

DEFINITIONS = [
    {
        "key": "students.suspension.notice",
        "label": "Who Is Told When A Pupil Is Suspended",
        "description": (
            "Who the school writes to when a pupil is suspended. "
            "PRIMARY_GUARDIAN tells the one guardian marked as the pupil's "
            "main contact; ALL_GUARDIANS tells every guardian on the pupil's "
            "record; NOBODY sends nothing, for a school that tells families "
            "itself. A guardian the school holds no email address and no "
            "account for cannot be written to, and nobody is written to in "
            "their place."
        ),
        "value_type": "CHOICE", "default_value": "PRIMARY_GUARDIAN",
        "validation_rules": {
            "choices": ["PRIMARY_GUARDIAN", "ALL_GUARDIANS", "NOBODY"],
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
    """Remove the definition and every value of it.

    The values go first, for the reason ``0006`` gives: an orphan would
    resurrect a school's choice if the key were declared again.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key__in=KEYS).delete()
    definition_model.objects.filter(key__in=KEYS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_students", "0011_no_server_day_default"),
    ]

    operations = [migrations.RunPython(declare, withdraw)]
