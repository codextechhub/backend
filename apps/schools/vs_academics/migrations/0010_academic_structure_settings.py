"""Declare a school's academic structure settings.

Three school-scoped definitions are created:

* ``academics.terms.word``: what the school calls the parts of its year, TERM
  or SEMESTER (default null, which reads as the school's term structure:
  SEMESTER on ``2_SEMESTERS``, TERM otherwise);
* ``academics.terms.names``: the ordered names a new year's terms are given,
  one to six (default null, which reads as the term structure's names);
* ``academics.classes.default_arms``: the arms "generate arms" makes, one to
  twelve (default A, B and C).

**Every default is what a school already had**, so this migration changes
nothing for anybody until a school saves a setting. Declared by the academics
module for the reason ``vs_students`` ``0006`` gives, and with the same shape
``seed_config_catalogue`` gives them.

Reversible: the definitions and every school's values of them are removed, and
every school reads its term structure's defaults again.
"""

from django.db import migrations


SCHOOL = ["platform", "school"]

DEFINITIONS = [
    {
        "key": "academics.terms.word",
        "label": "Word For A Term",
        "description": (
            "What the school calls the parts of its year, TERM or SEMESTER, in "
            "every sentence it is shown. Empty means the word the school's "
            "term structure implies. Changing it never renames a term."
        ),
        "value_type": "CHOICE", "default_value": None,
        "validation_rules": {"choices": ["TERM", "SEMESTER"]},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "academics.terms.names",
        "label": "Term Names",
        "description": (
            "The names a new academic year's terms are given, in order: one to "
            "six, each at most 30 characters, none repeated. Empty means the "
            "names the school's term structure implies."
        ),
        "value_type": "JSON", "default_value": None,
        "validation_rules": {},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "academics.classes.default_arms",
        "label": "Default Class Arms",
        "description": (
            "The arms a level's classes are generated with, in order: one to "
            "twelve, each at most 30 characters, none repeated. A class is "
            "named after its level and arm, such as JSS1 A."
        ),
        "value_type": "JSON", "default_value": ["A", "B", "C"],
        "validation_rules": {},
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
        ("vs_academics", "0009_schoolclass_class_teacher"),
        ("vs_config", "0013_seed_display_timezone"),
    ]

    operations = [migrations.RunPython(declare, withdraw)]
