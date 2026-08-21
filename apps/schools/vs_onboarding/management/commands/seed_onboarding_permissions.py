"""Seed the onboarding permission module and attach its defaults.

This is the single source of truth for the eight ``onboarding.*`` keys. It
registers the module, its resources and its permissions, then attaches the
school-facing ones to the ``school_admin`` prebuilt role and the single
read-only one to ``branch_admin``, backfills both into school role templates
that were provisioned before these keys existed, and grants the platform-only
ones to the two platform roles.

Run order::

    python manage.py seed_actions                    # canonical action verbs
    python manage.py seed_prebuilt_role_templates    # school_admin/branch_admin/teacher
    python manage.py seed_onboarding_permissions

Every action used here (view, create, update, submit, approve, reject,
reactivate) is already a seeded verb. None is invented: a key whose action is
not in the canonical list cannot be created at all, which is exactly why an
earlier draft of this module specified three keys that could never have been
built.

Safe to re-run - everything uses get_or_create. Supports ``--dry-run``.
"""
import re

from django.core.management.base import BaseCommand
from django.db import transaction


_NORMAL, _SENSITIVE, _CRITICAL = "NORMAL", "SENSITIVE", "CRITICAL"

ROLE_SCHOOL_ADMIN = "school_admin"
ROLE_BRANCH_ADMIN = "branch_admin"
SCHOOL_ROLE_KEYS = [ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN]
PLATFORM_ROLE_KEYS = ["xvs_super_admin", "xvs_platform_admin"]

MODULE_NAME = "onboarding"
MODULE_DESCRIPTION = (
    "School onboarding - the control room, its checklist and the go-live gate."
)

RESOURCE_DESCRIPTIONS = {
    "progress": "Onboarding progress and readiness for a school",
    "task": "Onboarding checklist tasks",
    "go_live": "Go-live requests and their review",
}

# (resource, action, sensitivity, description, school_admin, branch_admin, platform)
#
# The split is the product decision the FRD encodes: a school runs its own
# onboarding and asks to go live; the platform provisions the control room and
# decides on the request. Approve and reject are CRITICAL because between them
# they open every other module to a tenant.
#
# The branch_admin column is narrower than school_admin and holds exactly one
# key, and the reason is worth stating: a branch admin needs to KNOW where the
# school stands - whether the books arrived, what is still blocking go-live -
# because they are often the person being chased for one of the steps. What they
# do not do is run onboarding. Onboarding belongs to the school as a whole, not
# to a site, so transitioning a step and asking CodeX to go live stay with the
# school administrator. Read the state, change nothing.
ONBOARDING_PERMISSIONS: list[tuple[str, str, str, str, bool, bool, bool]] = [
    ("progress", "view",   _NORMAL,    "Read the onboarding control room state.",        True,  True,  True),
    ("progress", "create", _SENSITIVE, "Provision or re-provision onboarding state.",    False, False, True),
    # Reinstatement is platform-only for a reason that is not a policy choice:
    # a suspended school cannot authenticate, so nobody inside it could ever
    # call the endpoint this key gates.
    ("progress", "reactivate", _SENSITIVE, "Return a suspended school to onboarding.",   False, False, True),
    ("task",     "update", _NORMAL,    "Transition an onboarding checklist task.",       True,  False, True),
    ("go_live",  "submit", _SENSITIVE, "Submit a go-live request.",                      True,  False, False),
    ("go_live",  "view",   _NORMAL,    "Read current and historical go-live requests.",  True,  False, True),
    ("go_live",  "approve", _CRITICAL, "Approve a go-live request and activate the school.", False, False, True),
    ("go_live",  "reject",  _CRITICAL, "Reject a go-live request with a reason.",        False, False, True),
]


