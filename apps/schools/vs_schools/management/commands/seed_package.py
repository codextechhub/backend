"""Seed the subscription package catalogue.

Lives in the school package rather than in ``core`` because that is what it
seeds: ``PackagePlan`` and ``BillingCycle`` are school models, and a command in
a domain-neutral app importing them was one of three places the school app
leaked into the engines. Django discovers management commands per app, so the
command name is unchanged: ``manage.py seed_package`` still resolves, and every
runbook, ``reset_db --post-commands`` list and deploy script that names it
keeps working.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from schools.vs_schools.models import CapabilityDepth, PackagePlan, BillingCycle


# ---------------------------------------------------------------------------
# Seed data
# Each entry maps to one PackagePlan row.
#
# `default_depth` is how far into every module the tier reaches:
#   CORE     → the whole product, at its shallow end
#   PLUS     → adds bulk work and multi-step approvals
#   ADVANCED → adds payroll, analytics and the reporting tail
#   None     → not depth-limited, which is what Enterprise means
#
# No tier caps a school's size. Size is priced, not fenced.
#
# `code` is the slug used in API payloads (e.g. package_plan="standard")
# ---------------------------------------------------------------------------

PLANS = [
    {
        "name": "Basic",
        "code": "basic",
        "description": (
            "Entry-level plan suited for small single-branch schools. "
            "Covers core student and teacher management with limited capacity."
        ),
        "billing_cycle": BillingCycle.YEARLY,
        "default_depth": CapabilityDepth.CORE,
        "is_active": True,
    },
    {
        "name": "Standard",
        "code": "standard",
        "description": (
            "Mid-tier plan for growing schools with multiple branches. "
            "Includes expanded capacity and access to additional modules."
        ),
        "billing_cycle": BillingCycle.YEARLY,
        "default_depth": CapabilityDepth.PLUS,
        "is_active": True,
    },
    {
        "name": "Premium",
        "code": "premium",
        "description": (
            "Full-featured plan for established schools that need "
            "high capacity, all modules, and priority support."
        ),
        "billing_cycle": BillingCycle.YEARLY,
        "default_depth": CapabilityDepth.ADVANCED,
        "is_active": True,
    },
    {
        "name": "Enterprise",
        "code": "enterprise",
        "description": (
            "Unlimited plan for large school networks and multi-branch "
            "schools. No capacity ceilings. Custom SLA and support."
        ),
        "billing_cycle": BillingCycle.YEARLY,
        "default_depth": None,
        "is_active": True,
    },
]


class Command(BaseCommand):
    help = (
        "Seeds the PackagePlan table with the platform's four subscription tiers: "
        "Basic, Standard, Premium, Enterprise. "
        "Safe to run multiple times - uses update_or_create on `code`."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--deactivate-unlisted",
            action="store_true",
            default=False,
            help=(
                "If passed, any existing PackagePlan rows whose code is NOT "
                "in this seed list will be marked is_active=False. "
                "Use with caution on production."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("Seeding package plans..."))

        created_count = 0
        updated_count = 0
        seeded_codes = []

        for plan_data in PLANS:
            code = plan_data["code"]
            seeded_codes.append(code)

            obj, created = PackagePlan.objects.update_or_create(
                code=code,
                defaults={
                    "name": plan_data["name"],
                    "description": plan_data["description"],
                    "billing_cycle": plan_data["billing_cycle"],
                    "default_depth": plan_data["default_depth"],
                    "is_active": plan_data["is_active"],
                },
            )

            depth = obj.get_default_depth_display() or "unlimited"

            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  [CREATED]  {code:12s} → {obj.name:12s} | depth={depth}"
                ))
            else:
                updated_count += 1
                self.stdout.write(self.style.WARNING(
                    f"  [UPDATED]  {code:12s} → {obj.name:12s} | depth={depth}"
                ))

        # Optional: deactivate plans not in seed list
        if options["deactivate_unlisted"]:
            deactivated = PackagePlan.objects.exclude(code__in=seeded_codes).update(
                is_active=False
            )
            if deactivated:
                self.stdout.write(
                    self.style.ERROR(
                        f"\n  [DEACTIVATED] {deactivated} plan(s) not in seed list."
                    )
                )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {created_count} created, {updated_count} updated."
            )
        )