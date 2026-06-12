"""Seed the `platform` permission module and grant it to the platform roles.

This is the single source of truth for the platform-administration permissions
(permissions registry, roles, team, staff profiles, payroll, organogram,
schools, branches, audit, dashboard). Both ``create_superuser`` and
``seed_all_permissions`` run this — keeping the keys defined in exactly one
place so a new resource (e.g. organogram) can never again be wired into views
but forgotten by the seed.

    python manage.py seed_actions                 # action verbs (prerequisite)
    python manage.py create_superuser             # platform roles (prerequisite)
    python manage.py seed_platform_permissions

Grants: every platform permission to ``xvs_super_admin``; the same set EXCEPT
``platform.roles.transfer`` to ``xvs_platform_admin`` (only the Super Admin may
hand off the Super Admin role). Safe to re-run — everything uses get_or_create.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


# Resource → (description, [(action, description, is_restricted, sensitivity), ...]).
# The full permission key is f"platform.{resource}.{action}".
_NORMAL, _SENSITIVE, _CRITICAL = "NORMAL", "SENSITIVE", "CRITICAL"

PLATFORM_RESOURCES: list[tuple[str, str, list[tuple[str, str, bool, str]]]] = [
    (
        "permissions",
        "Global permission registry management",
        [
            ("view",   "View global permission registry",  False, _NORMAL),
            ("create", "Add new permissions",              False, _NORMAL),
            ("update", "Edit permission metadata",         False, _NORMAL),
            ("manage", "Manage groups and dependencies",   True,  _SENSITIVE),
            ("delete", "Delete permissions from registry", True,  _NORMAL),
        ],
    ),
    (
        "roles",
        "Platform role template management",
        [
            ("view",     "View platform roles",                       False, _NORMAL),
            ("create",   "Create new platform roles",                 False, _NORMAL),
            ("update",   "Edit platform role metadata",               False, _NORMAL),
            ("assign",   "Assign roles to users",                     True,  _SENSITIVE),
            ("manage",   "Full control over platform roles",          True,  _SENSITIVE),
            ("delete",   "Delete platform roles",                     True,  _SENSITIVE),
            ("transfer", "Transfer Super Admin role to another user", True,  _CRITICAL),
        ],
    ),
    (
        "team",
        "Vision staff team management",
        [
            ("view",       "View Vision team members",         False, _NORMAL),
            ("create",     "Invite new Vision team members",   False, _NORMAL),
            ("update",     "Edit a team member profile",       False, _NORMAL),
            ("delete",     "Permanently remove a team member", True,  _SENSITIVE),
            ("suspend",    "Suspend a team member account",    True,  _SENSITIVE),
            ("reactivate", "Reactivate a suspended account",   True,  _SENSITIVE),
        ],
    ),
    (
        "staff_profile",
        "CX staff HR / personal profile records",
        [
            ("view",   "View CX staff profiles",    False, _NORMAL),
            ("create", "Create a CX staff profile", False, _NORMAL),
            ("update", "Edit a CX staff profile",   False, _NORMAL),
        ],
    ),
    (
        "staff_payroll",
        "CX staff sensitive payroll / bank details (FLS-gated)",
        [
            ("view",   "View staff bank / payroll details", False, _SENSITIVE),
            ("manage", "Edit staff bank / payroll details", True,  _CRITICAL),
        ],
    ),
    (
        "organogram",
        "CX organogram — departments, positions, assignments, matrix lines",
        [
            ("view",   "View the org chart and its records",        False, _NORMAL),
            ("manage", "Edit departments, positions and assignments", True, _SENSITIVE),
        ],
    ),
    (
        "schools",
        "Customer school management",
        [
            ("view",   "View school list and detail",          False, _NORMAL),
            ("create", "Onboard a new school",                 False, _NORMAL),
            ("update", "Edit school info and settings",        False, _NORMAL),
            ("delete", "Decommission a school record",         True,  _SENSITIVE),
            ("manage", "Full school lifecycle administration", True,  _SENSITIVE),
        ],
    ),
    (
        "branches",
        "School branch management",
        [
            ("view",   "View branches under a school", False, _NORMAL),
            ("create", "Add a new branch to a school", False, _NORMAL),
            ("update", "Edit branch details",          False, _NORMAL),
            ("manage", "Transition branch lifecycle",  True,  _SENSITIVE),
        ],
    ),
    (
        "audit",
        "Audit and compliance",
        [
            ("view",   "View audit events and entity trails", False, _NORMAL),
            ("export", "Export audit data to file",           True,  _SENSITIVE),
            ("manage", "Create and manage compliance rules",  True,  _SENSITIVE),
        ],
    ),
    (
        "dashboard",
        "Platform overview dashboard",
        [
            ("view", "View the platform overview dashboard", False, _NORMAL),
        ],
    ),
]

# Only the Super Admin may transfer the Super Admin role.
TRANSFER_KEY = "platform.roles.transfer"
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]


class Command(BaseCommand):
    help = "Seed the platform permission module and grant it to the platform admin roles."

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

        self.stdout.write(self.style.MIGRATE_HEADING("\n  Seeding platform permissions...\n"))

        module, created = PermissionModule.objects.get_or_create(
            name="platform",
            defaults={"description": "Vision platform administration", "is_active": True},
        )
        if created:
            self.stdout.write("  Created module: platform")

        created_count = 0
        all_perms: list[Permission] = []

        for resource_name, resource_desc, action_specs in PLATFORM_RESOURCES:
            resource, _ = PermissionResource.objects.get_or_create(
                module=module,
                name=resource_name,
                defaults={"description": resource_desc, "is_active": True},
            )

            for action_name, description, is_restricted, sensitivity in action_specs:
                action = PermissionAction.objects.filter(name=action_name).first()
                if not action:
                    self.stdout.write(self.style.WARNING(
                        f"  ⚠  Action '{action_name}' not found — run seed_actions first."
                    ))
                    continue

                expected_key = f"platform.{resource_name}.{action_name}"
                perm = Permission.objects.filter(key=expected_key).first()
                if perm:
                    self.stdout.write(f"    {expected_key} (exists)")
                else:
                    perm = Permission(
                        module=module,
                        resource=resource,
                        action=action,
                        description=description,
                        is_restricted=is_restricted,
                        sensitivity_level=sensitivity,
                        is_active=True,
                    )
                    perm.save()
                    created_count += 1
                    self.stdout.write(f"  + {perm.key}")

                all_perms.append(perm)

        # ── Grant to platform roles ────────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\n  Granting to platform roles...\n"))

        for role_id in PLATFORM_ROLE_IDS:
            try:
                role = PlatformRoleTemplate.objects.get(id=role_id)
            except PlatformRoleTemplate.DoesNotExist:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠  Role '{role_id}' not found — run create_superuser first."
                ))
                continue

            granted = 0
            for perm in all_perms:
                # Platform Admin gets everything except the Super-Admin handoff.
                if role_id == "xvs_platform_admin" and perm.key == TRANSFER_KEY:
                    continue
                _, link_created = PlatformRolePermission.objects.get_or_create(
                    role=role,
                    permission=perm,
                    defaults={"granted": True, "granted_by": None},
                )
                if link_created:
                    granted += 1

            self.stdout.write(
                self.style.SUCCESS(f"  {role_id}: granted {granted} new permission(s).")
                if granted else
                f"  {role_id}: all permissions already assigned."
            )

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_count} new permission(s) created, "
            f"{len(all_perms)} total platform keys registered.\n"
        ))
