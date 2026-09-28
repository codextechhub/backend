"""Take ``school.teachers.view`` off every school's Teacher role.

The key reads the staff directory and every staff record inside the holder's
branches, as the role allows. A teacher held it by default, so a teacher read
every colleague at their branch in full: staff ID, date of birth, gender, roles
and history. That made the school's own staff profile policy (the Colleagues
row of Settings, Staff profiles) decide nothing for the people it is mostly
about. A teacher now reads a colleague's profile as far as that policy shows
colleagues and line managers, and their own record through the same policy.

Two layers carry the default, and both are changed:

* the library's Teacher template (``PrebuiltRoleTemplate`` keyed ``teacher``),
  which a school created from now on copies its Teacher role from;
* every school's native Teacher role: a system role whose key is ``teacher``
  or ``teacher-<branch pk>``, the same rule ``seed_school_permissions`` uses
  to find the roles it backfills.

Only a GRANTED row is removed. An explicit deny stays a deny. A custom role
(``is_system_role`` false) and every other prebuilt role are left alone, so a
school that built its own "Head of Department" role on the key keeps it.

**A school that deliberately gave its Teacher role this key loses it here**,
because a migration cannot tell that grant from the default one. The school
can grant it again from its roles screen, and its teachers then read every
colleague at their branches in full, as before.

Reversible: the reverse grants the key again to every native Teacher role that
has no row for it, and puts it back on the library template.
"""
import re

from django.db import migrations

KEY = "school.teachers.view"
PREBUILT_KEY = "teacher"
NATIVE_KEY = re.compile(r"^teacher(?:-\d+)?$")


def _native_teacher_role_ids(apps):
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")
    return [
        role.pk
        for role in Role.objects.filter(
            tenant__kind="SCHOOL", is_system_role=True, key__startswith=PREBUILT_KEY,
        ).only("id", "key")
        if NATIVE_KEY.match(role.key)
    ]


def withdraw(apps, schema_editor):
    RolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    PrebuiltPermission = apps.get_model("vs_rbac", "PrebuiltRolePermission")

    RolePermission.objects.filter(
        role_id__in=_native_teacher_role_ids(apps), permission_id=KEY, granted=True,
    ).delete()
    PrebuiltPermission.objects.filter(
        prebuilt_role__key=PREBUILT_KEY, permission_id=KEY,
    ).delete()


def regrant(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    Prebuilt = apps.get_model("vs_rbac", "PrebuiltRoleTemplate")
    RolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    PrebuiltPermission = apps.get_model("vs_rbac", "PrebuiltRolePermission")

    if not Permission.objects.filter(key=KEY).exists():
        return
    for role_id in _native_teacher_role_ids(apps):
        RolePermission.objects.get_or_create(
            role_id=role_id, permission_id=KEY, defaults={"granted": True},
        )
    for prebuilt in Prebuilt.objects.filter(key=PREBUILT_KEY):
        PrebuiltPermission.objects.get_or_create(prebuilt_role=prebuilt, permission_id=KEY)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0029_a_restricted_grant_waits_for_approval"),
    ]

    operations = [migrations.RunPython(withdraw, regrant)]
