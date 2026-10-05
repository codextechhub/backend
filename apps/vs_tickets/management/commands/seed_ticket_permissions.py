"""Seed vs_tickets permission keys and attach sensible default grants.

The ticket module is a cross-cutting support surface. Platform roles receive all
keys. School prebuilt roles receive request-side defaults (school-wide viewing,
commenting, attachments). Ticket *creation* is deliberately keyless - any
authenticated active user may file a ticket.

One key is CodeX's alone. Every assignee is support staff, so assigning is the
desk choosing which of its own people works a ticket; a school's say is
escalation, and it stops there. ``tickets.ticket.assign`` is therefore seeded
``PLATFORM`` so a school's roles screen never offers it.

A school's copies of those roles gain a default once, when the library role
gains it, so a key a school took off its own role stays off.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

MODULE_NAME = "tickets"
MODULE_DESCRIPTION = "Support ticketing for bug reports, help requests, and issue tracking."
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]
_PLATFORM_ROLE_NAMES = {"xvs_super_admin": "XVS Super Admin", "xvs_platform_admin": "XVS Platform Admin"}
SCHOOL_ROLE_KEYS = ["school_admin", "branch_admin", "teacher"]
SCHOOL_DEFAULT_KEYS = {
    "tickets.ticket.view",
    "tickets.comment.post",
    "tickets.attachment.create",
}
SCHOOL_ADMIN_EXTRA_KEYS = {
    "tickets.ticket.update",
    "tickets.ticket.triage",
    "tickets.ticket.transition",
    "tickets.ticket.escalate",
    "tickets.report.view",
}
_RESTRICTED = {"SENSITIVE", "CRITICAL"}
#: Keys a school may never hold, whatever module they sit in. See the module
#: docstring, and vs_rbac migration 0019 for the rows already granted.
PLATFORM_ONLY_KEYS = {"tickets.ticket.assign"}

TICKET_RESOURCES = [
    ("ticket", "support tickets", [
        ("view", "NORMAL"),
        ("update", "NORMAL"),
        ("triage", "SENSITIVE"),
        ("transition", "SENSITIVE"),
        ("escalate", "SENSITIVE"),
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
        from vs_rbac.library_growth import attach_defaults
        from vs_rbac.models import (
            Permission,
            PermissionAction,
            PermissionModule,
            PermissionResource,
            PrebuiltRoleTemplate,
            TenantRolePermission,
            TenantRoleTemplate,
            PermissionScope,
        )
        from vs_tenants.models import Tenant

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
                        scope=(
                            PermissionScope.PLATFORM
                            if expected_key in PLATFORM_ONLY_KEYS
                            else PermissionScope.TENANT
                        ),
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

        role_default_keys = {key: set(SCHOOL_DEFAULT_KEYS) for key in SCHOOL_ROLE_KEYS}
        role_default_keys["school_admin"] |= SCHOOL_ADMIN_EXTRA_KEYS
        role_default_keys["branch_admin"] |= SCHOOL_ADMIN_EXTRA_KEYS

        # A default the library role gains here reaches each school's copies
        # once (vs_rbac.library_growth.attach_defaults).
        grown = 0
        for role_key, keys in role_default_keys.items():
            role = PrebuiltRoleTemplate.objects.filter(key=role_key).first()
            if role is None:
                self.stdout.write(self.style.WARNING(f"  prebuilt role '{role_key}' missing; defaults skipped."))
                continue
            attached, added = attach_defaults(
                role, keys, branch_copies=True, tenant_kind="SCHOOL",
            )
            grown += added
            self.stdout.write(
                f"  {role_key}: attached {len(attached)} new default(s), "
                f"{added} grant(s) added to schools' copies."
            )

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_perms} new permission(s), {len(all_perms)} total ticket keys; "
            f"{grown} grant(s) added to schools' copies of the library roles.\n"
        ))
