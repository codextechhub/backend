"""Bring existing tenants onto the Finance Admin role.

The library is seeded by ``seed_prebuilt_role_templates``, but a school's roles
are independent copies: renaming a template changes nothing for a school that
already adopted it. This carries the same change across to the tenants.

What it does, per tenant that already holds a Finance Manager role:

* renames it to Finance Admin, in place. The role keeps its id, so every
  assignment on it survives - nobody is logged out of anything and nobody has to
  be re-assigned.
* grants every finance permission the tenant may hold, as DIRECT grants.

Direct grants are the only route available, and that is the whole reason this
command exists rather than a group. ``GroupPermission`` refuses a restricted
permission outright, and 87 of the 123 finance keys are restricted - which is
why a Finance Manager built out of groups could read all of finance and create
almost none of it. ``TenantRolePermission`` checks tenant scope and nothing
else, which is the reviewed path the group error message points at.

Two finance keys are platform-scoped and belong to no school
(``finance.currency.create``, ``finance.fxrate.create``). They are reported
rather than skipped quietly, so "all of finance" being two short is never a
mystery.
"""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction

from vs_rbac.models import (
    Permission,
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)

FINANCE_PREFIX = "finance."

# Tenant role keys this command recognises as "the finance role", old and new.
OLD_KEYS = ["finance-manager", "finance_manager"]
NEW_KEY = "finance-admin"
NEW_NAME = "Finance Admin"

# Bursar split the money work in two along a line no school actually staffs. A
# tenant copy with nobody assigned is dead config and goes; one with people on
# it is somebody's access and is reported for a human to decide.
RETIRED_KEYS = ["bursar"]


class Command(BaseCommand):
    help = (
        "Rename each tenant's Finance Manager role to Finance Admin and grant it "
        "every finance permission that tenant may hold."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print actions without writing.")
        parser.add_argument("--tenant", help="Limit to one tenant slug.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        only = options.get("tenant")

        roles = TenantRoleTemplate.objects.filter(key__in=OLD_KEYS + [NEW_KEY])
        if only:
            roles = roles.filter(tenant__slug=only)
        roles = roles.select_related("tenant").order_by("tenant__slug")

        if not roles.exists():
            self.stdout.write(self.style.WARNING("No tenant holds a finance role to convert."))

        finance = list(Permission.objects.filter(key__startswith=FINANCE_PREFIX).order_by("key"))
        self.stdout.write(f"{len(finance)} finance permissions in the registry.\n")

        for role in roles:
            self._convert(role, finance, dry_run)

        self._report_retired(only, dry_run)

    def _convert(self, role, finance, dry_run):
        slug = role.tenant.slug
        assignments = TenantUserRoleAssignment.objects.filter(role=role).count()

        if role.key != NEW_KEY:
            if dry_run:
                self.stdout.write(
                    f"[{slug}] would rename {role.key} -> {NEW_KEY} "
                    f"({assignments} assignment(s) carried over)"
                )
            else:
                role.key = NEW_KEY
                role.name = NEW_NAME
                role.save(update_fields=["key", "name"])
                self.stdout.write(self.style.SUCCESS(
                    f"[{slug}] renamed to {NEW_NAME} ({assignments} assignment(s) carried over)"
                ))

        held = set(
            TenantRolePermission.objects
            .filter(role=role, granted=True)
            .values_list("permission_id", flat=True)
        )
        missing = [p for p in finance if p.key not in held]

        if dry_run:
            self.stdout.write(
                f"[{slug}] would grant {len(missing)} finance permissions "
                f"({len(held & {p.key for p in finance})} already held)"
            )
            return

        granted = 0
        refused = []
        for permission in missing:
            try:
                with transaction.atomic():
                    TenantRolePermission.objects.update_or_create(
                        role=role,
                        permission=permission,
                        defaults={"granted": True},
                    )
                granted += 1
            except ValidationError as error:
                refused.append(f"{permission.key} ({error.messages[0]})")

        self.stdout.write(self.style.SUCCESS(
            f"[{slug}] granted {granted} finance permissions, {len(refused)} refused"
        ))
        for line in refused:
            self.stdout.write(self.style.WARNING(f"    refused: {line}"))

    def _report_retired(self, only, dry_run):
        roles = TenantRoleTemplate.objects.filter(key__in=RETIRED_KEYS)
        if only:
            roles = roles.filter(tenant__slug=only)

        for role in roles.select_related("tenant"):
            slug = role.tenant.slug
            assignments = TenantUserRoleAssignment.objects.filter(role=role).count()
            if assignments:
                self.stdout.write(self.style.WARNING(
                    f"[{slug}] {role.name} still has {assignments} assignment(s) and was LEFT ALONE. "
                    f"Move those people to {NEW_NAME} before retiring it."
                ))
                continue
            if dry_run:
                self.stdout.write(f"[{slug}] would delete unused role {role.name}")
                continue
            role.delete()
            self.stdout.write(self.style.SUCCESS(f"[{slug}] deleted unused role {role.name}"))
