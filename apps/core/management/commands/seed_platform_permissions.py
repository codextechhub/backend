"""Seed the `platform` permission module and grant it to the platform roles.

This is the single source of truth for the platform-administration permissions
(permissions registry, roles, team, staff profiles, payroll, organogram,
schools, branches, audit, dashboard, documents). Both ``create_superuser`` and
``seed_all_permissions`` run this - keeping the keys defined in exactly one
place so a new resource (e.g. organogram) can never again be wired into views
but forgotten by the seed.

    python manage.py seed_actions                 # action verbs (prerequisite)
    python manage.py create_superuser             # platform roles (prerequisite)
    python manage.py seed_platform_permissions

Grants: every platform permission to ``xvs_super_admin``; the same set EXCEPT
``platform.roles.transfer`` to ``xvs_platform_admin`` (only the Super Admin may
hand off the Super Admin role). Safe to re-run - everything uses get_or_create.
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
            ("approve",  "Approve restricted platform role grants",   True,  _CRITICAL),
            ("assign",   "Assign roles to users",                     True,  _SENSITIVE),
            ("manage",   "Full control over platform roles",          True,  _SENSITIVE),
            ("delete",   "Delete platform roles",                     True,  _SENSITIVE),
            ("transfer", "Transfer Super Admin role to another user", True,  _CRITICAL),
        ],
    ),
    (
        "impersonation",
        "Audited support impersonation",
        [
            # Scoped start: the target's tenant kind decides which key is required
            # (see ImpersonationSessionViewSet.get_permissions). start_all covers
            # both CX and school; start_cx / start_school narrow it.
            ("start_all",    "Impersonate any user, including CX staff.", True, _CRITICAL),
            ("start_cx",     "Impersonate CX (platform) staff only.",     True, _CRITICAL),
            ("start_school", "Impersonate school users only.",            True, _CRITICAL),
            ("end",          "End an impersonation session.",             True, _CRITICAL),
            ("view",         "View impersonation sessions.",              True, _CRITICAL),
        ],
    ),
    (
        "team",
        # Staff account management, for whichever tenant holds it. These keys
        # are held inside schools as well as by CodeX (see TENANT_HOLDABLE_KEYS
        # below), so the wording must be true for both readers. It used to say
        # "Vision", which read as CodeX's own staff on a screen where a school
        # was choosing what its own bursar could do.
        "Staff account management",
        [
            ("view",       "View staff accounts",              False, _NORMAL),
            ("create",     "Invite new staff members",         False, _NORMAL),
            ("update",     "Edit a staff member's profile",    False, _NORMAL),
            ("delete",     "Permanently remove a staff account", True, _SENSITIVE),
            ("suspend",    "Suspend a staff account",          True,  _SENSITIVE),
            ("reactivate", "Reactivate a suspended account",   True,  _SENSITIVE),
        ],
    ),
    (
        "team_overrides",
        "Per-user permission exceptions on CX team member profiles",
        [
            # Seeing that a user HAS exceptions is itself sensitive: without
            # this key the affected user cannot learn they have any.
            ("view",   "View a CX user's permission exceptions",            True, _CRITICAL),
            ("manage", "Grant or revoke a CX user's permission exceptions", True, _CRITICAL),
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
        "CX organogram - departments, positions, assignments, matrix lines",
        [
            ("view",   "View organogram summary metrics",             False, _NORMAL),
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
        "tasks",
        "Background task monitor - the engine-room view over every Celery run",
        [
            # The triage surface. Status, task name, owner, timings: enough to
            # answer "did Corona's import finish?" and nothing that names a
            # guardian. Held by both platform roles.
            ("view", "View the background task monitor (redacted)", False, _NORMAL),
            # Every tenant's runs in one list. Without it the monitor is
            # limited to the tenants the holder actually has a role in, which
            # is what turns "browse every customer" into "browse the customer
            # whose ticket you are on".
            ("view_all", "View task runs across every tenant", True, _CRITICAL),
            # The raw, unredacted failure text out of TaskDiagnostic. This is
            # the key that reads somebody's email address out of a Postgres
            # constraint error, so it is CRITICAL, super-admin only, and the
            # read itself emits an audit event naming who did it.
            ("view_sensitive", "Read raw task errors and tracebacks", True, _CRITICAL),
        ],
    ),
    (
        "dashboard",
        "Platform overview dashboard",
        [
            ("view", "View the platform overview dashboard", False, _NORMAL),
        ],
    ),
    (
        "documents",
        "Internal product requirements library (MRD and module FRDs)",
        [
            # These documents describe the whole platform's internals, so the key
            # lives in the `platform` module rather than anywhere a school role
            # could reach: platform roles are seeded on the codex PLATFORM tenant
            # only, which is what keeps the library CX-staff-only.
            ("view", "Browse and download requirements documents", False, _NORMAL),
        ],
    ),
]

# Resources in this module whose keys a TENANT role may legitimately hold.
#
# The ``platform`` prefix names where these surfaces were first built, not who
# is allowed to reach them, and for two families it is simply wrong:
#
# * ``platform.team.*`` gates ``UserAccountViewSet``, which filters its queryset
#   to the caller's own tenant for every non-platform caller. A school admin
#   creating their own staff holds ``platform.team.create`` today - that is what
#   M9 onboarding does.
# * ``platform.audit.view`` / ``platform.audit.export`` are held by audit
#   officers inside a tenant. This app's own committed tests build exactly that
#   user ("outsider holds the very same key, but in a different tenant") and
#   assert they may run and download their own exports.
#
# ``platform.audit.manage`` is deliberately NOT in this list: it edits
# compliance and retention rules through an unscoped queryset, and nothing
# grants it to a tenant.
#
# The 2026-08-22 note: what was wrong with ``platform.team.*`` was never the
# scope but the WORDING. These descriptions were written from CodeX's side of
# the platform, so the school roles screen offered a school admin choosing what
# her bursar could do "Invite new Vision team members". Reworded above rather
# than reclassified - reclassifying would have locked schools out of
# administering their own staff.
TENANT_HOLDABLE_KEYS = {
    "platform.team.view",
    "platform.team.create",
    "platform.team.update",
    "platform.team.delete",
    "platform.team.suspend",
    "platform.team.reactivate",
    "platform.audit.view",
    "platform.audit.export",
}

# Only the Super Admin may transfer the Super Admin role.
TRANSFER_KEY = "platform.roles.transfer"

# Keys withheld from xvs_platform_admin as well, for the same reason the
# transfer key is: they are powers over other people rather than over the
# product. Reading a raw traceback means reading whatever personal data the
# failing row contained, and the cross-tenant list means reading every
# customer at once. Both are the Super Admin's, and both are grantable to a
# named person afterwards if the platform genuinely needs a second holder.
SUPER_ADMIN_ONLY_KEYS = {
    "platform.tasks.view_all",
    "platform.tasks.view_sensitive",
}
# Canonical codex-tenant role keys (mirror the legacy PlatformRoleTemplate ids).
PLATFORM_ROLE_KEYS = ["xvs_super_admin", "xvs_platform_admin"]
_PLATFORM_ROLE_NAMES = {
    "xvs_super_admin": "XVS Super Admin",
    "xvs_platform_admin": "XVS Platform Admin",
}


class Command(BaseCommand):
    help = "Seed the platform permission module and grant it to the platform admin roles."

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import (
            Permission,
            PermissionAction,
            PermissionModule,
            PermissionResource,
            PermissionScope,
            TenantRolePermission,
            TenantRoleTemplate,
        )
        from vs_tenants.models import Tenant

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
                        f"  ⚠  Action '{action_name}' not found - run seed_actions first."
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
                        scope=(
                            PermissionScope.TENANT
                            if expected_key in TENANT_HOLDABLE_KEYS
                            else PermissionScope.PLATFORM
                        ),
                    )
                    perm.save()
                    created_count += 1
                    self.stdout.write(f"  + {perm.key}")

                all_perms.append(perm)

        # ── Grant to platform roles (codex tenant) ─────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\n  Granting to platform roles...\n"))

        codex = Tenant.objects.filter(slug="codex", kind=Tenant.Kind.PLATFORM).first()
        if codex is None:
            self.stdout.write(self.style.WARNING(
                "  ⚠  Codex platform tenant not found - run migrations first. Skipping grants."
            ))
        else:
            for role_key in PLATFORM_ROLE_KEYS:
                # Idempotently ensure the codex-tenant role exists (mirrors the
                # legacy PlatformRoleTemplate ids by key).
                role, _ = TenantRoleTemplate.objects.get_or_create(
                    tenant=codex,
                    key=role_key,
                    defaults={
                        "name": _PLATFORM_ROLE_NAMES.get(role_key, role_key),
                        "status": "ACTIVE",
                        "is_system_role": True,
                        "is_locked": True,
                    },
                )

                granted = 0
                for perm in all_perms:
                    # Platform Admin gets everything except the Super-Admin
                    # handoff and the keys that read other people's data.
                    if role_key == "xvs_platform_admin" and (
                        perm.key == TRANSFER_KEY or perm.key in SUPER_ADMIN_ONLY_KEYS
                    ):
                        continue
                    _, link_created = TenantRolePermission.objects.get_or_create(
                        role=role,
                        permission=perm,
                        defaults={"granted": True, "granted_by": None},
                    )
                    if link_created:
                        granted += 1

                self.stdout.write(
                    self.style.SUCCESS(f"  {role_key}: granted {granted} new permission(s).")
                    if granted else
                    f"  {role_key}: all permissions already assigned."
                )

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_count} new permission(s) created, "
            f"{len(all_perms)} total platform keys registered.\n"
        ))
