"""Declare a school's guardian settings, and let a link carry the school's own relationship.

Four school-scoped definitions are created:

* ``guardians.min_per_student``: how many guardians every child needs
  (default 1);
* ``guardians.email_required``: whether a new guardian needs an email address
  (default no);
* ``guardians.matching``: how a guardian typed in is recognised as one the
  school already holds, EMAIL_THEN_PHONE or EMAIL_ONLY (default
  EMAIL_THEN_PHONE);
* ``guardians.relationships.extra``: relationships the school adds to the
  fixed eight, such as Sponsor or Driver (default none).

``StudentGuardian.relationship_detail`` holds one of those added
relationships on a link, beside ``relationship = OTHER``. It is blank on every
existing link, which reads exactly as before.

**Every default is the behaviour every school had before it could choose**, so
this migration changes nothing for anybody until a school saves a setting.
Declared by the students module for the reason ``0006`` gives, and with the
same shape ``seed_config_catalogue`` gives them.

Reversible: the definitions and every school's values of them are removed, and
the column is dropped, which loses the added relationship on any link that
carries one (the link itself stays, as Other).
"""

from django.db import migrations, models


SCHOOL = ["platform", "school"]

DEFINITIONS = [
    {
        "key": "guardians.min_per_student",
        "label": "Guardians Per Student",
        "description": (
            "How many guardians every child at this school needs. Enrolment "
            "and saving an applicant refuse fewer, and a guardian cannot be "
            "removed from a child on the roll if that would leave fewer. The "
            "student import still imports its one guardian per child, with a "
            "warning."
        ),
        "value_type": "INTEGER", "default_value": 1,
        "validation_rules": {"min": 1, "max": 4}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "guardians.email_required",
        "label": "Guardian Email Required",
        "description": (
            "Whether a new guardian must be given an email address, on the "
            "enrolment form, when linking, and in both imports, and whether "
            "an edit may blank one. A guardian already held with no email can "
            "still be linked to another child."
        ),
        "value_type": "BOOLEAN", "default_value": False,
        "validation_rules": {}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "guardians.matching",
        "label": "Guardian Matching",
        "description": (
            "How a guardian typed in is recognised as one this school already "
            "holds. EMAIL_THEN_PHONE matches on email, then on phone; "
            "EMAIL_ONLY never matches on phone, for a school whose families "
            "share landlines."
        ),
        "value_type": "CHOICE", "default_value": "EMAIL_THEN_PHONE",
        "validation_rules": {"choices": ["EMAIL_THEN_PHONE", "EMAIL_ONLY"]},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "guardians.relationships.extra",
        "label": "Additional Guardian Relationships",
        "description": (
            "Relationships this school records beyond the fixed eight, such as "
            "Sponsor or Driver: up to 10, each up to 30 characters. A link "
            "stores one as Other with the school's label, and removing it from "
            "this list leaves those links as they are."
        ),
        "value_type": "JSON", "default_value": [],
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
        ("vs_students", "0006_student_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="studentguardian",
            name="relationship_detail",
            field=models.CharField(blank=True, default="", max_length=30),
        ),
        migrations.RunPython(declare, withdraw),
    ]
