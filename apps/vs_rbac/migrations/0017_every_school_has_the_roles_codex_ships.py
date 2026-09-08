"""Give the schools created before it the roles creation now provisions.

School creation used to provision School Admin and a Branch Admin per branch,
and nothing else. Teacher, Finance Admin and Procurement Admin sat in the
prebuilt library and reached a school only when somebody ran
``adopt_console_admin_roles`` by hand, so schools created days apart carry
different sets and a school opening its roles screen on its first day found two
rows where the product is built around five.

Creation provisions all four tenant-wide roles now. This is the schools that
already exist.

**Permissions come from the library template, not from a list here.** The
library is where "what a Teacher may do" is decided, and a second copy of that
decision in a migration is a copy that goes stale - which is the failure this
migration is partly cleaning up after. Only keys the tenant may actually hold
are granted: a school cannot be given a ``PermissionScope.PLATFORM`` key, and
the grant model refuses one, so they are filtered rather than left to raise.

Idempotent, and additive only. A school that already has the role keeps the one
it has, permissions untouched: it may have shaped that role since, and a
migration must not overwrite what a school decided about its own access.

Reversible as a no-op. Deleting roles a school may already have assigned to
people would take away access this only ever added.
"""
from django.db import migrations, models

#: Tenant-wide only. ``branch_admin`` is branch-scoped and provisioned per
#: branch at creation, so a back-fill here cannot know how many to make.
LIBRARY_KEYS = ["school_admin", "teacher", "finance_admin", "procurement_admin"]


def give_them_the_set(apps, schema_editor):
    Prebuilt = apps.get_model("vs_rbac", "PrebuiltRoleTemplate")
    PrebuiltPermission = apps.get_model("vs_rbac", "PrebuiltRolePermission")
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")
    RolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    Permission = apps.get_model("vs_rbac", "Permission")
    Tenant = apps.get_model("vs_tenants", "Tenant")

    schools = list(Tenant.objects.filter(kind="SCHOOL"))
    if not schools:
        return

    tenant_scoped = set(
        Permission.objects.exclude(scope="PLATFORM").values_list("key", flat=True)
    )

    for prebuilt in Prebuilt.objects.filter(key__in=LIBRARY_KEYS, is_active=True):
        keys = [
            k for k in PrebuiltPermission.objects
            .filter(prebuilt_role=prebuilt)
            .values_list("permission_id", flat=True)
            if k in tenant_scoped
        ]
        for tenant in schools:
            # By key OR by name, because the same role reached tenants under two
            # spellings: the library key is ``finance_admin`` and
            # ``adopt_console_admin_roles`` created its copies as
            # ``finance-admin``. Matching on key alone finds neither the other's
            # rows nor the collision, and the name is unique per tenant, so the
            # insert fails rather than skipping.
            if Role.objects.filter(tenant=tenant).filter(
                models.Q(key=prebuilt.key) | models.Q(name=prebuilt.name)
            ).exists():
                continue
            role = Role.objects.create(
                tenant=tenant,
                key=prebuilt.key,
                name=prebuilt.name,
                description=prebuilt.description,
                is_system_role=True,
                is_locked=False,
                status="ACTIVE",
            )
            RolePermission.objects.bulk_create(
                [
                    RolePermission(role=role, permission_id=key, granted=True)
                    for key in keys
                ],
                ignore_conflicts=True,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0016_a_split_key_takes_its_grants_with_it"),
        ("vs_tenants", "0009_a_site_is_a_branch_not_a_campus"),
    ]
    operations = [
        migrations.RunPython(give_them_the_set, migrations.RunPython.noop),
    ]
