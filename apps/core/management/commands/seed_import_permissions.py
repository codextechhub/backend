"""
Seed all RBAC permission keys for the vs_import_data app.

Run once after initial setup (safe to re-run — uses get_or_create):

    python manage.py seed_import_permissions

Also grants every import permission to the xvs_super_admin platform role
so super-admins can exercise all import functionality immediately.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction


S_NORMAL    = "NORMAL"
S_SENSITIVE = "SENSITIVE"


# (resource_name, resource_description, [(action, description, is_restricted, sensitivity), ...])
IMPORT_RESOURCES: list[tuple[str, str, list[tuple[str, str, bool, str]]]] = [
    (
        "templates",
        "System import template definitions",
        [
            ("view",   "List and retrieve system import templates",         False, S_NORMAL),
            ("create", "Create a new system import template with columns",  False, S_SENSITIVE),
            ("manage", "View and edit internal template config fields",     True,  S_SENSITIVE),
        ],
    ),
    (
        "batches",
        "Import batch file upload and lifecycle",
        [
            ("view",   "List and retrieve import batches",                  False, S_NORMAL),
            ("create", "Upload a new import batch file",                    False, S_NORMAL),
            ("update", "Edit import batch metadata",                        False, S_NORMAL),
            ("delete", "Delete an import batch",                            True,  S_SENSITIVE),
            ("run",    "Trigger validation or re-validation on a batch",    False, S_NORMAL),
            ("import", "Start actual import execution on a validated batch", True,  S_SENSITIVE),
        ],
    ),
    (
        "validations",
        "Import batch validation issue management",
        [
            ("view",   "List and retrieve validation issues for a batch",   False, S_NORMAL),
            ("update", "Mark a validation issue as resolved",               False, S_NORMAL),
        ],
    ),
    (
        "corrections",
        "Manual row-level data corrections before import",
        [
            ("view",   "List row corrections for a batch",                  False, S_NORMAL),
            ("create", "Create a manual row correction",                    False, S_NORMAL),
        ],
    ),
    (
        "jobs",
        "Background import job execution records",
        [
            ("view",   "List and retrieve import jobs and row results",     False, S_NORMAL),
        ],
    ),
    (
        "rollbacks",
        "Import rollback operations",
        [
            ("view",   "List rollback history records for a job",           False, S_NORMAL),
            ("run",    "Trigger a rollback on a completed import job",       True,  S_SENSITIVE),
        ],
    ),
    (
        "audit",
        "Import pipeline audit event log",
        [
            ("view",   "List audit events scoped to an import batch",       False, S_NORMAL),
        ],
    ),
    (
        "notifications",
        "Import pipeline delivery notifications",
        [
            ("view",   "List import notifications for a batch",             False, S_NORMAL),
        ],
    ),
]


class Command(BaseCommand):
    help = "Seed RBAC permission keys for the vs_import_data app."

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import (
            Permission,
            PermissionAction,
            PermissionModule,
            PermissionResource,
            PlatformRolePermission,
            PlatformRoleTemplate,
        )

        self.stdout.write(self.style.MIGRATE_HEADING("\n  Seeding import data permissions...\n"))

        module, created = PermissionModule.objects.get_or_create(
            name="import",
            defaults={"description": "Data import pipeline permissions", "is_active": True},
        )
        if created:
            self.stdout.write(f"  Created module: import")

        created_count = 0
        all_keys: list[str] = []

        for resource_name, resource_desc, actions in IMPORT_RESOURCES:
            resource, _ = PermissionResource.objects.get_or_create(
                module=module,
                name=resource_name,
                defaults={"description": resource_desc, "is_active": True},
            )

            for action_name, description, is_restricted, sensitivity in actions:
                action = PermissionAction.objects.filter(name=action_name).first()
                if not action:
                    self.stdout.write(
                        self.style.WARNING(f"  ⚠  Action '{action_name}' not found — run seed_actions first.")
                    )
                    continue

                key = f"import.{resource_name}.{action_name}"
                all_keys.append(key)

                perm, perm_created = Permission.objects.get_or_create(
                    key=key,
                    defaults={
                        "module": module,
                        "resource": resource,
                        "action": action,
                        "description": description,
                        "is_restricted": is_restricted,
                        "sensitivity_level": sensitivity,
                        "is_active": True,
                    },
                )
                if perm_created:
                    created_count += 1
                    self.stdout.write(f"  + {key}")

        # Grant all import permissions to xvs_super_admin role
        try:
            super_admin_role = PlatformRoleTemplate.objects.get(id="xvs_super_admin")
            granted = 0
            for key in all_keys:
                perm = Permission.objects.filter(key=key).first()
                if perm:
                    _, role_perm_created = PlatformRolePermission.objects.get_or_create(
                        role=super_admin_role,
                        permission=perm,
                        defaults={"granted": True, "granted_by": None},
                    )
                    if role_perm_created:
                        granted += 1
            if granted:
                self.stdout.write(f"\n  Granted {granted} import permissions to xvs_super_admin role.")
        except PlatformRoleTemplate.DoesNotExist:
            self.stdout.write(self.style.WARNING(
                "\n  ⚠  'xvs_super_admin' role not found — run create_superuser first."
            ))

        # -- Permission Groups -------------------------------------------------
        self._seed_permission_groups(all_keys)

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_count} new permission(s) created, {len(all_keys)} total import keys registered.\n"
        ))

    def _seed_permission_groups(self, all_keys: list[str]) -> None:
        from vs_rbac.models import GroupPermission, Permission, PermissionGroup

        TEMPLATE_KEYS = [k for k in all_keys if k.startswith("import.templates.")]
        BATCH_KEYS    = [k for k in all_keys if k.startswith("import.batches.")]

        groups = [
            (
                "Data Import - all",
                "Full access to the entire data import pipeline — templates, batches, jobs, and related resources.",
                all_keys,
            ),
            (
                "Import Batch - all",
                "Full access to import batch operations: upload, validate, execute, and delete batches.",
                BATCH_KEYS,
            ),
            (
                "Import Template - all",
                "Full access to import template management: view, create, and manage system templates.",
                TEMPLATE_KEYS,
            ),
        ]

        self.stdout.write(self.style.MIGRATE_HEADING("\n  Seeding import permission groups...\n"))

        for name, description, keys in groups:
            group, created = PermissionGroup.objects.get_or_create(
                name=name,
                defaults={
                    "description": description,
                    "is_system": True,
                    "is_active": True,
                },
            )
            action = "Created" if created else "Found  "
            self.stdout.write(f"  {action} group: {name!r}")

            added = 0
            for key in keys:
                perm = Permission.objects.filter(key=key).first()
                if not perm:
                    continue
                _, link_created = GroupPermission.objects.get_or_create(
                    group=group,
                    permission=perm,
                )
                if link_created:
                    added += 1

            if added:
                self.stdout.write(f"           + linked {added} permission(s)")
