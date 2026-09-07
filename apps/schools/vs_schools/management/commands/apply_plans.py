"""Bring existing schools onto the plan they already pay for.

A school created before the plan decided anything has grants that carry no
depth and cover only whatever the onboarding wizard happened to tick. Neither
is wrong, exactly: no depth means the school reaches every band, which is what
it had. But it means the plan on the row and the product the school gets are
still two unrelated facts, and the gate has nothing to read.

This makes them one. For every school with a package setup, it grants every
module at the depth that school's plan reaches, and writes the subscription
expiry onto each grant.

Two things it deliberately does not touch:

* schools with no package setup, which have no plan to apply and would be
  given one by guesswork;
* :class:`vs_config.models.CapabilityDepthGrant`, where a deal lives. An
  uplift someone was given survives being moved onto the tier they pay for.

Safe to re-run: it writes the same rows every time, through the service that
audits them.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from schools.vs_schools.models import SchoolPackageSetup
from schools.vs_schools.services.packages import apply_plan_entitlements


class Command(BaseCommand):
    help = (
        "Grant every school its plan's modules at its plan's depth, and write "
        "the subscription expiry onto each grant. Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--slug",
            default="",
            help="Apply to one school only, by slug. Default is every school with a package.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Report what would be written without writing it.",
        )

    def handle(self, *args, **options):
        setups = (
            SchoolPackageSetup.objects.select_related("school", "package_plan")
            .order_by("school__slug")
        )
        if options["slug"]:
            setups = setups.filter(school__slug=options["slug"])

        self.stdout.write(self.style.MIGRATE_HEADING("Applying package plans..."))
        applied = 0
        for setup in setups:
            depth = setup.package_plan.get_default_depth_display() or "unlimited"
            line = (
                f"  {setup.school.slug:28s} → {setup.package_plan.name:12s} "
                f"depth={depth:10s} expires={setup.subscription_expires_at}"
            )
            if options["dry_run"]:
                self.stdout.write(self.style.WARNING(f"  [DRY RUN]{line}"))
                continue
            with transaction.atomic():
                rows = apply_plan_entitlements(
                    school=setup.school,
                    plan=setup.package_plan,
                    expires_at=setup.subscription_expires_at,
                    actor=None,
                    reason="Applying the school's existing package plan.",
                )
            applied += 1
            self.stdout.write(self.style.SUCCESS(f"  [{len(rows):2d} rows]{line}"))

        self.stdout.write("")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                f"Dry run. {setups.count()} school(s) would be applied."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(f"Done. {applied} school(s) applied."))
