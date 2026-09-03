"""Bring existing tenants onto the Finance Admin and Procurement Admin roles.

``seed_prebuilt_role_templates`` maintains the library. A school's roles are
independent copies of it, so renaming or populating a template changes nothing
for a school that already adopted one. This carries the change across.

Per tenant, per role:

* an old role under a former name is renamed IN PLACE, so the row keeps its id
  and every assignment on it survives. Nobody is reassigned and nobody loses
  access mid-term.
* the role is granted every permission in its module that the tenant may hold.
* with ``--create``, a tenant that has no such role gets one from the library.

Two things this deliberately does NOT do by hand.

Grants go through ``set_role_access`` rather than writing TenantRolePermission
rows directly. That service takes the lock, bumps the role's version and writes
a durable audit record inside the same transaction. Handing a role eighty-odd
restricted permissions with no record of who did it or why is precisely the
event an RBAC system exists to remember. It also REPLACES the permission set
rather than adding to it, which is why the desired set below is the union of
what the role already grants with the module's keys - passing the module alone
would silently drop everything else the role carried.

New roles come from ``create_role_from_suggestion``, so a tenant's copy is made
the same way the product makes it when a school picks the role itself.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from vs_rbac.models import (
    Permission,
    PrebuiltRoleTemplate,
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_rbac.services import create_role_from_suggestion, set_role_access
from vs_rbac.models import platform_only_keys

User = get_user_model()

# Only a school gets these roles; see the filter in handle().
SCHOOL_KIND = "SCHOOL"


ROLES = [
    {
        "prebuilt_key": "finance_admin",
        "key": "finance-admin",
        "name": "Finance Admin",
        "prefixes": ["finance."],
        # Finance Manager was this role under an older name.
        "renames_from": ["finance-manager", "finance_manager"],
    },
    {
        "prebuilt_key": "procurement_admin",
        "key": "procurement-admin",
        "name": "Procurement Admin",
        "prefixes": ["procurement."],
        # Nothing to rename: no school ever had a procurement role.
        "renames_from": [],
    },
]

# Bursar split the money work along a line no school actually staffs. A tenant
# copy with nobody on it is dead config; one with people on it is somebody's
# access, and is reported for a human rather than deleted underneath them.
RETIRED_KEYS = ["bursar"]


class Command(BaseCommand):
    help = (
        "Rename, create and populate each tenant's Finance Admin and "
        "Procurement Admin roles from the prebuilt library."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print actions without writing.")
        parser.add_argument("--tenant", help="Limit to one tenant slug.")
        parser.add_argument(
            "--create",
            action="store_true",
            help="Also create the role for a tenant that does not have it yet.",
        )
        parser.add_argument(
            "--actor",
            help="Email of the user to record as having made the change. Recommended.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        only = options.get("tenant")
        create_missing = options["create"]

        actor = None
        if options.get("actor"):
            actor = User.objects.filter(email=options["actor"]).first()
            if actor is None:
                raise CommandError(f"No user with email {options['actor']}.")

        # Schools only. Finance Admin and Procurement Admin are school roles, and
        # the platform tenant (CodeX itself) is not a school - creating one there
        # would hand a fleet-wide account a school's money and buying rights.
        tenants = TenantRoleTemplate.objects.filter(
            tenant__kind=SCHOOL_KIND,
        ).values_list("tenant__slug", flat=True)
        slugs = sorted({slug for slug in tenants if not only or slug == only})
        if not slugs:
            self.stdout.write(self.style.WARNING("No school tenants matched."))
            return

        for spec in ROLES:
            keys = [k for k in spec["prefixes"]]
            wanted = self._module_keys(spec["prefixes"])
            self.stdout.write(
                f"\n{spec['name']}: {len(wanted)} grantable key(s) under {', '.join(keys)}"
            )
            for slug in slugs:
                self._apply(spec, slug, wanted, actor, create_missing, dry_run)

        self._report_retired(only, dry_run)

    def _module_keys(self, prefixes):
        """Every key under these prefixes that a school may actually hold.

        Platform-scoped keys are dropped here rather than left to fail one by
        one: they belong to no tenant, and two finance keys
        (``finance.currency.create``, ``finance.fxrate.create``) are in that
        position permanently.
        """
        keys = []
        for prefix in prefixes:
            keys.extend(
                Permission.objects.filter(key__startswith=prefix)
                .order_by("key")
                .values_list("key", flat=True)
            )
        platform = platform_only_keys(keys)
        if platform:
            self.stdout.write(self.style.WARNING(
                f"  not grantable inside a tenant: {', '.join(sorted(platform))}"
            ))
        return [key for key in keys if key not in platform]

    def _apply(self, spec, slug, wanted, actor, create_missing, dry_run):
        role = self._find(spec, slug)

        if role is None:
            if not create_missing:
                self.stdout.write(f"  [{slug}] no {spec['name']} role (pass --create to add one)")
                return
            if not PrebuiltRoleTemplate.objects.filter(key=spec["prebuilt_key"], is_active=True).exists():
                self.stdout.write(self.style.WARNING(
                    f"  [{slug}] cannot create: no active {spec['prebuilt_key']} in the library"
                ))
                return
            if dry_run:
                self.stdout.write(f"  [{slug}] would create {spec['name']} from the library")
                return
            tenant = (
                TenantRoleTemplate.objects
                .filter(tenant__slug=slug, tenant__kind=SCHOOL_KIND)
                .first()
                .tenant
            )
            role = create_role_from_suggestion(spec["prebuilt_key"], tenant, actor)
            self.stdout.write(self.style.SUCCESS(
                f"  [{slug}] created {role.name} with "
                f"{TenantRolePermission.objects.filter(role=role, granted=True).count()} grant(s)"
            ))
            return

        assignments = TenantUserRoleAssignment.objects.filter(role=role).count()

        if role.key != spec["key"]:
            if dry_run:
                self.stdout.write(
                    f"  [{slug}] would rename {role.key} -> {spec['key']} "
                    f"({assignments} assignment(s) carried over)"
                )
            else:
                role.key = spec["key"]
                role.name = spec["name"]
                role.save(update_fields=["key", "name"])
                self.stdout.write(self.style.SUCCESS(
                    f"  [{slug}] renamed to {spec['name']} "
                    f"({assignments} assignment(s) carried over)"
                ))

        held = set(
            TenantRolePermission.objects
            .filter(role=role, granted=True)
            .values_list("permission_id", flat=True)
        )
        missing = [key for key in wanted if key not in held]
        if not missing:
            self.stdout.write(f"  [{slug}] already holds all {len(wanted)}")
            return

        if dry_run:
            self.stdout.write(f"  [{slug}] would grant {len(missing)} more (has {len(held)})")
            return

        # A union, because set_role_access REPLACES the set. Passing the module
        # alone would take away everything else this role carries.
        set_role_access(
            role=role,
            actor=actor,
            reason=(
                f"Adopt {spec['name']}: grant every "
                f"{', '.join(spec['prefixes'])} permission the tenant may hold."
            ),
            permission_keys=sorted(held | set(wanted)),
            allow_restricted=True,
            source="role_suggestion",
        )
        self.stdout.write(self.style.SUCCESS(
            f"  [{slug}] granted {len(missing)} more, {len(held | set(wanted))} total"
        ))

    def _find(self, spec, slug):
        return TenantRoleTemplate.objects.filter(
            tenant__slug=slug,
            key__in=[spec["key"], *spec["renames_from"]],
        ).select_related("tenant").first()

    def _report_retired(self, only, dry_run):
        roles = TenantRoleTemplate.objects.filter(key__in=RETIRED_KEYS)
        if only:
            roles = roles.filter(tenant__slug=only)

        for role in roles.select_related("tenant"):
            slug = role.tenant.slug
            assignments = TenantUserRoleAssignment.objects.filter(role=role).count()
            if assignments:
                self.stdout.write(self.style.WARNING(
                    f"\n[{slug}] {role.name} still has {assignments} assignment(s) and was LEFT ALONE. "
                    f"Move those people to Finance Admin before retiring it."
                ))
                continue
            if dry_run:
                self.stdout.write(f"\n[{slug}] would delete unused role {role.name}")
                continue
            role.delete()
            self.stdout.write(self.style.SUCCESS(f"\n[{slug}] deleted unused role {role.name}"))
