"""Declare whether a school gives every guardian the parent app automatically.

One school-scoped definition is created:

* ``guardians.parent_app.auto_access``: whether a guardian is invited to the
  parent app as soon as a child of theirs is on the roll (default yes).

**Unlike the other guardian settings, this default changes behaviour on
purpose.** From this migration on, at every school that has not turned it off,
a new guardian needs an email address, and a guardian linked to a child on the
roll (or whose child joins the roll) is sent an invitation to the parent app.
Guardians the school already holds are not invited by this migration; each is
invited the next time they are linked to a child on the roll, a child of theirs
joins the roll, or an email is added to their record. Declared by the students
module for the reason ``0006`` gives, and with the same shape
``seed_config_catalogue`` gives it.

Reversible: the definition and every school's value of it are removed.
"""

from django.db import migrations


SCHOOL = ["platform", "school"]

DEFINITIONS = [
    {
        "key": "guardians.parent_app.auto_access",
        "label": "Parent App Automatic Access",
        "description": (
            "Whether a guardian is invited to the parent app automatically, "
            "as soon as a child of theirs is on the roll. While it is on, a "
            "new guardian must be given an email address, because that is "
            "where the invitation goes. A guardian whose access was taken "
            "away is never invited again automatically."
        ),
        "value_type": "BOOLEAN", "default_value": True,
        "validation_rules": {}, "allowed_scopes": SCHOOL,
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
    """Remove the definition and every value of it, values first (see ``0006``)."""
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key__in=KEYS).delete()
    definition_model.objects.filter(key__in=KEYS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_students", "0016_guardian_alt_phone"),
    ]

    operations = [
        migrations.RunPython(declare, withdraw),
    ]
