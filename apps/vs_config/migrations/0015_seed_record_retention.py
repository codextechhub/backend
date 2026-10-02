"""Declare how long financial records are kept, and when a year may be archived.

Seeded here for the reason ``payroll.scope`` is (0009): a definition is a row in
this app's catalogue, and the app that consumes a setting does not own it.

* ``finance.retention.statutory_years`` (platform only, default 6): the legal
  minimum, maintained by CodeX. National law is not a tenant's choice, so no
  tenant scope exists for it. See :mod:`vs_finance.retention` for the source.
* ``finance.retention.years`` (tenant only, default empty, meaning the
  statutory minimum): a tenant may keep its books longer. Finance's write guard
  refuses a value below the floor.
* ``finance.archive.min_age_years`` (tenant only, default 2): how many years
  after its end a closed fiscal year may be archived. Archiving is always a
  deliberate act; nothing archives a year on its own.

None changes behaviour on its own: retention reads six years with or without
the rows, and nothing is archived until someone archives it. Reversible: the
definitions and any values set for them are removed.
"""

from django.db import migrations

DEFINITIONS = (
    {
        "key": "finance.retention.statutory_years",
        "label": "Statutory record retention (years)",
        "description": (
            "The minimum number of years, counted from the end of the fiscal year a "
            "record belongs to, that financial records, their evidence and the audit "
            "trail must be kept by law. Maintained by CodeX; no tenant can shorten it."
        ),
        "value_type": "INTEGER",
        "default_value": 6,
        "validation_rules": {"min": 1, "max": 50},
        "allowed_scopes": ["platform"],
    },
    {
        "key": "finance.retention.years",
        "label": "Record retention (years)",
        "description": (
            "How many years this tenant keeps its financial records after the end of "
            "the fiscal year they belong to. Empty keeps them for the legal minimum; "
            "a value may lengthen that, never shorten it."
        ),
        "value_type": "INTEGER",
        "default_value": None,
        "validation_rules": {"min": 1, "max": 100},
        "allowed_scopes": ["school"],
    },
    {
        "key": "finance.archive.min_age_years",
        "label": "Archive fiscal years after (years)",
        "description": (
            "How many years after its end a closed fiscal year may be archived. "
            "Archiving takes a year out of the default lists and pickers; it is never "
            "automatic and never deletes anything."
        ),
        "value_type": "INTEGER",
        "default_value": 2,
        "validation_rules": {"min": 1, "max": 50},
        "allowed_scopes": ["school"],
    },
)


def seed(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    for row in DEFINITIONS:
        definition_model.objects.get_or_create(
            key=row["key"],
            defaults={
                **{k: v for k, v in row.items() if k != "key"},
                "sensitivity": "INTERNAL",
                "is_active": True,
            },
        )


def unseed(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    keys = [row["key"] for row in DEFINITIONS]
    value_model.objects.filter(definition__key__in=keys).delete()
    definition_model.objects.filter(key__in=keys).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_config", "0014_display_date_format_clock_and_branch_zones"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
