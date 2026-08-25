"""Remove grant rows that bypass restricted-role approval.

Restricted permissions may be granted only as direct role permissions applied
through an approved ``TenantRoleChangeRequest``. Group membership, per-user
ALLOW overrides, and direct grants on custom roles carry no trustworthy
approval provenance under the old rules, so keeping them active would preserve
the same escalation after the write paths are closed. Seeded system roles are
retained because they are the trusted bootstrap for the approval ceiling.

The reverse is intentionally a no-op. Recreating unapproved grants during a
rollback would restore the privilege-escalation vulnerability this migration
closes.
"""
from django.db import migrations
from django.db.models import F


def remove_bypasses(apps, schema_editor):
    GroupPermission = apps.get_model("vs_rbac", "GroupPermission")
    TenantRoleGroup = apps.get_model("vs_rbac", "TenantRoleGroup")
    TenantRolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    TenantRoleTemplate = apps.get_model("vs_rbac", "TenantRoleTemplate")
    UserPermissionOverride = apps.get_model("vs_rbac", "UserPermissionOverride")

    restricted_group_ids = list(
        GroupPermission.objects.filter(
            permission__is_restricted=True,
        ).values_list("group_id", flat=True).distinct()
    )
    affected_group_role_ids = list(
        TenantRoleGroup.objects.filter(
            group_id__in=restricted_group_ids,
        ).values_list("role_id", flat=True).distinct()
    )
    affected_direct_role_ids = list(
        TenantRolePermission.objects.filter(
            granted=True,
            permission__is_restricted=True,
            role__is_system_role=False,
        ).values_list("role_id", flat=True).distinct()
    )

    GroupPermission.objects.filter(permission__is_restricted=True).delete()
    TenantRolePermission.objects.filter(
        granted=True,
        permission__is_restricted=True,
        role__is_system_role=False,
    ).delete()
    UserPermissionOverride.objects.filter(
        mode="ALLOW", permission__is_restricted=True,
    ).delete()

    affected_role_ids = set(affected_group_role_ids) | set(affected_direct_role_ids)
    if affected_role_ids:
        TenantRoleTemplate.objects.filter(id__in=affected_role_ids).update(
            version=F("version") + 1,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0008_config_is_platform_only"),
    ]

    operations = [
        migrations.RunPython(remove_bypasses, migrations.RunPython.noop),
    ]
