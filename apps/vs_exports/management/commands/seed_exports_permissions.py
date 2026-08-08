"""Seed vs_exports permission keys and grant them to platform roles (idempotent).

Registers every ``exports.<resource>.<action>`` key enforced by the Export Centre views
into the RBAC Permission registry.

Two keys are deliberately *not* granted with the rest:

``exports.sensitive_field.export``
    Including a restricted column is a separate decision from being allowed to export
    at all. Granting it by default would make "sensitive" mean nothing.

``exports.activity.view``
    Reading other people's export activity is an administrator's power, and the read is
    itself audited. Only the super-admin role gets it.

Run order::

    python manage.py seed_actions
    python manage.py create_superuser
    python manage.py seed_exports_permissions

Safe to re-run - all operations are idempotent.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

MODULE_NAME = "exports"
MODULE_DESCRIPTION = "Export Centre - build, run and download data exports."
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]
_PLATFORM_ROLE_NAMES = {
    "xvs_super_admin": "XVS Super Admin",
    "xvs_platform_admin": "XVS Platform Admin",
}

_RESTRICTED = {"SENSITIVE", "CRITICAL"}

#: Keys only the super-admin role receives - see the module docstring.
SUPER_ADMIN_ONLY = {"exports.sensitive_field.export", "exports.activity.view"}

# (resource_name, resource_label, [(action, sensitivity), ...])
EXPORTS_RESOURCES = [
    ("catalogue",       "the dataset catalogue", [("view", "NORMAL")]),
    ("definition",      "saved exports",         [("view", "NORMAL"), ("create", "NORMAL"),
                                                  ("update", "NORMAL"), ("delete", "SENSITIVE"),
                                                  ("share", "SENSITIVE")]),
    ("run",             "export runs",           [("view", "NORMAL"), ("create", "NORMAL"),
                                                  ("cancel", "NORMAL")]),
    ("file",            "produced files",        [("download", "SENSITIVE")]),
    ("sensitive_field", "restricted fields in exports", [("export", "CRITICAL")]),
    ("activity",        "other people's export activity", [("view", "CRITICAL")]),
]


class Command(BaseCommand):
    help = "Seed vs_exports permission keys and grant them to platform admin roles."

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import (
            Permission,
            PermissionAction,
            PermissionModule,
            PermissionResource,
            TenantRolePermission,
            TenantRoleTemplate,
        )
        from vs_tenants.models import Tenant

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n  Seeding {MODULE_NAME} permissions...\n"))

        # seed_actions owns the canonical verbs; this is a safety net for standalone runs.
        needed_actions = {a for _, _, acts in EXPORTS_RESOURCES for a, _ in acts}
        for name in sorted(needed_actions):
            _, created = PermissionAction.objects.get_or_create(
                name=name,
                defaults={
                    "description": f"Auto-registered action verb '{name}'.",
                    "is_active": True,
                },
            )
            if created:
                self.stdout.write(
                    f"  + action '{name}' (auto-registered - run seed_actions for full description)"
                )

        module, created = PermissionModule.objects.get_or_create(
            name=MODULE_NAME,
            defaults={"description": MODULE_DESCRIPTION, "is_active": True},
        )
        self.stdout.write(f"  module '{MODULE_NAME}' " + ("created" if created else "exists"))

        created_perms = 0
        all_perms = []
        for resource_name, resource_label, actions in EXPORTS_RESOURCES:
            resource, _ = PermissionResource.objects.get_or_create(
                module=module,
                name=resource_name,
                defaults={
                    "description": f"{resource_label.capitalize()} ({MODULE_NAME}).",
                    "is_active": True,
                },
            )
            for action_name, sensitivity in actions:
                action = PermissionAction.objects.get(name=action_name)
                expected_key = f"{MODULE_NAME}.{resource_name}.{action_name}"
                verb = action_name.replace("_", " ")

                perm = Permission.objects.filter(key=expected_key).first()
                if perm is None:
                    perm = Permission(
                        module=module,
                        resource=resource,
                        action=action,
                        description=f"{verb.capitalize()} {resource_label}.",
                        sensitivity_level=sensitivity,
                        is_restricted=sensitivity in _RESTRICTED,
                        is_active=True,
                    )
                    perm.save()
                    created_perms += 1
                    self.stdout.write(f"  + {perm.key}  [{sensitivity}]")
                all_perms.append(perm)

        codex = Tenant.objects.filter(slug="codex", kind=Tenant.Kind.PLATFORM).first()
        if codex is None:
            self.stdout.write(self.style.WARNING(
                "  ⚠  Codex platform tenant not found - run migrations first; grants skipped."
            ))
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
                grantable = [
                    p for p in all_perms
                    if role_id == "xvs_super_admin" or p.key not in SUPER_ADMIN_ONLY
                ]
                granted = 0
                for perm in grantable:
                    _, link_created = TenantRolePermission.objects.get_or_create(
                        role=role, permission=perm,
                        defaults={"granted": True, "granted_by": None},
                    )
                    if link_created:
                        granted += 1
                self.stdout.write(
                    f"  {role_id}: granted {granted} new key(s)." if granted
                    else f"  {role_id}: all keys already assigned."
                )

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_perms} new permission(s), {len(all_perms)} total "
            f"'{MODULE_NAME}' keys registered.\n"
        ))
