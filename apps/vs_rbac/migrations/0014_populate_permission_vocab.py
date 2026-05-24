"""
Data migration: populate PermissionModule, PermissionResource, PermissionAction
from any existing Permission rows and link them via FK.

Permission keys follow the format: module.resource.action
e.g. finance.invoice.view
"""
from django.db import migrations


def populate_vocab_from_permissions(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    PermissionModule = apps.get_model("vs_rbac", "PermissionModule")
    PermissionResource = apps.get_model("vs_rbac", "PermissionResource")
    PermissionAction = apps.get_model("vs_rbac", "PermissionAction")

    for perm in Permission.objects.all():
        parts = perm.key.split(".")
        if len(parts) < 3:
            continue

        module_key = parts[0]
        resource_key = parts[1]
        action_key = ".".join(parts[2:])  # handles dot in action if ever present

        module, _ = PermissionModule.objects.get_or_create(
            key=module_key,
            defaults={"name": module_key.replace("-", " ").title()},
        )
        resource, _ = PermissionResource.objects.get_or_create(
            module=module,
            key=resource_key,
            defaults={"name": resource_key.replace("-", " ").title()},
        )
        action, _ = PermissionAction.objects.get_or_create(
            key=action_key,
            defaults={"name": action_key.replace("-", " ").title()},
        )

        perm.module = module
        perm.resource = resource
        perm.action = action
        perm.save(update_fields=["module_id", "resource_id", "action_id"])


def reverse_vocab(apps, schema_editor):
    # Reversing just clears the FK links; the vocab records stay (harmless).
    Permission = apps.get_model("vs_rbac", "Permission")
    Permission.objects.update(module=None, resource=None, action=None)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0013_permission_module_resource_action_models"),
    ]

    operations = [
        migrations.RunPython(populate_vocab_from_permissions, reverse_code=reverse_vocab),
    ]
