"""Turn on the maker-checker gate over bulk payouts (idempotent, non-destructive).

A payout batch pays many beneficiaries at once and is the highest-risk cash-out path in
the product. The approval handler for it already exists, but the gate is opt-in by
template, so an install with no template does not have a locked door on that path, it
has an open one. This command is the operational half of the fix
(:mod:`vs_payments.approvals` is the service half): it publishes the single approving
stage that makes approval actually required.

Usage::

    python manage.py seed_payout_approvals --platform      # the shared fallback
    python manage.py seed_payout_approvals --tenant corona
    python manage.py seed_payout_approvals --all-tenants

Two guarantees, both deliberate and matching ``seed_procurement_approvals``:

* **Never destructive.** A tenant that already has its own ladder is reported and
  skipped, so re-running after an administrator customised the approving role or
  added stages of their own cannot restore the defaults over them. Only ``--platform``
  upserts, because that row is platform provisioning's to own.
* **Seeded blocked.** The rules arrive with nobody holding the approving role, so
  the first batch submitted parks and asks for an approver rather than paying itself
  out. Appoint somebody to the ``payout-approver`` role deliberately afterwards.

Safe to re-run. ``--dry-run`` reports what would change and writes nothing.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vs_payments.approvals import (
    ensure_default_approval_templates,
    ensure_tenant_approval_templates,
)
from vs_payments.constants import WF_DEFAULT_APPROVE_ROLE


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
            help="Role key the approving stage resolves approvers against.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would change; write nothing.",
        )

    def handle(self, *args, **options):
        from vs_tenants.models import Tenant

        slugs = options["tenants"]
        if not (slugs or options["all_tenants"] or options["platform"]):
            raise CommandError("Pass --tenant SLUG, --all-tenants, or --platform.")
        ladder_kwargs = {"approve_role_key": options["approve_role"]}

        tenants = []
        if slugs:
            tenants = list(Tenant.objects.filter(slug__in=slugs))
            missing = set(slugs) - {tenant.slug for tenant in tenants}
            if missing:
                raise CommandError(f"No tenant with slug: {', '.join(sorted(missing))}.")
        elif options["all_tenants"]:
            tenants = list(Tenant.objects.all().order_by("slug"))

        # One transaction: a half-seeded run would leave some tenants gated and others
        # paying out unreviewed, which is the state this command exists to end.
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
            "holds the payout-approver role, so the first batch will park "
            "until one does.",
        ))
