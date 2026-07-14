"""Seed the communication permission keys enforced by vs_notifications views.

Only the three keys the views actually check are seeded (see
NotificationPermission in constants.py — the rest of that class is reserved
for future messaging work and gets seeded when something enforces it):

    communication.notification_templates.configure   — template editor
    communication.communication_permissions.enforce  — settings matrix GET/PATCH
    communication.message_activity.audit             — delivery history log

Platform roles receive all three. School admin/branch admin prebuilt roles
receive the settings + history keys, because the backend already scopes both
endpoints to the caller's own school (see NotificationSettingViewSet /
NotificationHistoryViewSet docstrings).
"""
import re

from django.core.management.base import BaseCommand
from django.db import transaction

MODULE_NAME = "communication"
MODULE_DESCRIPTION = "Notifications, messaging and delivery tracking."
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]
_PLATFORM_ROLE_NAMES = {"xvs_super_admin": "XVS Super Admin", "xvs_platform_admin": "XVS Platform Admin"}
SCHOOL_ROLE_KEYS = ["school_admin", "branch_admin"]
SCHOOL_DEFAULT_KEYS = {
    "communication.communication_permissions.enforce",
    "communication.message_activity.audit",
}
_RESTRICTED = {"SENSITIVE", "CRITICAL"}

COMMUNICATION_RESOURCES = [
    ("notification_templates", "notification templates", [("configure", "SENSITIVE")]),
    ("communication_permissions", "notification settings", [("enforce", "SENSITIVE")]),
    ("message_activity", "notification delivery history", [("audit", "SENSITIVE")]),
]


class Command(BaseCommand):
    help = "Seed vs_notifications communication permission keys and default grants."

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import (
            Permission,
            PermissionAction,
            PermissionModule,
            PermissionResource,
            PrebuiltRolePermission,
            PrebuiltRoleTemplate,
            TenantRolePermission,
            TenantRoleTemplate,
        )
        from vs_tenants.models import Tenant

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n  Seeding {MODULE_NAME} permissions...\n"))

        needed_actions = {action for _, _, actions in COMMUNICATION_RESOURCES for action, _ in actions}
        for name in sorted(needed_actions):
            _, created = PermissionAction.objects.get_or_create(
                name=name,
                defaults={"description": f"Auto-registered action verb '{name}'.", "is_active": True},
            )
            if created:
                self.stdout.write(f"  + action '{name}'")

        module, created = PermissionModule.objects.get_or_create(
            name=MODULE_NAME,
            defaults={"description": MODULE_DESCRIPTION, "is_active": True},
        )
        self.stdout.write(f"  module '{MODULE_NAME}' " + ("created" if created else "exists"))

        all_perms = []
        created_perms = 0
        for resource_name, resource_label, actions in COMMUNICATION_RESOURCES:
            resource, _ = PermissionResource.objects.get_or_create(
                module=module,
                name=resource_name,
                defaults={"description": f"{resource_label.capitalize()} ({MODULE_NAME}).", "is_active": True},
            )
            for action_name, sensitivity in actions:
                action = PermissionAction.objects.get(name=action_name)
                expected_key = f"{MODULE_NAME}.{resource_name}.{action_name}"
                perm = Permission.objects.filter(key=expected_key).first()
                if perm is None:
                    perm = Permission(
                        module=module,
                        resource=resource,
                        action=action,
                        description=f"{action_name.replace('_', ' ').capitalize()} {resource_label}.",
                        sensitivity_level=sensitivity,
                        is_restricted=sensitivity in _RESTRICTED,
                        is_active=True,
                    )
                    perm.save()
                    created_perms += 1
                    self.stdout.write(f"  + {perm.key} [{sensitivity}]")
                all_perms.append(perm)

        codex = Tenant.objects.filter(slug="codex", kind=Tenant.Kind.PLATFORM).first()
        if codex is None:
            self.stdout.write(self.style.WARNING("  Codex platform tenant missing; platform grants skipped."))
        else:
            for role_id in PLATFORM_ROLE_IDS:
                role, _ = TenantRoleTemplate.objects.get_or_create(
                    tenant=codex,
                    key=role_id,
                    defaults={
                        "name": _PLATFORM_ROLE_NAMES.get(role_id, role_id),
                        "status": "ACTIVE",
                        "is_system_role": True,
                        "is_locked": True,
                    },
                )
                granted = 0
                for perm in all_perms:
                    _, link_created = TenantRolePermission.objects.get_or_create(
                        role=role,
                        permission=perm,
                        defaults={"granted": True, "granted_by": None},
                    )
                    granted += int(link_created)
                self.stdout.write(f"  {role_id}: granted {granted} new key(s).")

        for role_key in SCHOOL_ROLE_KEYS:
            role = PrebuiltRoleTemplate.objects.filter(key=role_key).first()
            if role is None:
                self.stdout.write(self.style.WARNING(f"  prebuilt role '{role_key}' missing; defaults skipped."))
                continue
            attached = 0
            for key in sorted(SCHOOL_DEFAULT_KEYS):
                _, link_created = PrebuiltRolePermission.objects.get_or_create(
                    prebuilt_role=role,
                    permission_id=key,
                )
                attached += int(link_created)
            self.stdout.write(f"  {role_key}: attached {attached} new default(s).")

        # Backfill existing tenant role templates (runtime grants live in the
        # tenant tables now). A tenant role maps to its prebuilt by its native
        # key: key=<prebuilt.key> or key=<prebuilt.key>-<branch>.
        prebuilt_for_role: dict[int, str] = {}

        native_key_re = re.compile(
            r"^(%s)(?:-\d+)?$" % "|".join(re.escape(k) for k in SCHOOL_ROLE_KEYS)
        )
        for role in TenantRoleTemplate.objects.filter(
            tenant__kind="SCHOOL", is_system_role=True,
        ).only("id", "key"):
            match = native_key_re.match(role.key)
            if match and role.pk not in prebuilt_for_role:
                prebuilt_for_role[role.pk] = match.group(1)

        backfilled = 0
        template_count = 0
        for role_pk in prebuilt_for_role:
            template_count += 1
            for key in sorted(SCHOOL_DEFAULT_KEYS):
                _, row_created = TenantRolePermission.objects.get_or_create(
                    role_id=role_pk,
                    permission_id=key,
                    defaults={"granted": True, "granted_by": None},
                )
                backfilled += int(row_created)

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_perms} new permission(s), {len(all_perms)} total communication keys; "
            f"backfilled {backfilled} grant(s) across {template_count} tenant role template(s).\n"
        ))
