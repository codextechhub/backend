"""Notification settings belong to XVS, and no other tenant may hold the key.

``communication.communication_permissions.enforce`` gates the notification
settings matrix: ``GET /v1/notify/settings/`` and ``PATCH
/v1/notify/settings/update/``. That matrix decides which events send email and
on which channel, and it is XVS's to run. A tenant has no business deciding it,
and no reason to see it: when a tenant administrator switches off the invoice
reminder email, the people who should have been reminded are the ones who pay
for it, and XVS is the one asked why the reminders stopped.

The key was ``TENANT``-scoped, and ``seed_notification_permissions`` made it a
default grant of the ``school_admin`` and ``branch_admin`` prebuilts and
backfilled it into every existing tenant role built from them. So every tenant
administrator, and every branch administrator with no branch narrowing at all,
could rewrite the whole tenant's matrix.

What this takes away
--------------------

The key becomes ``PLATFORM``, and every grant of it held outside the platform
tenant goes, through the shared sweep
:func:`vs_rbac.scope_withdrawal.withdraw_from_tenants`:

* the grant on each tenant role, ``school_admin`` and ``branch_admin`` and any
  role a tenant built for itself;
* the default on the ``school_admin`` and ``branch_admin`` prebuilts, so a new
  tenant is never given it;
* membership of any tenant-scoped permission group;
* personal ALLOW overrides, restricted ones included;
* the ``ADD`` line on any role change still waiting on its approval ladder.

``xvs_super_admin`` and ``xvs_platform_admin`` on the platform tenant keep it,
and so does anything else held there. The registry revision is bumped, so an
already-warm user object does not keep authority the registry has withdrawn.

The tenant rows of ``NotificationSetting`` are not touched. They are data, not
grants, and what should happen to them is a separate decision.

Reverse
-------

Restores ``TENANT`` scope and the default grant on the ``school_admin`` and
``branch_admin`` prebuilts and on every system role outside the platform tenant
built from one of them (key ``school_admin`` or ``branch_admin``, optionally
suffixed ``-<number>``), which is exactly what the seed handed out. Grants a
tenant chose for itself, group memberships and personal overrides are not
recreated: nobody can say which of them existed, and inventing one would hand
somebody authority nobody granted.
"""
import re

from django.db import migrations, models

from vs_rbac.scope_withdrawal import withdraw_from_tenants

KEY = "communication.communication_permissions.enforce"

#: The prebuilts the seed gave the key to, and the tenant roles built from them.
DEFAULT_HOLDERS = ("school_admin", "branch_admin")
_NATIVE_ROLE_KEY = re.compile(
    r"^(%s)(?:-\d+)?$" % "|".join(re.escape(key) for key in DEFAULT_HOLDERS)
)


def _bump_revision(apps):
    PermissionRegistryRevision = apps.get_model("vs_rbac", "PermissionRegistryRevision")
    PermissionRegistryRevision.objects.filter(pk=1).update(
        revision=models.F("revision") + 1,
    )


def forward(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")

    Permission.objects.filter(key=KEY).update(scope="PLATFORM")
    withdraw_from_tenants([KEY], apps=apps)
    _bump_revision(apps)


def backward(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    PrebuiltRoleTemplate = apps.get_model("vs_rbac", "PrebuiltRoleTemplate")
    PrebuiltRolePermission = apps.get_model("vs_rbac", "PrebuiltRolePermission")
    TenantRoleTemplate = apps.get_model("vs_rbac", "TenantRoleTemplate")
    TenantRolePermission = apps.get_model("vs_rbac", "TenantRolePermission")

    if not Permission.objects.filter(key=KEY).update(scope="TENANT"):
        return

    for prebuilt in PrebuiltRoleTemplate.objects.filter(key__in=DEFAULT_HOLDERS):
        PrebuiltRolePermission.objects.get_or_create(
            prebuilt_role=prebuilt, permission_id=KEY,
        )

    roles = (
        TenantRoleTemplate.objects
        .filter(is_system_role=True)
        .exclude(tenant__kind="PLATFORM")
        .only("id", "key")
    )
    for role in roles:
        if _NATIVE_ROLE_KEY.match(role.key):
            TenantRolePermission.objects.get_or_create(
                role_id=role.pk, permission_id=KEY,
                defaults={"granted": True, "granted_by": None},
            )

    _bump_revision(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0029_a_restricted_grant_waits_for_approval"),
    ]
    operations = [
        migrations.RunPython(forward, backward),
    ]
