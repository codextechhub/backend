"""Declare ``payroll.scope`` - central or per-branch payroll, the school's choice.

Seeded here rather than in ``vs_finance`` for the same reason
``notifications.email_max_retries`` is (0005): a definition is a row in this app's
catalogue, and the app that consumes a setting does not own the row that declares
it.

Two things about the shape are deliberate.

**The default is CENTRAL**, so this migration changes nothing for anybody. Every
school keeps running one payroll covering everyone, exactly as before; per-branch
payroll only exists for a school that opts in.

**School scope only.** Not ``platform``, because a product-wide default flipped in
one place would move every school onto per-branch payroll at once, past the guard
that checks each school's own roster. Not ``branch``, because "does this school run
payroll per branch" is a fact about the school; a branch answering it differently
from its siblings is not a configuration, it is a contradiction.
"""

from django.db import migrations


KEY = "payroll.scope"


def seed_payroll_scope(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    definition_model.objects.get_or_create(
        key=KEY,
        defaults={
            "label": "Payroll scope",
            "description": (
                "Whether payroll is run centrally for the whole school (CENTRAL) or "
                "separately by each branch's payroll officer (PER_BRANCH). Switching "
                "to PER_BRANCH is refused while any active employee has no branch."
            ),
            "value_type": "CHOICE",
            "default_value": "CENTRAL",
            "validation_rules": {"choices": ["CENTRAL", "PER_BRANCH"]},
            "allowed_scopes": ["school"],
            "sensitivity": "INTERNAL",
            "is_active": True,
        },
    )


def drop_payroll_scope(apps, schema_editor):
    """Remove the definition and any school's choice of it.

    Reversible in full: with the definition gone ``payroll_scope`` falls back to
    CENTRAL for everybody, which is what the value meant before this existed. The
    values go with it because ``ConfigurationValue`` points at the definition, and
    leaving orphans behind would resurrect stale choices if the key were ever
    re-seeded.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key=KEY).delete()
    definition_model.objects.filter(key=KEY).delete()


class Migration(migrations.Migration):
    dependencies = [("vs_config", "0008_retarget_branch_to_vs_tenants")]

    operations = [migrations.RunPython(seed_payroll_scope, drop_payroll_scope)]
