"""Seed vs_exports permission keys and grant them (idempotent).

Registers every ``exports.<resource>.<action>`` key enforced by the Export Centre views
into the RBAC Permission registry, then grants them to the platform roles AND to the
school prebuilt roles.

**Schools export their own data.** The Export Centre was platform-only until now,
which meant a school administrator pressing Export on their own class list was
refused by RBAC with nothing they could act on. It is their school and their
records; what governs the answer is the dataset's own permission - every dataset
declares one, and ``may_export_dataset`` checks it - so a school user can only
ever export what they can already read on screen.

``exports.sensitive_field.export`` follows the same rule rather than an exception
to it. It IS granted to school_admin, because a school administrator who may read
a restricted column may take it with them; it is not granted to branch_admin or
teacher, and it stays CRITICAL, so including a restricted column remains a
separate decision from being allowed to export at all.

``exports.activity.view`` is still nobody's but the super-admin's. Reading other
people's export activity is an administrator's power over other administrators,
and the read is itself audited.

Run order::

    python manage.py seed_actions
    python manage.py seed_prebuilt_role_templates
    python manage.py create_superuser
    python manage.py seed_exports_permissions

Safe to re-run - all operations are idempotent, an existing explicit deny is
never flipped, and a school's copy of a library role gains a default only once.
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

#: Prebuilt school roles the defaults attach to, and what each one gets.
#:
#: A branch admin and a teacher may export what they can see and take the file;
#: they may not save, share or schedule an export for other people, and they may
#: not include a restricted column. Those are school_admin's.
ROLE_SCHOOL_ADMIN = "school_admin"
ROLE_BRANCH_ADMIN = "branch_admin"
ROLE_TEACHER = "teacher"

_EXPORT_AND_TAKE = (
    "exports.catalogue.view",
    "exports.run.view",
    "exports.run.create",
    "exports.run.cancel",
    "exports.file.download",
)

SCHOOL_ROLE_DEFAULTS: dict[str, tuple[str, ...]] = {
    ROLE_SCHOOL_ADMIN: _EXPORT_AND_TAKE + (
        "exports.definition.view",
        "exports.definition.create",
        "exports.definition.update",
        "exports.definition.delete",
        "exports.definition.share",
        "exports.schedule.view",
        "exports.schedule.create",
        "exports.schedule.update",
        "exports.schedule.delete",
        "exports.schedule.suspend",
        "exports.schedule.reactivate",
        # The whole point of the sensitivity gate: it is held separately, by the
        # one school role trusted with restricted columns.
        "exports.sensitive_field.export",
    ),
    ROLE_BRANCH_ADMIN: _EXPORT_AND_TAKE,
    ROLE_TEACHER: _EXPORT_AND_TAKE,
}

# (resource_name, resource_label, [(action, sensitivity), ...])
EXPORTS_RESOURCES = [
    ("catalogue",       "the dataset catalogue", [("view", "NORMAL")]),
    ("definition",      "saved exports",         [("view", "NORMAL"), ("create", "NORMAL"),
                                                  ("update", "NORMAL"), ("delete", "SENSITIVE"),
                                                  ("share", "SENSITIVE")]),
    ("run",             "export runs",           [("view", "NORMAL"), ("create", "NORMAL"),
                                                  ("cancel", "NORMAL")]),
    ("file",            "produced files",        [("download", "SENSITIVE")]),
    ("schedule",        "export schedules",      [("view", "NORMAL"), ("create", "NORMAL"),
                                                  ("update", "NORMAL"), ("delete", "SENSITIVE"),
                                                  ("suspend", "SENSITIVE"), ("reactivate", "SENSITIVE")]),
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
            PrebuiltRoleTemplate,
            TenantRolePermission,
            TenantRoleTemplate,
            PermissionScope,
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
                        scope=PermissionScope.TENANT,
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

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Granting the Export Centre to school roles...\n"
        ))
        self._grant_school_roles(PrebuiltRoleTemplate)

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_perms} new permission(s), {len(all_perms)} total "
            f"'{MODULE_NAME}' keys registered.\n"
        ))

    def _grant_school_roles(self, PrebuiltRoleTemplate):
        """Attach the school defaults to the library roles and grow their copies.

        Attached to the prebuilt templates, so every school provisioned from
        them has these. A default the library role gains here also reaches the
        copies every school already holds, per-branch copies included, so a
        school that onboarded earlier is not left with an Export button its
        role can never satisfy. That happens once, when the library gains the
        key (:func:`vs_rbac.library_growth.attach_defaults`), so a key a school
        took off its own role stays off.
        """
        from vs_rbac.library_growth import attach_defaults

        grown = 0
        for role_key, keys in SCHOOL_ROLE_DEFAULTS.items():
            prebuilt = PrebuiltRoleTemplate.objects.filter(key=role_key).first()
            if prebuilt is None:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠  Prebuilt role '{role_key}' not found - run "
                    f"seed_prebuilt_role_templates first. Skipping its defaults."
                ))
                continue
            attached, added = attach_defaults(
                prebuilt, keys, branch_copies=True, tenant_kind="SCHOOL",
            )
            grown += added
            self.stdout.write(
                f"  {role_key}: +{len(attached)} default(s) ({len(keys)} total)."
            )
        self.stdout.write(f"  {grown} grant(s) added to schools' copies.")
