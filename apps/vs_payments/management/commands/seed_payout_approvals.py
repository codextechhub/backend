"""Provision the mandatory maker-checker ladder for payouts.

A payout batch pays one or more beneficiaries and is a high-risk cash-out path. Every
payout must enter the workflow before any provider call. This command publishes the
default checker and high-value senior stages; a missing template fails closed rather
than restoring a direct cash-out path.

Usage::

    python manage.py seed_payout_approvals --platform      # the shared fallback
    python manage.py seed_payout_approvals --tenant corona
    python manage.py seed_payout_approvals --all-tenants

Two guarantees, both deliberate and matching ``seed_procurement_approvals``:

* **Never destructive.** A tenant that already has its own ladder is reported and
  skipped, so re-running after an administrator customised the approving role or
  added stages of their own cannot restore the defaults over them. Only ``--platform``
  upserts, because that row is platform provisioning's to own.
* **Seeded blocked.** The rules arrive with nobody holding either approving role, so
  each applicable stage parks and asks for an approver rather than paying itself out.
  Appoint holders deliberately afterwards.

Safe to re-run. ``--dry-run`` reports what would change and writes nothing.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vs_payments.approvals import (
    ensure_default_approval_templates,
    ensure_tenant_approval_templates,
)
from vs_payments.constants import (
    WF_DEFAULT_APPROVE_ROLE,
    WF_DEFAULT_HIGH_VALUE_ROLE,
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
            help="Publish the platform-wide fallback rule (upserts in place).",
        )
        parser.add_argument(
            "--approve-role", default=WF_DEFAULT_APPROVE_ROLE,
            help="Role key the standard checker stage resolves against.",
        )
        parser.add_argument(
            "--high-value-role", default=WF_DEFAULT_HIGH_VALUE_ROLE,
            help="Role key the high-value senior stage resolves against.",
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
            "approve_role_key": options["approve_role"],
            "high_value_role_key": options["high_value_role"],
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
                ensure_default_approval_templates(**ladder_kwargs)
                self.stdout.write("Platform fallback: payout-batch approval published.")

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
            "holds the required payout approval roles, so each unstaffed stage will "
            "park until an administrator appoints one.",
        ))
