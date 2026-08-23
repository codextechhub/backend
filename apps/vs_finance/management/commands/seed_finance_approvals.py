"""Publish the adjustment-approval ladders for tenants that already exist.

New tenants get these when their books are created: finance registers a provisioner
with the entity-creation path, so the ladders arrive with the chart of accounts. This
command is for the tenants created before that existed.

Usage::

    python manage.py seed_finance_approvals --tenant corona
    python manage.py seed_finance_approvals --all-tenants
    python manage.py seed_finance_approvals --all-tenants --threshold 0

Two guarantees, matching the procurement and payout seeds:

* **Never destructive.** A document type that already has a tenant-scoped ladder is
  reported and skipped, so re-running after an administrator customised a threshold or
  a stage cannot restore the defaults over them.
* **Seeded blocked.** The approving roles arrive with nobody in them, so the first
  refund, write-off, or above-threshold concession parks and names the role to fill
  rather than posting itself.

Safe to re-run. ``--dry-run`` reports what would change and writes nothing.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vs_finance.approvals import ensure_tenant_approval_templates
from vs_finance.constants import (
    WF_ADJUSTMENT_APPROVER_ROLE,
    WF_ADJUSTMENT_THRESHOLD,
    WF_SENIOR_ADJUSTMENT_APPROVER_ROLE,
)


class Command(BaseCommand):
    help = "Publish per-tenant refund, write-off, concession and credit-note ladders."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", action="append", default=[], dest="tenants", metavar="SLUG",
            help="Tenant slug to seed. Repeat for several.",
        )
        parser.add_argument(
            "--all-tenants", action="store_true",
            help="Seed every tenant that does not already have its own ladders.",
        )
        parser.add_argument(
            "--threshold", type=int, default=WF_ADJUSTMENT_THRESHOLD,
            help="Kobo at/above which a concession or credit note needs a second "
                 "approver (new ladders only). Pass 0 to approve every one.",
        )
        parser.add_argument(
            "--approver-role", default=WF_ADJUSTMENT_APPROVER_ROLE,
            help="Role key the first stage resolves approvers against.",
        )
        parser.add_argument(
            "--senior-role", default=WF_SENIOR_ADJUSTMENT_APPROVER_ROLE,
            help="Role key the threshold-gated stage resolves against.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change; write nothing.",
        )

    def handle(self, *args, **options):
        from vs_tenants.models import Tenant

        slugs = options["tenants"]
        if not (slugs or options["all_tenants"]):
            raise CommandError("Pass --tenant SLUG or --all-tenants.")
        if options["threshold"] < 0:
            raise CommandError("--threshold is an amount in kobo and cannot be negative.")

        if slugs:
            tenants = list(Tenant.objects.filter(slug__in=slugs))
            missing = set(slugs) - {t.slug for t in tenants}
            if missing:
                raise CommandError(f"No tenant with slug: {', '.join(sorted(missing))}.")
        else:
            tenants = list(Tenant.objects.all().order_by("slug"))

        ladder_kwargs = {
            "threshold": options["threshold"],
            "approver_role_key": options["approver_role"],
            "senior_role_key": options["senior_role"],
        }

        # One transaction: a half-seeded tenant would gate some adjustments and leave
        # others posting directly, which is harder to reason about than neither.
        with transaction.atomic():
            for tenant in tenants:
                results = ensure_tenant_approval_templates(tenant, **ladder_kwargs)
                created = sum(1 for _t, was_created in results if was_created)
                kept = len(results) - created
                self.stdout.write(
                    f"{tenant.slug}: {created} created, {kept} left as configured.",
                )

            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run - nothing was written."))
                return

        self.stdout.write(self.style.SUCCESS(
            "Done. Refunds and write-offs now need approval, and concessions and "
            "credit notes need it at or above the threshold. Nobody can approve until "
            "somebody holds the approving role, so the first one will park until they "
            "do.",
        ))
