"""
Seed all RBAC permission keys for the vs_import_data app.

Run once after initial setup (safe to re-run - uses get_or_create):

    python manage.py seed_import_permissions

The super-admin receives every import permission. The platform-admin receives
only template-management permissions; other import operations must be granted
deliberately.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction


S_NORMAL    = "NORMAL"
S_SENSITIVE = "SENSITIVE"

PLATFORM_ROLE_NAMES = {
    "xvs_super_admin": "XVS Super Admin",
    "xvs_platform_admin": "XVS Platform Admin",
}

#: What a SCHOOL administrator may do with the import engine.
#:
#: A school loads its own roll at onboarding - "Import your initial data" is a
#: step on its checklist - and until this existed the school_admin prebuilt role
#: carried none of these keys, so the step could be asked for and never done.
#:
#: Narrower than the platform set on purpose, and each exclusion is a decision:
#:
#: - ``templates.create`` / ``templates.manage`` shape what a valid file IS.
#:   That is platform configuration; a school picks a template, it does not
#:   write one.
#: - ``batches.update`` / ``batches.delete`` rewrite or erase the record of an
#:   import. A school corrects its data by uploading a corrected file, which is
#:   a new batch and leaves the old one legible.
#: - ``rollbacks.*`` unwind data that is already live. That is a support action
#:   with a person on the other end of it, not a button on an onboarding screen.
#: - ``audit.view`` / ``notifications.view`` are platform observability over
#:   every tenant's imports.
#: - ``validations.update`` resolves an issue in place. The school-facing flow
#:   is fix-the-file-and-upload-again, which keeps the file and the data in
#:   step; resolving in place lets them drift.
SCHOOL_ADMIN_IMPORT_KEYS = {
    "import.templates.view",
    "import.batches.view",
    "import.batches.create",
    "import.batches.run",
    "import.batches.import",
    "import.validations.view",
    "import.jobs.view",
}


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
            TenantRolePermission,
            TenantRoleTemplate,
            PermissionScope,
        )
        from vs_tenants.models import Tenant

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
                        self.style.WARNING(f"  ⚠  Action '{action_name}' not found - run seed_actions first.")
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
                        "scope": PermissionScope.TENANT,
                        "is_active": True,
                    },
                )
                if perm_created:
                    created_count += 1
                    self.stdout.write(f"  + {key}")

        # The super-admin is unrestricted. The platform-admin gets only the
        # template permissions required for the template administration UI.
        codex = Tenant.objects.filter(slug="codex", kind=Tenant.Kind.PLATFORM).first()
        if codex is None:
            self.stdout.write(self.style.WARNING(
                "\n  ⚠  Codex platform tenant not found - run migrations first; grants skipped."
            ))
        else:
            role_permission_keys = {
                "xvs_super_admin": set(all_keys),
                "xvs_platform_admin": {
                    key for key in all_keys if key.startswith("import.templates.")
                },
            }
            for role_key, role_name in PLATFORM_ROLE_NAMES.items():
                role, _ = TenantRoleTemplate.objects.get_or_create(
                    tenant=codex,
                    key=role_key,
                    defaults={
                        "name": role_name,
                        "status": "ACTIVE",
                        "is_system_role": True,
                        "is_locked": True,
                    },
                )
                allowed_keys = role_permission_keys[role_key]

                # Repair deployments that previously gave platform-admin every
                # import permission. This role is system-managed, so its seeded
                # import grants must match the intended least-privilege set.
                TenantRolePermission.objects.filter(
                    role=role,
                    permission__key__startswith="import.",
                    granted=True,
                ).exclude(permission_id__in=allowed_keys).delete()

                granted = 0
                for perm in Permission.objects.filter(key__in=allowed_keys):
                    role_perm, role_perm_created = TenantRolePermission.objects.get_or_create(
                        role=role,
                        permission=perm,
                        defaults={"granted": True, "granted_by": None},
                    )
                    if not role_perm_created and not role_perm.granted:
                        role_perm.granted = True
                        role_perm.save(update_fields=["granted", "updated_at"])
                    if role_perm_created or role_perm.granted:
                        granted += 1
                self.stdout.write(
                    f"\n  Ensured {granted} import permissions for {role_key} role."
                )

        # -- School-side defaults ----------------------------------------------
        # Attached to the prebuilt template AND backfilled into schools that
        # already exist, for the same reason every seeder in this repo does
        # both: without the backfill the keys only ever reach schools created
        # after today, and every existing school admin keeps getting a 403
        # nobody can explain.
        self._seed_school_admin_defaults()

        # -- Permission Groups -------------------------------------------------
        self._seed_permission_groups(all_keys)

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_count} new permission(s) created, {len(all_keys)} total import keys registered.\n"
        ))

    def _seed_school_admin_defaults(self) -> None:
        from vs_rbac.models import (
            Permission,
            PrebuiltRolePermission,
            PrebuiltRoleTemplate,
            TenantRolePermission,
            TenantRoleTemplate,
        )

        keys = sorted(
            Permission.objects
            .filter(key__in=SCHOOL_ADMIN_IMPORT_KEYS)
            .values_list("key", flat=True)
        )
        missing = SCHOOL_ADMIN_IMPORT_KEYS - set(keys)
        if missing:
            self.stdout.write(self.style.WARNING(
                f"\n  ⚠  Not registered, so not granted: {', '.join(sorted(missing))}"
            ))

        prebuilt = PrebuiltRoleTemplate.objects.filter(key="school_admin").first()
        if prebuilt is None:
            self.stdout.write(self.style.WARNING(
                "\n  ⚠  Prebuilt role 'school_admin' not found - run "
                "seed_prebuilt_role_templates first; school grants skipped."
            ))
            return

        attached = 0
        for key in keys:
            _, created = PrebuiltRolePermission.objects.get_or_create(
                prebuilt_role=prebuilt, permission_id=key,
            )
            attached += int(created)

        backfilled = 0
        roles = [
            role for role in TenantRoleTemplate.objects.filter(
                tenant__kind="SCHOOL", is_system_role=True,
            ).only("id", "key")
            # The whole-tenant template only. A branch-pinned copy must not gain
            # the keys that load the whole school's roll.
            if role.key == "school_admin"
        ]
        for role in roles:
            for key in keys:
                # get_or_create leaves an existing row alone, so an explicit
                # deny an administrator set is never flipped back on.
                _, created = TenantRolePermission.objects.get_or_create(
                    role=role, permission_id=key,
                    defaults={"granted": True, "granted_by": None},
                )
                backfilled += int(created)

        self.stdout.write(
            f"\n  school_admin: {len(keys)} import key(s) - "
            f"{attached} newly attached, {backfilled} backfilled across "
            f"{len(roles)} existing role template(s)."
        )

    def _seed_permission_groups(self, all_keys: list[str]) -> None:
        from vs_rbac.models import (
            GroupPermission,
            Permission,
            PermissionGroup,
            PermissionScope,
        )

        TEMPLATE_KEYS = [k for k in all_keys if k.startswith("import.templates.")]
        BATCH_KEYS    = [k for k in all_keys if k.startswith("import.batches.")]

        groups = [
            (
                "Data Import - all",
                "Full access to the entire data import pipeline - templates, batches, jobs, and related resources.",
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
                    # ``PermissionGroup.scope`` has no default, deliberately, so
                    # every creation path has to declare it. Migration 0007
                    # classified the groups that already existed; a group seeded
                    # after it without this line is created unclassified, and
                    # ``TenantRoleGroup`` refuses to attach an unclassified
                    # bundle to any role inside a tenant. Every import key is
                    # TENANT-scoped (see the Permission rows above), so the
                    # bundle is too.
                    "scope": PermissionScope.TENANT,
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
