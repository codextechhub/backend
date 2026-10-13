"""Give each permission split out of a broad ``manage`` key its own wording.

Splitting a ``*.manage`` permission into concrete keys copied the broad
description onto every replacement, so two switches on a role screen could both
read "Manage dunning notices." and an administrator could not tell them apart.
This operation rewrites the description of every replacement key that still
carries a "Manage ..." label so it names its own verb ("Create dunning
notices.", "Update dunning notices."). The replacement keys and the wording rule
come from the migration that performed the split, so the two cannot drift.

Rows whose description no longer starts with "Manage " (an administrator or a
seed already reworded them) and every key outside the split are left alone.
The reverse is a no-op: the shared broad label is the defect being repaired, so
restoring it would reintroduce it.
"""
from importlib import import_module

from django.db import migrations


def name_each_split_permission_by_its_action(apps, schema_editor):
    split = import_module("vs_rbac.migrations.0027_manage_is_not_an_action")
    Permission = apps.get_model("vs_rbac", "Permission")

    for old_key, action_names in split.REPLACEMENTS.items():
        prefix = old_key.rsplit(".", 1)[0]
        for action_name in action_names:
            row = (
                Permission.objects.select_related("resource")
                .filter(key=f"{prefix}.{action_name}")
                .first()
            )
            if row is None or not row.description.startswith("Manage "):
                continue
            row.description = split.replacement_description(
                row.description, action_name, row.resource,
            )
            row.save(update_fields=["description"])


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0033_closed_figures_wording"),
    ]
    operations = [
        migrations.RunPython(
            name_each_split_permission_by_its_action,
            migrations.RunPython.noop,
        ),
    ]
