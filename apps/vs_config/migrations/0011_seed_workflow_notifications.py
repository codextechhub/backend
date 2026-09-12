"""Declare ``workflow.notifications.enabled`` - whether a school's approvals write to people.

Seeded here rather than in ``vs_workflow`` for the reason 0009 gives: a
definition is a row in this app's catalogue, and the app that consumes a
setting does not own the row that declares it.

Three things about the shape are deliberate.

**The default is true**, so this migration changes nothing for anybody. Every
school keeps being told when a step activates, when a request is returned,
rejected or finally approved, exactly as before.

**School scope only.** Not ``platform``, because one switch thrown centrally
would silence every school at once. Not ``branch``, because "does this school
tell people what is happening" is a fact about the school, and a branch
answering it differently from its siblings would mean an approver hearing about
a request only when it reached the right site.

**One switch, not four.** The template screen used to ask which of the four
lifecycle events should notify, on every template, and that was both too many
decisions and a trap: an untouched template notified on everything, while
setting any one of them made the other three count as off. A school answers the
question once here, and a template's own dict is left to whoever authored it.
"""

from django.db import migrations


KEY = "workflow.notifications.enabled"


def seed_workflow_notifications(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    definition_model.objects.get_or_create(
        key=KEY,
        defaults={
            "label": "Workflow notifications",
            "description": (
                "Whether this school's approvals notify people: the approvers of a "
                "step when it activates, and whoever raised a request when it is "
                "returned, rejected or fully approved. Off silences all four; the "
                "approvals themselves are unaffected and still wait for a decision."
            ),
            "value_type": "BOOLEAN",
            "default_value": True,
            "validation_rules": {},
            "allowed_scopes": ["school"],
            "sensitivity": "INTERNAL",
            "is_active": True,
        },
    )


def drop_workflow_notifications(apps, schema_editor):
    """Remove the definition and any school's choice of it.

    Reversible in full: with the definition gone the engine's own default takes
    over, which is to notify - what the value meant before this existed. The
    values go with it because ``ConfigurationValue`` points at the definition,
    and leaving orphans behind would resurrect a school's "off" if the key were
    ever re-seeded.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key=KEY).delete()
    definition_model.objects.filter(key=KEY).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_config", "0010_a_module_sold_whole_and_used_in_slices"),
    ]

    operations = [
        migrations.RunPython(seed_workflow_notifications, drop_workflow_notifications),
    ]
