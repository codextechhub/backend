"""Four permission keys split in two, and every grant follows the split.

Four keys each gated two things the price list sells at different depths, so
neither thing could be sold as designed:

    academics.timetable.*      class timetables, and exams
    school.teachers.view/update  the staff register, and qualifications and documents
    school.students.manage     transferring one child, and promoting the whole roll
    procurement.report.view    category and catalogue insights, and spend analysis

Splitting a key is the easy half. The dangerous half is that a split silently
takes access away: the moment ``exams.py`` starts asking for
``academics.exam.view``, everybody who could schedule an exam yesterday through
``academics.timetable.view`` is refused, and nothing on any screen says why.

So every grant of the old key is copied onto the new one, at every layer that
can carry one: role grants, group grants, the platform's prebuilt role
templates, and personal overrides. An explicit deny is copied as a deny, for
the same reason - somebody deliberately refused the old key must not gain the
new one by way of a migration.

Nothing is removed. The old keys keep their own meaning: the timetable keys
still govern timetables, and the register keys still govern the register. What
changes is only that the second thing each of them used to govern now has a key
of its own, which the plan gate can band separately.

The reverse is a no-op. Deleting the copied rows could not tell a grant this
migration made from one an administrator made afterwards, and guessing wrong
takes away somebody's access.
"""
from django.db import migrations

#: old dotted key -> new dotted key, and the vocabulary the new one needs.
SPLITS = [
    # (module, resource, action, old_key)
    ("academics", "exam", "view", "academics.timetable.view"),
    ("academics", "exam", "create", "academics.timetable.create"),
    ("academics", "exam", "update", "academics.timetable.update"),
    ("academics", "exam", "manage", "academics.timetable.manage"),
    ("academics", "exam", "publish", "academics.timetable.publish"),
    ("school", "staff_records", "view", "school.teachers.view"),
    ("school", "staff_records", "update", "school.teachers.update"),
    ("school", "students", "promote", "school.students.manage"),
    ("procurement", "analytics", "view", "procurement.report.view"),
]

RESOURCE_DESCRIPTIONS = {
    ("academics", "exam"): "Exams, exam slots and their publication",
    ("school", "staff_records"): "Staff qualifications, certificates and documents",
    ("procurement", "analytics"): "Spend analysis, aging and vendor performance",
}

ACTION_DESCRIPTIONS = {
    "promote": "Move a cohort up a level or year in one deliberate, reversible run.",
}

#: Sensitivity is copied from the key each new one was split out of, so a split
#: cannot quietly downgrade how dangerous a key is considered.
SENSITIVITY_OVERRIDES = {
    "school.staff_records.update": "SENSITIVE",
    "school.students.promote": "SENSITIVE",
}


def split_keys(apps, schema_editor):
    Module = apps.get_model("vs_rbac", "PermissionModule")
    Resource = apps.get_model("vs_rbac", "PermissionResource")
    Action = apps.get_model("vs_rbac", "PermissionAction")
    Permission = apps.get_model("vs_rbac", "Permission")
    RolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    GroupPermission = apps.get_model("vs_rbac", "GroupPermission")
    PrebuiltRolePermission = apps.get_model("vs_rbac", "PrebuiltRolePermission")
    Override = apps.get_model("vs_rbac", "UserPermissionOverride")

    for module_name, resource_name, action_name, old_key in SPLITS:
        old = Permission.objects.filter(key=old_key).first()
        if old is None:
            # The registry has not been seeded here yet. The seeder creates the
            # new key on its own, and there are no grants to carry over.
            continue

        module, _ = Module.objects.get_or_create(name=module_name)
        resource, _ = Resource.objects.get_or_create(
            module=module,
            name=resource_name,
            defaults={
                "description": RESOURCE_DESCRIPTIONS.get(
                    (module_name, resource_name), ""
                ),
            },
        )
        action, _ = Action.objects.get_or_create(
            name=action_name,
            defaults={"description": ACTION_DESCRIPTIONS.get(action_name, "")},
        )

        new_key = f"{module_name}.{resource_name}.{action_name}"
        new, _ = Permission.objects.get_or_create(
            key=new_key,
            defaults={
                "module": module,
                "resource": resource,
                "action": action,
                "description": old.description,
                "sensitivity_level": SENSITIVITY_OVERRIDES.get(
                    new_key, old.sensitivity_level
                ),
                "scope": old.scope,
                "is_restricted": old.is_restricted,
                "is_active": True,
            },
        )

        # Role grants, denies included. get_or_create leaves an existing row
        # alone, so re-running never overwrites a later decision.
        for row in RolePermission.objects.filter(permission=old):
            RolePermission.objects.get_or_create(
                role_id=row.role_id,
                permission=new,
                defaults={"granted": row.granted, "granted_by_id": row.granted_by_id},
            )

        for row in GroupPermission.objects.filter(permission=old):
            GroupPermission.objects.get_or_create(
                group_id=row.group_id, permission=new,
            )

        for row in PrebuiltRolePermission.objects.filter(permission=old):
            PrebuiltRolePermission.objects.get_or_create(
                prebuilt_role_id=row.prebuilt_role_id, permission=new,
            )

        # Personal overrides carry their mode across. A DENY that is not copied
        # would hand somebody the new key precisely because they were refused
        # the old one.
        for row in Override.objects.filter(permission=old):
            Override.objects.get_or_create(
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                permission=new,
                defaults={
                    "mode": row.mode,
                    "reason": f"Carried over when {old_key} split into {new_key}.",
                    "created_by_id": row.created_by_id,
                    "expires_at": row.expires_at,
                },
            )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0015_a_permission_names_the_capability_behind_it"),
    ]
    operations = [migrations.RunPython(split_keys, migrations.RunPython.noop)]
