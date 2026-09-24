"""Replace every broad manage permission with the operations it concealed.

The old action bundled unrelated powers under one label. A grant that allowed
an administrator to edit a record could also authorize deletion, lifecycle
changes, or publication because the view had no narrower key to request.

Each old grant, deny, pending role change, and dependency is copied to the
concrete replacement keys before the old permissions and action are removed.
Permission-group membership moves only to unrestricted replacements because a
group must never carry a restricted key. The reverse is deliberately a no-op:
several concrete decisions cannot be collapsed back into one broad grant
without discarding later choices.
"""
from django.db import migrations


REPLACEMENTS = {
    "academics.calendar.manage": ("delete",),
    "academics.classes.manage": ("archive", "reactivate"),
    "academics.exam.manage": ("delete",),
    "academics.session.manage": ("activate", "archive", "delete"),
    "academics.structure.manage": ("archive", "reactivate"),
    "academics.subject.manage": ("archive", "reactivate"),
    "academics.timetable.manage": ("delete",),
    "config.capability.manage": ("create", "update", "archive"),
    "config.entitlement.manage": ("update", "delete"),
    "config.integration.manage": ("update", "trigger"),
    "config.override.manage": ("update",),
    "config.security.manage": ("update",),
    "exports.schedule.manage": ("update", "delete", "suspend", "reactivate"),
    "finance.dunning.manage": ("create", "update"),
    "finance.tax.manage": ("create", "update"),
    "payments.virtual_account.manage": ("update",),
    "platform.audit.manage": ("create", "update", "delete"),
    "platform.branches.manage": ("transition",),
    "platform.field_access.manage": ("update",),
    "platform.health.manage": ("create", "update"),
    "platform.organogram.manage": ("create", "update", "delete", "assign"),
    "platform.permission_groups.manage": ("create", "update", "delete"),
    "platform.roles.manage": ("create", "update", "delete"),
    "platform.schools.manage": ("configure", "transition"),
    "platform.team_overrides.manage": ("create", "delete"),
    "procurement.approval.manage": ("view", "update"),
    "procurement.stock.manage": ("create", "update"),
    "procurement.vendor.manage": ("verify",),
    "school.branches.manage": ("delete",),
    "school.fees.manage": ("update",),
    "school.field_access.manage": ("update",),
    "school.leave.manage": ("update", "cancel"),
    "school.students.manage": ("transition", "transfer", "suspend", "reactivate"),
    "school.settings.manage": ("update",),
    "school.teachers.manage": ("transition",),
    "school.user_overrides.manage": ("create", "delete"),
    "tickets.ticket.manage": ("triage", "transition", "escalate"),
    "todo.task.manage": ("create", "update", "mark", "delete"),
    "import.templates.manage": ("update",),
    "workflow.group.manage": ("create", "update", "delete"),
    "workflow.template.manage": ("update", "publish"),
}


NORMAL_UNRESTRICTED = {
    "platform.permission_groups.create",
    "platform.permission_groups.update",
    "platform.roles.create",
    "platform.roles.update",
    "platform.organogram.create",
    "platform.organogram.update",
    "todo.task.create",
    "todo.task.update",
    "todo.task.mark",
}

SENSITIVE_UNRESTRICTED = {
    "platform.health.create",
    "platform.health.update",
}

CRITICAL_RESTRICTED = {
    "config.entitlement.update",
    "config.entitlement.delete",
    "config.integration.update",
    "config.integration.trigger",
    "config.security.update",
    "platform.field_access.update",
    "platform.team_overrides.create",
    "platform.team_overrides.delete",
    "school.field_access.update",
    "school.user_overrides.create",
    "school.user_overrides.delete",
}


def target_metadata(key):
    if key in NORMAL_UNRESTRICTED:
        return "NORMAL", False
    if key in SENSITIVE_UNRESTRICTED:
        return "SENSITIVE", False
    if key in CRITICAL_RESTRICTED:
        return "CRITICAL", True
    return "SENSITIVE", True


