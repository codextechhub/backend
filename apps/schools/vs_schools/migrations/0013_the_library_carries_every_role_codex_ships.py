"""Put every role CodeX ships in the library, not two of the five.

``0010_seed_required_admin_role_templates`` guaranteed School Admin and Branch
Admin on a fresh install, because school onboarding cannot provision an
administrator without them. Teacher, Finance Admin and Procurement Admin were
left to ``seed_prebuilt_role_templates``, so an install that had never run it
gave a new school two of the five roles the product is built around and said
nothing about the other three.

Identity only, exactly as 0010 seeds it: key, name, scope and tier. What each
role may DO is decided by the seeders that attach the defaults, and a second
copy of that decision here would be a copy that goes stale.

Existing rows are preserved. An operator may have refined a description or
deactivated a template deliberately, and a migration must not reverse either.

The schools created while the library was short are topped up as well, the way
``vs_rbac.0017`` topped up the schools created before creation provisioned the
full set: additive, matched on the role's key or its name because the same role
reached tenants under two spellings, and with permissions copied from the
library rather than listed here. ``branch_admin`` is not topped up: it is
branch-scoped, and a backfill cannot know how many copies a school should have.

Reversible as a no-op. Deleting library rows or roles a school may already have
assigned to people would take away access this only ever added.
"""
from django.db import migrations, models


ROLES_CODEX_SHIPS = (
    {
        "key": "teacher",
        "name": "Teacher",
        "scope": "branch",
        "tier": "B",
        "description": "Teaching staff member, scoped to a branch.",
    },
    {
        "key": "finance_admin",
        "name": "Finance Admin",
        "scope": "institution",
        "tier": "A",
        "description": "Runs the whole of a school's money, across every branch.",
    },
    {
        "key": "procurement_admin",
        "name": "Procurement Admin",
        "scope": "institution",
        "tier": "B",
        "description": "Runs the whole of a school's buying, across every branch.",
    },
)

#: Tenant-wide only, for the reason given in the module docstring.
TOP_UP_KEYS = ["school_admin", "teacher", "finance_admin", "procurement_admin"]


def carry_every_role(apps, schema_editor):
    Prebuilt = apps.get_model("vs_rbac", "PrebuiltRoleTemplate")

    for role in ROLES_CODEX_SHIPS:
        Prebuilt.objects.get_or_create(
            key=role["key"],
            defaults={
                "name": role["name"],
                "scope": role["scope"],
                "tier": role["tier"],
                "description": role["description"],
                "is_active": True,
            },
        )

    _give_existing_schools_the_set(apps)


def _give_existing_schools_the_set(apps):
    """The schools that were created while the library was short."""
    Prebuilt = apps.get_model("vs_rbac", "PrebuiltRoleTemplate")
    PrebuiltPermission = apps.get_model("vs_rbac", "PrebuiltRolePermission")
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")
    RolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    Permission = apps.get_model("vs_rbac", "Permission")
    Tenant = apps.get_model("vs_tenants", "Tenant")

    schools = list(Tenant.objects.filter(kind="SCHOOL"))
    if not schools:
        return

    # A school may only hold tenant-scoped keys, and the grant model refuses a
    # platform one, so they are filtered rather than left to raise.
    tenant_scoped = set(
        Permission.objects.exclude(scope="PLATFORM").values_list("key", flat=True)
    )

    for prebuilt in Prebuilt.objects.filter(key__in=TOP_UP_KEYS, is_active=True):
        keys = [
            key for key in PrebuiltPermission.objects
            .filter(prebuilt_role=prebuilt)
            .values_list("permission_id", flat=True)
            if key in tenant_scoped
        ]
        for tenant in schools:
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
        ("vs_schools", "0012_a_tier_sets_depth_and_stops_capping_size"),
        ("vs_rbac", "0017_every_school_has_the_roles_codex_ships"),
    ]

    operations = [
        migrations.RunPython(carry_every_role, migrations.RunPython.noop),
    ]
