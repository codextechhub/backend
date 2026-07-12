from django.db import migrations


def forwards(apps, schema_editor):
    Tenant = apps.get_model("vs_tenants", "Tenant")
    SchoolRole = apps.get_model("vs_rbac", "SchoolRoleTemplate")
    SchoolRolePermission = apps.get_model("vs_rbac", "SchoolRolePermission")
    SchoolRoleGroup = apps.get_model("vs_rbac", "SchoolRoleGroup")
    SchoolAssignment = apps.get_model("vs_rbac", "SchoolUserRoleAssignment")
    PlatformRole = apps.get_model("vs_rbac", "PlatformRoleTemplate")
    PlatformRolePermission = apps.get_model("vs_rbac", "PlatformRolePermission")
    PlatformRoleGroup = apps.get_model("vs_rbac", "PlatformRoleGroup")
    PlatformAssignment = apps.get_model("vs_rbac", "PlatformUserRoleAssignment")
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")
    RolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    RoleGroup = apps.get_model("vs_rbac", "TenantRoleGroup")
    Assignment = apps.get_model("vs_rbac", "TenantUserRoleAssignment")

    codex = Tenant.objects.get(slug="codex", kind="PLATFORM")

    for old in SchoolRole.objects.select_related("school").all().iterator():
        if not old.school_id or not old.school.tenant_id:
            continue
        Role.objects.update_or_create(
            tenant_id=old.school.tenant_id,
            key=str(old.pk),
            defaults={
                "branch_id": old.branch_id,
                "name": old.name,
                "description": old.description,
                "status": old.status,
                "is_system_role": old.is_system_role,
                "is_locked": old.is_locked,
                "version": old.version,
                "created_by_id": old.created_by_id,
            },
        )

    for old in PlatformRole.objects.all().iterator():
        Role.objects.update_or_create(
            tenant_id=codex.pk,
            key=str(old.pk),
            defaults={
                "name": old.name,
                "description": old.description,
                "status": old.status,
                "is_system_role": old.is_system_role,
                "is_locked": old.is_locked,
                "version": old.version,
                "created_by_id": old.created_by_id,
            },
        )

    for old in SchoolRolePermission.objects.select_related("role__school").all().iterator():
        if not old.role.school_id or not old.role.school.tenant_id:
            continue
        role = Role.objects.get(tenant_id=old.role.school.tenant_id, key=str(old.role_id))
        RolePermission.objects.update_or_create(
            role=role, permission_id=old.permission_id,
            defaults={"granted": old.granted, "granted_by_id": old.granted_by_id, "granted_at": old.granted_at},
        )
    for old in PlatformRolePermission.objects.all().iterator():
        role = Role.objects.get(tenant=codex, key=str(old.role_id))
        RolePermission.objects.update_or_create(
            role=role, permission_id=old.permission_id,
            defaults={"granted": old.granted, "granted_by_id": old.granted_by_id, "granted_at": old.granted_at},
        )

    for old in SchoolRoleGroup.objects.select_related("role__school").all().iterator():
        if not old.role.school_id or not old.role.school.tenant_id:
            continue
        role = Role.objects.get(tenant_id=old.role.school.tenant_id, key=str(old.role_id))
        RoleGroup.objects.update_or_create(
            role=role, group_id=old.group_id,
            defaults={"attached_by_id": old.attached_by_id, "attached_at": old.attached_at},
        )
    for old in PlatformRoleGroup.objects.all().iterator():
        role = Role.objects.get(tenant=codex, key=str(old.role_id))
        RoleGroup.objects.update_or_create(
            role=role, group_id=old.group_id,
            defaults={"attached_by_id": old.attached_by_id, "attached_at": old.attached_at},
        )

    for old in SchoolAssignment.objects.select_related("school").all().iterator():
        if not old.school.tenant_id:
            continue
        role = Role.objects.get(tenant_id=old.school.tenant_id, key=str(old.role_id))
        Assignment.objects.update_or_create(
            tenant_id=old.school.tenant_id, user_id=old.user_id, role=role,
            defaults={
                "branch_id": role.branch_id,
                "assignment_status": old.assignment_status,
                "assigned_by_id": old.assigned_by_id,
                "assigned_at": old.assigned_at,
                "revoked_at": old.revoked_at,
                "revoked_by_id": old.revoked_by_id,
                "reason_note": old.reason_note,
            },
        )
    for old in PlatformAssignment.objects.all().iterator():
        role = Role.objects.get(tenant=codex, key=str(old.role_id))
        Assignment.objects.update_or_create(
            tenant=codex, user_id=old.user_id, role=role,
            defaults={
                "assignment_status": old.assignment_status,
                "assigned_by_id": old.assigned_by_id,
                "assigned_at": old.assigned_at,
                "revoked_at": old.revoked_at,
                "revoked_by_id": old.revoked_by_id,
                "reason_note": old.reason_note,
            },
        )


def backwards(apps, schema_editor):
    apps.get_model("vs_rbac", "TenantUserRoleAssignment").objects.all().delete()
    apps.get_model("vs_rbac", "TenantRoleGroup").objects.all().delete()
    apps.get_model("vs_rbac", "TenantRolePermission").objects.all().delete()
    apps.get_model("vs_rbac", "TenantRoleTemplate").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("vs_rbac", "0003_tenantroletemplate_tenantrolepermission_and_more")]
    operations = [migrations.RunPython(forwards, backwards)]