def replace_manage_permissions(apps, schema_editor):
    Action = apps.get_model("vs_rbac", "PermissionAction")
    Permission = apps.get_model("vs_rbac", "Permission")
    RolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    GroupPermission = apps.get_model("vs_rbac", "GroupPermission")
    PrebuiltPermission = apps.get_model("vs_rbac", "PrebuiltRolePermission")
    Override = apps.get_model("vs_rbac", "UserPermissionOverride")
    Dependency = apps.get_model("vs_rbac", "PermissionDependency")
    Delta = apps.get_model("vs_rbac", "TenantRoleChangeDeltaItem")

    for old_key, action_names in REPLACEMENTS.items():
        old = Permission.objects.filter(key=old_key).first()
        if old is None:
            continue

        replacements = []
        for action_name in action_names:
            action, _ = Action.objects.get_or_create(name=action_name)
            new_key = f"{old.module_id}.{old.resource.name}.{action_name}"
            sensitivity, is_restricted = target_metadata(new_key)
            new, created = Permission.objects.get_or_create(
                key=new_key,
                defaults={
                    "module_id": old.module_id,
                    "resource_id": old.resource_id,
                    "action": action,
                    "description": old.description,
                    "sensitivity_level": sensitivity,
                    "scope": old.scope,
                    "is_restricted": is_restricted,
                    "is_active": True,
                    "capability_id": old.capability_id,
                },
            )
            if not created:
                changed = []
                if new.sensitivity_level != sensitivity:
                    new.sensitivity_level = sensitivity
                    changed.append("sensitivity_level")
                if new.is_restricted != is_restricted:
                    new.is_restricted = is_restricted
                    changed.append("is_restricted")
                if changed:
                    new.save(update_fields=changed)
            replacements.append(new)

            for row in RolePermission.objects.filter(permission=old):
                RolePermission.objects.get_or_create(
                    role_id=row.role_id,
                    permission=new,
                    defaults={
                        "granted": row.granted,
                        "granted_by_id": row.granted_by_id,
                        "granted_at": row.granted_at,
                    },
                )
            if not is_restricted:
                for row in GroupPermission.objects.filter(permission=old):
                    GroupPermission.objects.get_or_create(
                        group_id=row.group_id, permission=new,
                    )
            for row in PrebuiltPermission.objects.filter(permission=old):
                PrebuiltPermission.objects.get_or_create(
                    prebuilt_role_id=row.prebuilt_role_id, permission=new,
                )
            for row in Override.objects.filter(permission=old):
                Override.objects.get_or_create(
                    tenant_id=row.tenant_id,
                    user_id=row.user_id,
                    permission=new,
                    defaults={
                        "mode": row.mode,
                        "reason": row.reason,
                        "created_by_id": row.created_by_id,
                        "expires_at": row.expires_at,
                    },
                )
            for row in Delta.objects.filter(permission=old):
                Delta.objects.get_or_create(
                    request_id=row.request_id,
                    permission=new,
                    operation=row.operation,
                )

        for edge in Dependency.objects.filter(permission=old):
            for new in replacements:
                Dependency.objects.get_or_create(
                    permission=new, depends_on_id=edge.depends_on_id,
                )
        for edge in Dependency.objects.filter(depends_on=old):
            for new in replacements:
                Dependency.objects.get_or_create(
                    permission_id=edge.permission_id, depends_on=new,
                )

        Override.objects.filter(permission=old).delete()
        Delta.objects.filter(permission=old).delete()
        old.delete()

    remaining = Permission.objects.filter(action_id="manage")
    Override.objects.filter(permission__in=remaining).delete()
    Delta.objects.filter(permission__in=remaining).delete()
    remaining.delete()
    Action.objects.filter(name="manage").delete()


class Migration(migrations.Migration):
    dependencies = [("vs_rbac", "0026_a_field_is_a_switch_and_no_longer_a_key")]
    operations = [
        migrations.RunPython(replace_manage_permissions, migrations.RunPython.noop),
    ]
