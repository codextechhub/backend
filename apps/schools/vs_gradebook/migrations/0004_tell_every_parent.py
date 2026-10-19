"""Declare ``gradebook.results_tell_every_parent``.

Whether a released result is told to every parent of the child who has a
parent account, in the parent app and on their phone, or to the primary
guardian alone (the default). Declared here for the reason ``vs_attendance``
``0002`` gives, with the shape ``seed_config_catalogue`` gives it.

Reversible: the definition and every school's value of it are removed.
"""

from django.db import migrations

KEY = "gradebook.results_tell_every_parent"

DEFINITION = {
    "label": "Tell Every Parent When A Result Is Released",
    "description": (
        "Whether a released result is told to every parent of the child who "
        "has a parent account, in the parent app and on their phone. Off "
        "tells the primary guardian alone. The report card email goes to the "
        "primary guardian either way."
    ),
    "value_type": "BOOLEAN", "default_value": False,
    "validation_rules": {}, "allowed_scopes": ["platform", "school"],
}


def declare(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    definition_model.objects.get_or_create(
        key=KEY, defaults={**DEFINITION, "sensitivity": "INTERNAL", "is_active": True},
    )


def withdraw(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key=KEY).delete()
    definition_model.objects.filter(key=KEY).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_gradebook", "0003_score_imports"),
        ("vs_config", "0016_held_tolerance_in_naira"),
    ]

    operations = [migrations.RunPython(declare, withdraw)]