class Command(BaseCommand):
    help = (
        "Seed the onboarding permission module, attach the school_admin "
        "defaults, backfill existing school role templates and grant the "
        "platform-only keys to the platform roles (idempotent)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without touching the DB.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            # Run for real inside a transaction that is then rolled back, so
            # the counts printed are the counts that would happen.
            try:
                with transaction.atomic():
                    self._run(dry_run=True)
                    raise _DryRunRollback()
            except _DryRunRollback:
                self.stdout.write(self.style.WARNING(
                    "\n  [dry-run] All changes rolled back. Nothing was written.\n"
                ))
        else:
            with transaction.atomic():
                self._run(dry_run=False)

    def _run(self, dry_run: bool):
        from vs_rbac.models import (
            Permission,
            PermissionAction,
            PermissionModule,
            PermissionResource,
            PrebuiltRolePermission,
            PrebuiltRoleTemplate,
            TenantRolePermission,
            TenantRoleTemplate,
            PermissionScope,
        )
        from vs_tenants.models import Tenant

        prefix = "  [dry-run]" if dry_run else " "

        # ── Phase 1: register the module, resources and permissions ───────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Phase 1 - registering onboarding permissions...\n"
        ))

        module, created = PermissionModule.objects.get_or_create(
            name=MODULE_NAME,
            defaults={"description": MODULE_DESCRIPTION, "is_active": True},
        )
        if created:
            self.stdout.write(f"{prefix} + module: {MODULE_NAME}")

        resources: dict[str, PermissionResource] = {}
        created_perm_count = 0
        school_keys: list[str] = []
        branch_keys: list[str] = []
        platform_keys: list[str] = []
        all_keys: list[str] = []

        for (
            resource_name, action_name, sensitivity, description,
            for_school, for_branch, for_platform,
        ) in ONBOARDING_PERMISSIONS:
            resource = resources.get(resource_name)
            if resource is None:
                resource, _ = PermissionResource.objects.get_or_create(
                    module=module,
                    name=resource_name,
                    defaults={
                        "description": RESOURCE_DESCRIPTIONS.get(
                            resource_name, resource_name,
                        ),
                        "is_active": True,
                    },
                )
                resources[resource_name] = resource

            action = PermissionAction.objects.filter(name=action_name).first()
            if not action:
                self.stdout.write(self.style.WARNING(
                    f"  !  Action '{action_name}' not found - run seed_actions "
                    f"first. Skipping {MODULE_NAME}.{resource_name}.{action_name}."
                ))
                continue

            expected_key = f"{MODULE_NAME}.{resource_name}.{action_name}"
            all_keys.append(expected_key)
            if for_school:
                school_keys.append(expected_key)
            if for_branch:
                branch_keys.append(expected_key)
            if for_platform:
                platform_keys.append(expected_key)

            perm = Permission.objects.filter(key=expected_key).first()
            if perm:
                self.stdout.write(f"    {expected_key} (exists)")
            else:
                perm = Permission(
                    module=module,
                    resource=resource,
                    action=action,
                    description=description,
                    is_restricted=sensitivity in (_SENSITIVE, _CRITICAL),
                    sensitivity_level=sensitivity,
                    is_active=True,
                    scope=PermissionScope.TENANT,
                )
                perm.save()
                created_perm_count += 1
                self.stdout.write(f"{prefix} + {perm.key}")

        # ── Phase 2: school-side prebuilt defaults ────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Phase 2 - attaching school-side defaults...\n"
        ))

        keys_by_role = {
            ROLE_SCHOOL_ADMIN: school_keys,
            ROLE_BRANCH_ADMIN: branch_keys,
        }
        for role_key in SCHOOL_ROLE_KEYS:
            role_keys = keys_by_role[role_key]
            prebuilt = PrebuiltRoleTemplate.objects.filter(key=role_key).first()
            if prebuilt is None:
                self.stdout.write(self.style.WARNING(
                    f"  !  Prebuilt role '{role_key}' not found - run "
                    f"seed_prebuilt_role_templates first. Skipping its defaults."
                ))
                continue
            attached = 0
            for key in role_keys:
                _, link_created = PrebuiltRolePermission.objects.get_or_create(
                    prebuilt_role=prebuilt, permission_id=key,
                )
                if link_created:
                    attached += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"{prefix} {role_key}: attached {attached} new "
                    f"default(s) ({len(role_keys)} total)."
                )
                if attached else
                f"{prefix} {role_key}: all {len(role_keys)} defaults already attached."
            )

        # ── Phase 3: backfill existing school role templates ──────────────────
        # A school provisioned before these keys existed has a school_admin
        # role template with none of them, and nobody would ever notice: the
        # control room would simply answer 403 to the only person who needs it.
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Phase 3 - backfilling existing school role templates...\n"
        ))

        # Only the whole-tenant templates. A branch-pinned copy
        # (``branch_admin-12``) must not gain onboarding keys: onboarding
        # belongs to the school as a whole, and a key scoped to one site would
        # be answering a question that was never about that site.
        existing_roles = [
            role
            for role in TenantRoleTemplate.objects.filter(
                tenant__kind="SCHOOL", is_system_role=True,
            ).only("id", "key")
            if role.key in SCHOOL_ROLE_KEYS
        ]

        total_backfilled = 0
        for role in existing_roles:
            role_pk = role.pk
            granted_here = 0
            for key in keys_by_role[role.key]:
                # get_or_create with granted=True in defaults leaves an existing
                # row alone, so an admin's explicit deny is never flipped back.
                _, row_created = TenantRolePermission.objects.get_or_create(
                    role_id=role_pk,
                    permission_id=key,
                    defaults={"granted": True, "granted_by": None},
                )
                if row_created:
                    granted_here += 1
            total_backfilled += granted_here
            if granted_here:
                self.stdout.write(
                    f"{prefix} tenant role #{role_pk} ({role.key}): "
                    f"+{granted_here} grant(s)."
                )
        if not existing_roles:
            self.stdout.write("  No existing school role templates to backfill.")

        # ── Phase 4: platform roles ───────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Phase 4 - granting the platform keys...\n"
        ))

        codex = Tenant.objects.filter(slug="codex", kind=Tenant.Kind.PLATFORM).first()
        if codex is None:
            self.stdout.write(self.style.WARNING(
                "  !  Codex platform tenant not found - run migrations first. "
                "Skipping platform grants."
            ))
        else:
            for role_key in PLATFORM_ROLE_KEYS:
                role = TenantRoleTemplate.objects.filter(
                    tenant=codex, key=role_key,
                ).first()
                if role is None:
                    self.stdout.write(self.style.WARNING(
                        f"  !  Platform role '{role_key}' not found - run "
                        f"create_superuser first. Skipping its grants."
                    ))
                    continue
                granted = 0
                for key in platform_keys:
                    _, link_created = TenantRolePermission.objects.get_or_create(
                        role=role,
                        permission_id=key,
                        defaults={"granted": True, "granted_by": None},
                    )
                    if link_created:
                        granted += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{prefix} {role_key}: granted {granted} new permission(s)."
                    )
                    if granted else
                    f"{prefix} {role_key}: all onboarding keys already assigned."
                )

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_perm_count} new permission(s) created, "
            f"{len(all_keys)} onboarding keys registered; backfilled "
            f"{total_backfilled} grant(s) across {len(existing_roles)} existing "
            f"role template(s).\n"
        ))


class _DryRunRollback(Exception):
    """Internal sentinel to roll back the transaction in --dry-run mode."""
