"""The daily onboarding sweep: expire what is out of time, warn what is close.

Two steps, in this order and for a reason. First, suspend every school that has
been PENDING for 90 days. Then warn every school that reaches 76 days, so a
school past the deadline is suspended rather than sent a notice about a date it
has already missed, and so a warning that could not be delivered can never keep
a school alive.

Both steps measure from ``Tenant.pending_since`` (entry into the current
PENDING spell) and never from ``Tenant.created_at``, so a school an operator
has reinstated gets its ninety days and its warning back rather than being
expired again the next morning. Completing tasks does NOT reset the clock: that
is a product decision, not an oversight.

The suspension is written through ``School.status`` wherever a school profile
exists, because ``School.save()`` mirrors status onto the tenant and a tenant
written beside its school would be silently un-suspended by the next school
edit. A school-kind tenant with no profile at all is written on the tenant and
counted separately in the output.

Authentication already refuses a SUSPENDED tenant, so the school's sign-in dies
with the status change. Idempotent in both steps: a suspended tenant is no
longer PENDING, and a warned school carries ``expiry_warned_at`` until its
pending spell changes. ``--dry-run`` lists what would happen and writes nothing.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Suspend school tenants that have been PENDING for the onboarding "
        "expiry window, then warn those approaching it (idempotent)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would happen without writing anything.",
        )

    def handle(self, *args, **options):
        from schools.vs_onboarding.services.lifecycle import run_sweep

        dry_run = options["dry_run"]
        result = run_sweep(dry_run=dry_run)
        prefix = "  [dry-run]" if dry_run else " "

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n  Onboarding pending since before {result['cutoff']:%Y-%m-%d %H:%M} "
            f"({result['expiry_days']} days)\n"
        ))

        for row in result["expired"]:
            note = "" if row["has_school_profile"] else "  (no school profile)"
            self.stdout.write(
                f"{prefix} {row['slug']}: {row['pending_days']} days pending{note}"
            )
        if not result["expired"]:
            self.stdout.write("  No school has been onboarding that long.")

        if result["without_school_profile"]:
            self.stdout.write(self.style.WARNING(
                f"  !  {result['without_school_profile']} tenant(s) had no school "
                f"profile and were written on the tenant directly."
            ))

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n  Warnings, {result['warning_days']} days out\n"
        ))
        for row in result["warned"]:
            self.stdout.write(
                f"{prefix} {row['slug']}: {row['days_remaining']} days remaining"
            )
        if not result["warned"]:
            self.stdout.write(
                "  Nobody to warn (nobody new in the window, or all warned already)."
            )

        verb = "would suspend" if dry_run else "suspended"
        told = "would warn" if dry_run else "warned"
        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {verb} {result['expired_count']} school(s); "
            f"{told} {result['warned_count']} school(s).\n"
        ))
