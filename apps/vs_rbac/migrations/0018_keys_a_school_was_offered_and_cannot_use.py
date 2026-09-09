"""Take back the keys a school's roles screen offered but no school can use.

A school choosing what its bursar may do is shown every ``PermissionScope.TENANT``
key. Four of them govern things that are not a school's at all, so the screen
offered a choice that could only ever disappoint: tick it, and nothing happens.

**``todo.task.view`` / ``.manage`` / ``.assign``.** ``vs_todo`` is CodeX's own
accountability tracker - "one accountable item, owned by exactly one CX staff
member" - and its queryset has no tenant column anywhere. It scopes by the CX
organogram through ``TodoHierarchy.area_user_ids``, so a school user holding
these would be asking about a hierarchy they are not in. No school role held
any of them, which is the clearest evidence they were never a school's to hold.

**``finance.entity.create``.** A set of books is CodeX's to create: a school is
given one when it is created and keeps that one. The school app does not route
the screen that would make another, and Finance Settings hides the section for
it. Reading stays tenant-holdable, because a school's finance module has to say
which entity it is working in. This mirrors ``finance.currency.create`` and
``finance.fxrate.create``, which are platform-only for the same reason.

Its sixteen grants go with it. They reached Finance Admin through
``adopt_console_admin_roles``, which grants every key in the module, and the
grant models refuse a platform key on a tenant role - so leaving them would make
those roles unsaveable through the API.

**What is deliberately NOT here.** ``platform.team.*`` and ``platform.audit.*``
look like CodeX's by their module name and are not: their endpoints are
tenant-scoped, a school admin holding ``platform.team.view`` lists their own
school's people, and a note in ``seed_platform_permissions`` records the decision
to reword them rather than reclassify, because reclassifying "would have locked
schools out of administering their own staff". The module a key sits in is not
the authority on whose it is; what its endpoint scopes to is.
"""
from django.db import migrations

TODO_KEYS = ["todo.task.view", "todo.task.manage", "todo.task.assign"]
ENTITY_CREATE = "finance.entity.create"


def take_them_back(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    RolePermission = apps.get_model("vs_rbac", "TenantRolePermission")

    keys = TODO_KEYS + [ENTITY_CREATE]

    # The grants first: the constraint that refuses a platform key on a tenant
    # role would otherwise be violated by rows already sitting there.
    RolePermission.objects.filter(
        permission_id__in=keys, role__tenant__kind="SCHOOL",
    ).delete()

    Permission.objects.filter(key__in=keys).update(scope="PLATFORM")


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0017_every_school_has_the_roles_codex_ships"),
    ]
    operations = [
        migrations.RunPython(take_them_back, migrations.RunPython.noop),
    ]
