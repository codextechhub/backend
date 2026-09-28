"""Declare ``staff.profile_visibility`` - how much of a colleague's profile each reader sees.

Seeded here rather than in the staff app for the reason 0009 gives: a
definition is a row in this app's catalogue, and the app that consumes a
setting does not own the row that declares it. The staff app validates the
value's shape through the write guard it registers for this key.

Three things about the shape are deliberate.

**The default is the policy a school gets until it saves one**: a person reads
the whole of their own record, from the contact card to their history and
roles; a line manager reads the contact card, employment, leave and
teaching duties of the people under them; any other colleague reads the
contact card. Written out here
rather than imported, because a migration must say the same thing on the day it
is replayed as on the day it was written.

**School scope only.** Not ``platform``, because a value set centrally would
decide for every school at once who reads their staff's details. Not
``branch``, because the relationships it governs cross branches: a Lekki line
manager of an Ikeja teacher would otherwise be governed by whichever branch was
asked.

**JSON**, one list of section keys per audience. The shape is the staff app's
to check, since this app knows nothing of what a staff profile holds.
"""

from django.db import migrations


KEY = "staff.profile_visibility"


def seed_profile_visibility(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    definition_model.objects.get_or_create(
        key=KEY,
        defaults={
            "label": "Staff profile visibility",
            "description": (
                "How much of a member of staff's profile each reader sees: the "
                "person themselves (SELF), anyone above them in the reporting "
                "line (LINE) and any other colleague (COLLEAGUE), as a list of "
                "sections per audience. The contact card is always shown. "
                "Readers whose role reaches the record see what their role "
                "allows, whatever this says."
            ),
            "value_type": "JSON",
            "default_value": {
                "SELF": [
                    "contact", "employment", "personal", "records", "leave",
                    "teaching", "history", "roles",
                ],
                "LINE": ["contact", "employment", "leave", "teaching"],
                "COLLEAGUE": ["contact"],
            },
            "validation_rules": {},
            "allowed_scopes": ["school"],
            "sensitivity": "INTERNAL",
            "is_active": True,
        },
    )


def drop_profile_visibility(apps, schema_editor):
    """Remove the definition and every school's choice of it.

    Reversible in full: with the definition gone the staff app falls back to
    the same default in code. The values go with it because
    ``ConfigurationValue`` points at the definition, and leaving orphans behind
    would resurrect a school's old choice if the key were ever re-seeded.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key=KEY).delete()
    definition_model.objects.filter(key=KEY).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_config", "0011_seed_workflow_notifications"),
    ]

    operations = [
        migrations.RunPython(seed_profile_visibility, drop_profile_visibility),
    ]
