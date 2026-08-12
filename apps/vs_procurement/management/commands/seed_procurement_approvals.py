"""Give tenants their own procurement approval rules (idempotent, non-destructive).

Every tenant sharing one platform-wide ladder means one tenant's settings decide how
every other tenant's spend is approved. This command is the operational half of the fix
(``vs_procurement.approvals.ensure_tenant_approval_templates`` is the service half): it
publishes a tenant-scoped ladder per approvable document type, which then wins over the
platform fallback through the workflow engine's own cascade.

Usage::

    python manage.py seed_procurement_approvals --tenant corona
    python manage.py seed_procurement_approvals --all-tenants
    python manage.py seed_procurement_approvals --platform      # the shared fallback

Two guarantees, both deliberate:

* **Never destructive.** A tenant that already has its own ladder for a document type
  is reported and skipped, so re-running after an administrator customised the
  threshold or the approving permissions cannot restore the defaults over them. Only
  ``--platform`` upserts, because that row is platform provisioning's to own.
* **Seeded blocked.** The rules arrive with nobody holding the approving permission, so
  the first document submitted parks and asks for an approver rather than approving
  itself. Grant the approving permission deliberately, per branch, afterwards.

Safe to re-run. ``--dry-run`` reports what would change and writes nothing.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vs_procurement.approvals import (
    ensure_default_approval_templates,
    ensure_tenant_approval_templates,
)
from vs_procurement.constants import (
    WF_DEFAULT_MANAGER_PERMISSION,
    WF_DEFAULT_SENIOR_PERMISSION,
    WF_DEFAULT_SENIOR_THRESHOLD,
)


class Command(BaseCommand):
    help = "Publish per-tenant (or the platform fallback) procurement approval rules."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", action="append", default=[], dest="tenants", metavar="SLUG",
            help="Tenant slug to seed. Repeat for several.",
        )
        parser.add_argument(
            "--all-tenants", action="store_true",
            help="Seed every tenant that does not already have its own rules.",
        )
        parser.add_argument(
            "--platform", action="store_true",
            help="Publish the platform-wide fallback ladder (upserts in place).",
        )
        parser.add_argument(
            "--threshold", type=int, default=WF_DEFAULT_SENIOR_THRESHOLD,
            help="Kobo at/above which the senior stage runs (new ladders only).",
        )
        parser.add_argument(
            "--manager-permission", default=WF_DEFAULT_MANAGER_PERMISSION,
            help="Permission key the first stage resolves approvers against.",
        )
        parser.add_argument(
            "--senior-permission", default=WF_DEFAULT_SENIOR_PERMISSION,
            help="Permission key the threshold-gated second stage resolves against.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would change; write nothing.",
        )

    def handle(self, *args, **options):
        from vs_tenants.models import Tenant

        slugs = options["tenants"]
        if not (slugs or options["all_tenants"] or options["platform"]):
            raise CommandError("Pass --tenant SLUG, --all-tenants, or --platform.")
        if options["threshold"] < 0:
            raise CommandError("--threshold is an amount in kobo and cannot be negative.")

        ladder_kwargs = {
            "threshold": options["threshold"],
            "manager_permission": options["manager_permission"],
            "senior_permission": options["senior_permission"],
        }

        tenants = []
        if slugs:
            tenants = list(Tenant.objects.filter(slug__in=slugs))
            missing = set(slugs) - {tenant.slug for tenant in tenants}
            if missing:
                raise CommandError(f"No tenant with slug: {', '.join(sorted(missing))}.")
        elif options["all_tenants"]:
            tenants = list(Tenant.objects.all().order_by("slug"))

        # One transaction: a partially seeded tenant would leave some document types
        # routing to its own ladder and others to the platform fallback.
        with transaction.atomic():
            if options["platform"]:
                published = ensure_default_approval_templates(**ladder_kwargs)
                self.stdout.write(
                    f"Platform fallback: {len(published)} ladder(s) published.",
                )

            for tenant in tenants:
                results = ensure_tenant_approval_templates(tenant, **ladder_kwargs)
                created = [t for t, was_created in results if was_created]
                kept = [t for t, was_created in results if not was_created]
                self.stdout.write(
                    f"{tenant.slug}: {len(created)} created, {len(kept)} left as configured.",
                )

            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run - nothing was written."))
                return

        self.stdout.write(self.style.SUCCESS(
            "Done. Approval rules are in place; nobody can approve until the approving "
            "permission is granted, so the first submission will park until it is.",
        ))
