"""Declare ``display.timezone`` - the time zone a tenant keeps its calendar in.

Seeded here for the reason 0009 gives: a definition is a row in this app's
catalogue. Its reader is :mod:`vs_config.clock`, which every app asks for a
tenant's "today" and "now", and its write guard (a real IANA zone, nothing
else) is registered by this app too.

Two things about the shape are deliberate.

**The default is Africa/Lagos.** The server keeps UTC, so before this every
"today" was the UTC day, which for a Lagos school is yesterday between midnight
and 1am. Every school starts on Lagos, and one that is elsewhere chooses its
own zone.

**Platform and school scope.** The platform layer is the default a school
inherits and the zone used where no tenant is in reach. Not ``branch``: a
school keeps one calendar, and two branches disagreeing about which day it is
would put one fee on two due dates.
"""

from django.db import migrations


KEY = "display.timezone"


def seed_display_timezone(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    definition_model.objects.get_or_create(
        key=KEY,
        defaults={
            "label": "Time Zone",
            "description": (
                "The IANA time zone this school keeps its calendar in, such as "
                "Africa/Lagos. It decides which day \"today\" is for due dates, "
                "overdue checks, attendance and the calendar, and the local time "
                "shown on documents. The platform value is the default for every "
                "school."
            ),
            "value_type": "STRING",
            "default_value": "Africa/Lagos",
            "validation_rules": {},
            "allowed_scopes": ["platform", "school"],
            "sensitivity": "INTERNAL",
            "is_active": True,
        },
    )


def drop_display_timezone(apps, schema_editor):
    """Remove the definition and every tenant's choice of it.

    Reversible in full: with the definition gone :mod:`vs_config.clock` falls
    back to its own Africa/Lagos default. The values go with it because
    ``ConfigurationValue`` points at the definition, and leaving orphans behind
    would resurrect a stale zone if the key were ever re-seeded.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key=KEY).delete()
    definition_model.objects.filter(key=KEY).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_config", "0012_seed_staff_profile_visibility"),
    ]

    operations = [
        migrations.RunPython(seed_display_timezone, drop_display_timezone),
    ]
