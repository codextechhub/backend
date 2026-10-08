"""Keep stored access-registry wording aligned with the Closed figures screen.

Permission and action rows are reference data created by deployment seeds rather
than schema migrations. This operation writes the canonical descriptions when the
rows are present. The seed definitions carry the same values when they create the
rows in an empty registry.
"""
from django.db import migrations


def use_closed_figures_wording(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    PermissionAction = apps.get_model("vs_rbac", "PermissionAction")
    Permission.objects.filter(key="finance.seal.view").update(
        description="View closed period figures.",
    )
    PermissionAction.objects.filter(name="lock").update(
        description="Permanently lock a closed accounting period against reopening.",
    )


def restore_sealed_figures_wording(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    PermissionAction = apps.get_model("vs_rbac", "PermissionAction")
    Permission.objects.filter(key="finance.seal.view").update(
        description="View sealed period figures.",
    )
    PermissionAction.objects.filter(name="lock").update(
        description="Permanently seal a closed accounting period against any re-open.",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0032_module_roles_catch_up_with_their_modules"),
    ]
    operations = [
        migrations.RunPython(use_closed_figures_wording, restore_sealed_figures_wording),
    ]
