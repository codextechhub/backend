"""Seed vs_tickets permission keys and attach sensible default grants.

The ticket module is a cross-cutting support surface. Platform roles receive all
keys. School prebuilt roles receive request-side defaults (school-wide viewing,
commenting, attachments). Ticket *creation* is deliberately keyless — any
authenticated active user may file a ticket.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

MODULE_NAME = "tickets"
MODULE_DESCRIPTION = "Support ticketing for bug reports, help requests, and issue tracking."
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]
SCHOOL_ROLE_KEYS = ["school_admin", "branch_admin", "teacher"]
SCHOOL_DEFAULT_KEYS = {
    "tickets.ticket.view",
    "tickets.comment.post",
    "tickets.attachment.create",
}
SCHOOL_ADMIN_EXTRA_KEYS = {
    "tickets.ticket.update",
    "tickets.ticket.manage",
    "tickets.report.view",
}
_RESTRICTED = {"SENSITIVE", "CRITICAL"}

TICKET_RESOURCES = [
    ("ticket", "support tickets", [
        ("view", "NORMAL"),
        ("update", "NORMAL"),
        ("manage", "SENSITIVE"),
        ("assign", "SENSITIVE"),
    ]),
    ("comment", "ticket comments", [("post", "NORMAL")]),
    ("internal_note", "ticket internal notes", [("post", "SENSITIVE")]),
    ("attachment", "ticket attachments", [("create", "NORMAL")]),
    ("audit", "ticket audit logs", [("view", "SENSITIVE")]),
    ("report", "ticket reports", [("view", "NORMAL")]),
]


class Command(BaseCommand):
    help = "Seed vs_tickets permission keys and default grants."

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import (
            Permission,
            PermissionAction,
            PermissionModule,
            PermissionResource,
            PlatformRolePermission,
            PlatformRoleTemplate,
            PrebuiltRolePermission,
            PrebuiltRoleTemplate,
            SchoolRolePermission,
            SchoolRoleTemplate,
        )

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n  Seeding {MODULE_NAME} permissions...\n"))

        needed_actions = {action for _, _, actions in TICKET_RESOURCES for action, _ in actions}
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
        for resource_name, resource_label, actions in TICKET_RESOURCES:
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

        for role_id in PLATFORM_ROLE_IDS:
            role = PlatformRoleTemplate.objects.filter(id=role_id).first()
            if role is None:
                self.stdout.write(self.style.WARNING(f"  role '{role_id}' missing; platform grants skipped."))
                continue
            granted = 0
            for perm in all_perms:
                _, link_created = PlatformRolePermission.objects.get_or_create(
                    role=role,
                    permission=perm,
                    defaults={"granted": True, "granted_by": None},
                )
                granted += int(link_created)
            self.stdout.write(f"  {role_id}: granted {granted} new key(s).")

        role_default_keys = {key: set(SCHOOL_DEFAULT_KEYS) for key in SCHOOL_ROLE_KEYS}
        role_default_keys["school_admin"] |= SCHOOL_ADMIN_EXTRA_KEYS
        role_default_keys["branch_admin"] |= SCHOOL_ADMIN_EXTRA_KEYS

        for role_key, keys in role_default_keys.items():
            role = PrebuiltRoleTemplate.objects.filter(key=role_key).first()
            if role is None:
                self.stdout.write(self.style.WARNING(f"  prebuilt role '{role_key}' missing; defaults skipped."))
                continue
            attached = 0
            for key in sorted(keys):
                _, link_created = PrebuiltRolePermission.objects.get_or_create(
                    prebuilt_role=role,
                    permission_id=key,
                )
                attached += int(link_created)
            self.stdout.write(f"  {role_key}: attached {attached} new default(s).")

        templates = SchoolRoleTemplate.all_objects.filter(prebuilt_from__key__in=SCHOOL_ROLE_KEYS).select_related("prebuilt_from")
        backfilled = 0
        template_count = 0
        for template in templates:
            template_count += 1
            keys = role_default_keys.get(template.prebuilt_from.key, set())
            for key in sorted(keys):
                _, row_created = SchoolRolePermission.objects.get_or_create(
                    role=template,
                    permission_id=key,
                    defaults={"granted": True, "granted_by": None},
                )
                backfilled += int(row_created)

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_perms} new permission(s), {len(all_perms)} total ticket keys; "
            f"backfilled {backfilled} grant(s) across {template_count} school role template(s).\n"
        ))
