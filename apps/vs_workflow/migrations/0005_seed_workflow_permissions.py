"""Data migration: seed workflow permission keys and grant to platform admin roles.

Runs automatically during `python manage.py migrate`. Idempotent — safe to
apply on environments where the permissions already exist.

Depends on vs_rbac tables (PermissionModule, PermissionResource, etc.) and
the PermissionAction rows seeded by seed_actions. If vs_rbac is unavailable
the migration skips silently to avoid breaking installs that don't have RBAC.
"""
from django.db import migrations

# (resource_name, [(action_name, description, is_restricted), ...])
WORKFLOW_PERMISSIONS = [
    ("template", [
        ("manage", "Create, update, and publish workflow templates",        True),
        ("view",   "View workflow templates (read-only)",                   False),
    ]),
    ("instance", [
        ("submit", "Submit a document for workflow approval",               False),
        ("view",   "View workflow instances and their stage history",       False),
        ("cancel", "Cancel a workflow instance (admin override)",           True),
    ]),
    ("action", [
        ("reverse", "Reverse a recorded approver action (admin override)", True),
    ]),
]

PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]


def seed_permissions(apps, schema_editor):
    try:
        PermissionModule   = apps.get_model("vs_rbac", "PermissionModule")
        PermissionResource = apps.get_model("vs_rbac", "PermissionResource")
        PermissionAction   = apps.get_model("vs_rbac", "PermissionAction")
        Permission         = apps.get_model("vs_rbac", "Permission")
        PlatformRoleTemplate   = apps.get_model("vs_rbac", "PlatformRoleTemplate")
        PlatformRolePermission = apps.get_model("vs_rbac", "PlatformRolePermission")
    except LookupError:
        return  # vs_rbac not installed — skip silently

    module, _ = PermissionModule.objects.get_or_create(
        name="workflow",
        defaults={"description": "Approval workflow engine permissions", "is_active": True},
    )

    all_perms = []

    for resource_name, actions in WORKFLOW_PERMISSIONS:
        resource, _ = PermissionResource.objects.get_or_create(
            module=module,
            name=resource_name,
            defaults={
                "description": f"Workflow {resource_name} permissions",
                "is_active": True,
            },
        )

        for action_name, description, is_restricted in actions:
            action = PermissionAction.objects.filter(name=action_name).first()
            if not action:
                continue  # seed_actions hasn't run yet — skip this key

            expected_key = f"workflow.{resource_name}.{action_name}"
            perm = Permission.objects.filter(key=expected_key).first()
            if not perm:
                perm = Permission(
                    module=module,
                    resource=resource,
                    action=action,
                    description=description,
                    is_restricted=is_restricted,
                    sensitivity_level="SENSITIVE" if is_restricted else "NORMAL",
                    is_active=True,
                )
                perm.save()

            all_perms.append(perm)

    for role_id in PLATFORM_ROLE_IDS:
        role = PlatformRoleTemplate.objects.filter(id=role_id).first()
        if not role:
            continue
        for perm in all_perms:
            PlatformRolePermission.objects.get_or_create(
                role=role,
                permission=perm,
                defaults={"granted": True, "granted_by": None},
            )


def reverse_permissions(apps, schema_editor):
    try:
        Permission = apps.get_model("vs_rbac", "Permission")
    except LookupError:
        return
    Permission.objects.filter(key__startswith="workflow.").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_workflow", "0004_workflowstage_retired_at"),
    ]

    operations = [
        migrations.RunPython(seed_permissions, reverse_code=reverse_permissions),
    ]
