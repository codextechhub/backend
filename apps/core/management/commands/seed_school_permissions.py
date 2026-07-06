"""Seed the school-scoped permission modules, attach prebuilt-role defaults,
and backfill already-onboarded schools.

This is the single source of truth for the school-facing permission keys used by
school-fe (the XVS school-facing app). It registers two modules — ``school``
(administration / people) and ``academics`` (sessions / calendar / classes) —
then attaches sensible defaults to the ``school_admin`` / ``branch_admin`` /
``teacher`` PrebuiltRoleTemplates and, critically, backfills those defaults into
any existing SchoolRoleTemplate that was provisioned from one of those prebuilt
roles BEFORE these permissions existed.

Run order::

    python manage.py seed_actions                    # canonical action verbs
    python manage.py seed_prebuilt_role_templates    # school_admin/branch_admin/teacher
    python manage.py seed_school_permissions

Three idempotent phases:
  1. Register modules + resources + permissions (with sensitivity per table).
  2. Attach PrebuiltRolePermission defaults per the school_admin/branch_admin/
     teacher columns.
  3. Backfill: for every SchoolRoleTemplate whose ``prebuilt_from.key`` is one of
     the three roles, get_or_create a GRANTED SchoolRolePermission row for each of
     that prebuilt role's default keys. get_or_create never flips an existing
     explicit deny (granted=False) — admin customisations survive.

Safe to re-run — everything uses get_or_create. Supports ``--dry-run``.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


# Sensitivity levels (mirrors seed_platform_permissions).
_NORMAL, _SENSITIVE, _CRITICAL = "NORMAL", "SENSITIVE", "CRITICAL"

# Prebuilt role keys the defaults attach to (and that the backfill scans for).
ROLE_SCHOOL_ADMIN = "school_admin"
ROLE_BRANCH_ADMIN = "branch_admin"
ROLE_TEACHER = "teacher"
PREBUILT_ROLE_KEYS = [ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER]

# ── Permission key table (single source of truth) ─────────────────────────────
# Each row: (dotted_key, description, sensitivity, {default-role flags}).
# The dotted key is module.resource.action. Modules: school, academics.
# `defaults` lists which prebuilt roles receive this permission by default.
#
# This table MUST stay in lockstep with school-fe/src/permissions/index.ts.

# (module, resource, action, sensitivity, roles)
SCHOOL_PERMISSIONS: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    # module: school
    ("school", "dashboard", "view",            _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),

    ("school", "branches", "view",             _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "branches", "create",           _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "branches", "update",           _NORMAL,    (ROLE_SCHOOL_ADMIN,)),
    ("school", "branches", "manage",           _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    ("school", "students", "view",             _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("school", "students", "create",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "students", "update",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "students", "manage",           _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "students", "view_sensitive",   _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),

    ("school", "teachers", "view",             _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("school", "teachers", "create",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "teachers", "update",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "teachers", "manage",           _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    ("school", "administrators", "view",       _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "administrators", "create",     _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "administrators", "update",     _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "administrators", "suspend",    _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "administrators", "reactivate", _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    ("school", "fees", "view",                 _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "fees", "manage",               _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    ("school", "settings", "view",             _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "settings", "manage",           _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    ("school", "roles", "view",                _NORMAL,    (ROLE_SCHOOL_ADMIN,)),
    ("school", "roles", "assign",              _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    # module: academics
    ("academics", "session", "view",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("academics", "session", "create",         _NORMAL,    (ROLE_SCHOOL_ADMIN,)),
    ("academics", "session", "update",         _NORMAL,    (ROLE_SCHOOL_ADMIN,)),
    ("academics", "session", "manage",         _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    ("academics", "calendar", "view",          _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("academics", "calendar", "create",        _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "calendar", "update",        _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "calendar", "manage",        _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    ("academics", "classes", "view",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("academics", "classes", "create",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "classes", "update",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("academics", "classes", "manage",         _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "classes", "assign",         _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
]

MODULES: dict[str, str] = {
    "school": "School administration — branches, people, fees, settings, roles.",
    "academics": "Academic operations — sessions, calendar, classes.",
}

# Short descriptions per resource (for PermissionResource rows).
RESOURCE_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("school", "dashboard"):      "School overview dashboard",
    ("school", "branches"):       "School branch management",
    ("school", "students"):       "Student records",
    ("school", "teachers"):       "Teacher records",
    ("school", "administrators"): "School administrator accounts",
    ("school", "fees"):           "Fees and billing",
    ("school", "settings"):       "School settings",
    ("school", "roles"):          "School role management",
    ("academics", "session"):     "Academic sessions",
    ("academics", "calendar"):    "Academic calendar",
    ("academics", "classes"):     "Classes",
}


class Command(BaseCommand):
    help = (
        "Seed the school + academics permission modules, attach prebuilt-role "
        "defaults, and backfill existing school role templates (idempotent)."
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
            # Wrap the whole thing in an atomic block we roll back so counts are
            # accurate against the real DB without persisting anything.
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
            SchoolRolePermission,
            SchoolRoleTemplate,
        )

        prefix = "  [dry-run]" if dry_run else " "

        # ── Phase 1: register modules, resources, permissions ─────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Phase 1 — registering school + academics permissions...\n"
        ))

        modules: dict[str, PermissionModule] = {}
        for module_name, module_desc in MODULES.items():
            module, created = PermissionModule.objects.get_or_create(
                name=module_name,
                defaults={"description": module_desc, "is_active": True},
            )
            modules[module_name] = module
            if created:
                self.stdout.write(f"{prefix} + module: {module_name}")

        resources: dict[tuple[str, str], PermissionResource] = {}
        created_perm_count = 0
        all_keys: list[str] = []

        for module_name, resource_name, action_name, sensitivity, _roles in SCHOOL_PERMISSIONS:
            module = modules[module_name]

            rkey = (module_name, resource_name)
            resource = resources.get(rkey)
            if resource is None:
                resource, _ = PermissionResource.objects.get_or_create(
                    module=module,
                    name=resource_name,
                    defaults={
                        "description": RESOURCE_DESCRIPTIONS.get(rkey, resource_name),
                        "is_active": True,
                    },
                )
                resources[rkey] = resource

            action = PermissionAction.objects.filter(name=action_name).first()
            if not action:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠  Action '{action_name}' not found — run seed_actions first. "
                    f"Skipping {module_name}.{resource_name}.{action_name}."
                ))
                continue

            expected_key = f"{module_name}.{resource_name}.{action_name}"
            all_keys.append(expected_key)

            is_restricted = sensitivity in (_SENSITIVE, _CRITICAL)
            perm = Permission.objects.filter(key=expected_key).first()
            if perm:
                self.stdout.write(f"    {expected_key} (exists)")
            else:
                perm = Permission(
                    module=module,
                    resource=resource,
                    action=action,
                    description="",
                    is_restricted=is_restricted,
                    sensitivity_level=sensitivity,
                    is_active=True,
                )
                perm.save()
                created_perm_count += 1
                self.stdout.write(f"{prefix} + {perm.key}")

        # ── Phase 2: attach prebuilt-role defaults ────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Phase 2 — attaching prebuilt-role defaults...\n"
        ))

        # Build role -> [keys] from the table.
        role_default_keys: dict[str, list[str]] = {k: [] for k in PREBUILT_ROLE_KEYS}
        for module_name, resource_name, action_name, _sensitivity, roles in SCHOOL_PERMISSIONS:
            key = f"{module_name}.{resource_name}.{action_name}"
            for role_key in roles:
                role_default_keys[role_key].append(key)

        prebuilt_roles: dict[str, PrebuiltRoleTemplate] = {}
        for role_key in PREBUILT_ROLE_KEYS:
            role = PrebuiltRoleTemplate.objects.filter(key=role_key).first()
            if role is None:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠  Prebuilt role '{role_key}' not found — run "
                    f"seed_prebuilt_role_templates first. Skipping its defaults."
                ))
                continue
            prebuilt_roles[role_key] = role

            attached = 0
            for key in role_default_keys[role_key]:
                _, link_created = PrebuiltRolePermission.objects.get_or_create(
                    prebuilt_role=role,
                    permission_id=key,
                )
                if link_created:
                    attached += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"{prefix} {role_key}: attached {attached} new default(s) "
                    f"({len(role_default_keys[role_key])} total)."
                )
                if attached else
                f"{prefix} {role_key}: all {len(role_default_keys[role_key])} defaults already attached."
            )

        # ── Phase 3: backfill existing school role templates ──────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Phase 3 — backfilling existing school role templates...\n"
        ))

        templates = (
            SchoolRoleTemplate.all_objects
            .filter(prebuilt_from__key__in=PREBUILT_ROLE_KEYS)
            .select_related("prebuilt_from")
        )

        total_backfilled = 0
        template_count = 0
        for template in templates:
            template_count += 1
            role_key = template.prebuilt_from.key
            keys = role_default_keys.get(role_key, [])
            granted_here = 0
            for key in keys:
                # get_or_create with granted=True in defaults: if a row already
                # exists (grant OR explicit deny) it is left untouched, so an
                # admin's explicit deny (granted=False) is never flipped.
                _, row_created = SchoolRolePermission.objects.get_or_create(
                    role=template,
                    permission_id=key,
                    defaults={"granted": True, "granted_by": None},
                )
                if row_created:
                    granted_here += 1
            total_backfilled += granted_here
            self.stdout.write(
                f"{prefix} school={template.school_id} role={template.name} "
                f"({role_key}): +{granted_here} grant(s)."
            )

        if template_count == 0:
            self.stdout.write("  No existing school role templates to backfill.")

        # ── Summary ───────────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_perm_count} new permission(s) created, "
            f"{len(all_keys)} school/academics keys registered; "
            f"backfilled {total_backfilled} grant(s) across {template_count} "
            f"existing role template(s).\n"
        ))


class _DryRunRollback(Exception):
    """Internal sentinel to roll back the transaction in --dry-run mode."""
