"""Classify every permission and group against the platform/tenant boundary.

The classification is derived from what the seeders actually register and
grant, not from the dotted key alone. The evidence, taken from the seeded
registry:

* Every module other than ``platform`` (``school``, ``academics``,
  ``communication``, ``config``, ``exports``, ``finance``, ``import``,
  ``payments``, ``procurement``, ``tickets``, ``todo``, ``workflow``) is a
  tenant surface: entity/tenant scoped views, and the school prebuilt roles hold
  keys from four of them directly. These are granted to the platform roles as
  well, but that is CX operating a tenant, not cross-tenant reach.
  -> ``TENANT``.

* The ``platform`` module is registered by ``seed_platform_permissions``, and
  most of it is a CX surface: the impersonation tiering, the global permission
  registry, the schools and branches roster, CX staff/payroll/organogram, the
  requirements library, the platform dashboard, compliance rule management, and
  platform health's cross-tenant aggregates. -> ``PLATFORM``.

**Two families in that module contradict their own prefix**, which is the whole
reason this is a declared column and not a ``startswith`` check:

* ``platform.team.*`` gates ``UserAccountViewSet``, whose queryset is filtered
  to the caller's own tenant for every non-platform caller. School admins hold
  ``platform.team.create`` today - it is how a school adds its own staff, and
  ``vs_user.tests.UserBranchAssignmentTests`` grants it inside a school tenant.

* ``platform.audit.view`` and ``platform.audit.export`` are held by audit
  officers *inside* a tenant. ``vs_audit``'s committed test suite builds that
  user deliberately - "outsider holds the very same key, but in a different
  tenant" - and asserts they may run and download their own exports.

Both are classified ``TENANT``. Enforcing on the ``platform.`` prefix would have
locked school admins out of user management and audit officers out of their own
trail. ``platform.audit.manage`` is *not* included: it edits compliance and
retention rules through an unscoped queryset and nothing grants it to a tenant.

The reverse pairing is also deliberate and stays legal: ``xvs_consultant`` is a
role on the codex platform tenant holding ``school.*`` and ``academics.*`` view
keys, which is why ``TENANT`` means "any tenant may hold it" rather than "no
platform role may hold it".

A group is classified by its contents: any platform member makes the whole
bundle platform, since attaching it grants everything inside.

The prefix is an input to this classification because it is what the seeders
encoded. It is not the enforcement mechanism - ``Permission.scope`` is, and from
here on a key's scope travels with the row rather than with its name.
"""
from django.db import migrations


PLATFORM_MODULE = "platform"

# Keys inside the platform module that a tenant role may legitimately hold.
# Kept in step with seed_platform_permissions.TENANT_HOLDABLE_KEYS.
TENANT_HOLDABLE_KEYS = [
    "platform.team.view",
    "platform.team.create",
    "platform.team.update",
    "platform.team.delete",
    "platform.team.suspend",
    "platform.team.reactivate",
    "platform.audit.view",
    "platform.audit.export",
]


def classify(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    PermissionGroup = apps.get_model("vs_rbac", "PermissionGroup")
    GroupPermission = apps.get_model("vs_rbac", "GroupPermission")

    Permission.objects.filter(module_id=PLATFORM_MODULE).update(scope="PLATFORM")
    Permission.objects.exclude(module_id=PLATFORM_MODULE).update(scope="TENANT")
    Permission.objects.filter(key__in=TENANT_HOLDABLE_KEYS).update(scope="TENANT")

    platform_keys = set(
        Permission.objects.filter(scope="PLATFORM").values_list("key", flat=True)
    )
    platform_group_ids = set(
        GroupPermission.objects.filter(permission_id__in=platform_keys).values_list(
            "group_id", flat=True,
        )
    )
    PermissionGroup.objects.filter(id__in=platform_group_ids).update(scope="PLATFORM")
    PermissionGroup.objects.exclude(id__in=platform_group_ids).update(scope="TENANT")


def unclassify(apps, schema_editor):
    apps.get_model("vs_rbac", "Permission").objects.update(scope="")
    apps.get_model("vs_rbac", "PermissionGroup").objects.update(scope="")


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0006_permission_scope_permissiongroup_scope"),
    ]

    operations = [
        migrations.RunPython(classify, unclassify),
    ]
