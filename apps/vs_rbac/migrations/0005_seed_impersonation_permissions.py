from django.db import migrations


def forwards(apps, schema_editor):
    Module = apps.get_model("vs_rbac", "PermissionModule")
    Resource = apps.get_model("vs_rbac", "PermissionResource")
    Action = apps.get_model("vs_rbac", "PermissionAction")
    Permission = apps.get_model("vs_rbac", "Permission")
    Tenant = apps.get_model("vs_tenants", "Tenant")
    TenantRole = apps.get_model("vs_rbac", "TenantRoleTemplate")
    TenantRolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    module, _ = Module.objects.get_or_create(name="platform", defaults={"description": "Platform administration"})
    resource, _ = Resource.objects.get_or_create(module=module, name="impersonation", defaults={"description": "Audited support impersonation"})
    for action_name in ("start", "end", "view"):
        action, _ = Action.objects.get_or_create(name=action_name)
        permission, _ = Permission.objects.get_or_create(
            key=f"platform.impersonation.{action_name}",
            defaults={
                "module": module,
                "resource": resource,
                "action": action,
                "description": f"May {action_name} support impersonation sessions.",
                "sensitivity_level": "CRITICAL",
                "is_restricted": True,
                "is_active": True,
            },
        )
        codex = Tenant.objects.get(slug="codex")
        super_role = TenantRole.objects.filter(tenant=codex, key="xvs_super_admin").first()
        if super_role:
            TenantRolePermission.objects.get_or_create(
                role=super_role, permission=permission, defaults={"granted": True},
            )


def backwards(apps, schema_editor):
    apps.get_model("vs_rbac", "Permission").objects.filter(
        key__in=["platform.impersonation.start", "platform.impersonation.end", "platform.impersonation.view"],
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("vs_rbac", "0004_backfill_tenant_rbac")]
    operations = [migrations.RunPython(forwards, backwards)]
