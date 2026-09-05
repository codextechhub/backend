"""Provision the mandatory maker-checker ladder for payouts.

A payout batch pays one or more beneficiaries and is a high-risk cash-out path. Every
payout must enter the workflow before any provider call. This command publishes the
default checker and high-value senior stages for a tenant; a missing template fails
closed rather than restoring a direct cash-out path.

Usage::

    python manage.py seed_payout_approvals --platform      # the shared fallback
    python manage.py seed_payout_approvals --tenant corona
    python manage.py seed_payout_approvals --all-tenants

Two guarantees, both deliberate and matching ``seed_procurement_approvals``:

* **Never destructive.** A tenant that already has its own ladder is reported and
  skipped, so re-running after an administrator repointed a stage or added stages of
  their own cannot restore the defaults over them. Only ``--platform`` upserts,
  because that row is platform provisioning's to own.
* **Seeded blocked.** The stages arrive naming approver groups that are empty, so
  each applicable stage parks and asks for an approver rather than paying itself out.
  Fill the groups deliberately afterwards.

``--platform`` publishes a route with no stages at all, and takes none of the approver
options below: a shared row cannot name a tenant's approver group, so it carries the
document type only. A batch that resolves to it is refused as unconfigured rather than
approved.

Safe to re-run. ``--dry-run`` reports what would change and writes nothing.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vs_payments.approvals import (
    ensure_default_approval_templates,
    ensure_tenant_approval_templates,
)
from vs_payments.constants import (
    WF_DEFAULT_APPROVE_GROUP,
    WF_DEFAULT_HIGH_VALUE_GROUP,
    WF_DEFAULT_HIGH_VALUE_THRESHOLD,
)


class Command(BaseCommand):
    help = "Publish per-tenant (or the platform fallback) payout-batch approval rules."

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
            help="Publish the platform-wide route, which carries no stages "
                 "(upserts in place). Ignores the approver options below.",
        )
        parser.add_argument(
            "--approve-group", default=WF_DEFAULT_APPROVE_GROUP, dest="approve_group",
            help="Approver group code the standard checker stage resolves against.",
        )
        parser.add_argument(
            "--high-value-group", default=WF_DEFAULT_HIGH_VALUE_GROUP,
            dest="high_value_group",
            help="Approver group code the high-value senior stage resolves against.",
        )
        parser.add_argument(
            "--high-value-threshold", type=int, default=WF_DEFAULT_HIGH_VALUE_THRESHOLD,
            help="Batch total in kobo that activates senior approval.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would change; write nothing.",
        )

    def handle(self, *args, **options):
        from vs_tenants.models import Tenant

        slugs = options["tenants"]
        if not (slugs or options["all_tenants"] or options["platform"]):
            raise CommandError("Pass --tenant SLUG, --all-tenants, or --platform.")
        ladder_kwargs = {
            "approve_group_code": options["approve_group"],
            "high_value_group_code": options["high_value_group"],
            "high_value_threshold": options["high_value_threshold"],
        }

        tenants = []
        if slugs:
            tenants = list(Tenant.objects.filter(slug__in=slugs))
            missing = set(slugs) - {tenant.slug for tenant in tenants}
            if missing:
                raise CommandError(f"No tenant with slug: {', '.join(sorted(missing))}.")
        elif options["all_tenants"]:
            tenants = list(Tenant.objects.all().order_by("slug"))

        # One transaction prevents a partially provisioned approval policy.
        with transaction.atomic():
            if options["platform"]:
                ensure_default_approval_templates()
                self.stdout.write(
                    "Platform route: payout-batch published with no stages. A tenant "
                    "resolving here is asked to configure its ladder or confirm.",
                )

            for tenant in tenants:
                _template, created = ensure_tenant_approval_templates(tenant, **ladder_kwargs)
                state = "created" if created else "left as configured"
                self.stdout.write(f"{tenant.slug}: {state}.")

            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run - nothing was written."))
                return

        self.stdout.write(self.style.SUCCESS(
            "Done. Payouts now need approval; nobody can approve until somebody "
            "is put in the payout approver groups, so each unstaffed stage will "
            "park until an administrator fills them.",
        ))
