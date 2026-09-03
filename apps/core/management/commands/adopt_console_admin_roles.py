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
    PrebuiltRoleTemplate,
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_rbac.services import create_role_from_suggestion, set_role_access

User = get_user_model()

# Only a school gets these roles; see the filter in handle().
SCHOOL_KIND = "SCHOOL"


# What each role should carry is NOT restated here. It is read from the
# library template's own defaults, because the library is where that decision
# lives and a second copy of it drifts: adding `payments.` to Finance Admin in
# the seeder left a list here still saying `finance.` only, and the tenants
# quietly kept the old set.
ROLES = [
    {
        "prebuilt_key": "finance_admin",
        "key": "finance-admin",
        "name": "Finance Admin",
        # Finance Manager was this role under an older name.
        "renames_from": ["finance-manager", "finance_manager"],
    },
    {
        "prebuilt_key": "procurement_admin",
        "key": "procurement-admin",
        "name": "Procurement Admin",
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
            wanted = self._template_keys(spec["prebuilt_key"])
            if wanted is None:
                self.stdout.write(self.style.WARNING(
                    f"\n{spec['name']}: no active {spec['prebuilt_key']} in the library, skipped. "
                    f"Run seed_prebuilt_role_templates first."
                ))
                continue
            self.stdout.write(
                f"\n{spec['name']}: {len(wanted)} key(s) on the library template"
            )
            for slug in slugs:
                self._apply(spec, slug, wanted, actor, create_missing, dry_run)

        self._report_retired(only, dry_run)

    def _template_keys(self, prebuilt_key):
        """What the library says this role carries.

        Reading the template's defaults rather than re-deriving from prefixes
        makes the library the single answer to "what is in this role", and means
        a key added there by any route - a prefix sweep, or somebody attaching
        one by hand - reaches the tenants on the next run.

        Platform-scoped keys cannot be template defaults in the first place, so
        nothing needs filtering here; ``PrebuiltRolePermission`` already refused
        them when the library was seeded.
        """
        template = PrebuiltRoleTemplate.objects.filter(
            key=prebuilt_key, is_active=True,
        ).first()
        if template is None:
            return None
        return list(
            template.default_permissions
            .order_by("permission_id")
            .values_list("permission_id", flat=True)
        )

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
            reason=f"Adopt {spec['name']}: match the library template's defaults.",
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
