"""Seed the onboarding permission module and attach its defaults.

This is the single source of truth for the eight ``onboarding.*`` keys. It
registers the module, its resources and its permissions, then attaches the
school-facing ones to the ``school_admin`` prebuilt role and the single
read-only one to ``branch_admin``, and grants the platform-only ones to the two
platform roles. A key the library role gains here also reaches each school's
whole-school copy of that role, once, at that moment
(:func:`vs_rbac.library_growth.attach_defaults`): a school provisioned before
the key existed would otherwise find its control room answering 403 to the only
person who needs it, and a key a school later took off stays off.

Run order::

    python manage.py seed_actions                    # canonical action verbs
    python manage.py seed_prebuilt_role_templates    # school_admin/branch_admin/teacher
    python manage.py seed_onboarding_permissions

Every action used here (view, create, update, submit, approve, reject,
reactivate) is already a seeded verb. None is invented: a key whose action is
not in the canonical list cannot be created at all, which is exactly why an
earlier draft of this module specified three keys that could never have been
built.

Safe to re-run. Supports ``--dry-run``.
"""
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
        "defaults, grow schools' copies with newly attached ones and grant the "
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
            # Whole-tenant copies only (branch_copies off). A branch-pinned copy
            # (``branch_admin-12``) must not gain onboarding keys: onboarding
            # belongs to the school as a whole, not to one branch.
            attached, grown = attach_defaults(
                prebuilt, role_keys, branch_copies=False, tenant_kind="SCHOOL",
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"{prefix} {role_key}: attached {len(attached)} new "
                    f"default(s) ({len(role_keys)} total), {grown} grant(s) "
                    f"added to schools' copies."
                )
                if attached else
                f"{prefix} {role_key}: all {len(role_keys)} defaults already attached."
            )

        # ── Phase 3: platform roles ───────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Phase 3 - granting the platform keys...\n"
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
            f"{len(all_keys)} onboarding keys registered.\n"
        ))


class _DryRunRollback(Exception):
    """Internal sentinel to roll back the transaction in --dry-run mode."""
