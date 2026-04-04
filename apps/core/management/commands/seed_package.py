from django.core.management.base import BaseCommand
from django.db import transaction

from vs_institutions.models import PackagePlan, BillingCycle


# ---------------------------------------------------------------------------
# Seed data
# Each entry maps to one PackagePlan row.
#
# Capacity limits (max_*):
#   None  → unlimited (no ceiling enforced)
#   int   → hard cap validated in InstitutionPackageSetup.clean()
#
# `code` is the slug used in API payloads (e.g. package_plan="standard")
# ---------------------------------------------------------------------------

PLANS = [
    {
        "name": "Basic",
        "code": "basic",
        "description": (
            "Entry-level plan suited for small single-branch institutions. "
            "Covers core student and teacher management with limited capacity."
        ),
        "billing_cycle": BillingCycle.YEARLY,
        "max_students": 200,
        "max_teachers": 20,
        "max_admins": 3,
        "max_branch": 1,
        "is_active": True,
    },
    {
        "name": "Standard",
        "code": "standard",
        "description": (
            "Mid-tier plan for growing institutions with multiple branches. "
            "Includes expanded capacity and access to additional modules."
        ),
        "billing_cycle": BillingCycle.YEARLY,
        "max_students": 800,
        "max_teachers": 60,
        "max_admins": 10,
        "max_branch": 5,
        "is_active": True,
    },
    {
        "name": "Premium",
        "code": "premium",
        "description": (
            "Full-featured plan for established institutions that need "
            "high capacity, all modules, and priority support."
        ),
        "billing_cycle": BillingCycle.YEARLY,
        "max_students": 3000,
        "max_teachers": 200,
        "max_admins": 30,
        "max_branch": 20,
        "is_active": True,
    },
    {
        "name": "Enterprise",
        "code": "enterprise",
        "description": (
            "Unlimited plan for large school networks and multi-campus "
            "institutions. No capacity ceilings. Custom SLA and support."
        ),
        "billing_cycle": BillingCycle.YEARLY,
        "max_students": None,   # unlimited
        "max_teachers": None,   # unlimited
        "max_admins": None,     # unlimited
        "max_branch": None,     # unlimited
        "is_active": True,
    },
]


class Command(BaseCommand):
    help = (
        "Seeds the PackagePlan table with the platform's four subscription tiers: "
        "Basic, Standard, Premium, Enterprise. "
        "Safe to run multiple times — uses update_or_create on `code`."
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
                    "max_students": plan_data["max_students"],
                    "max_teachers": plan_data["max_teachers"],
                    "max_admins": plan_data["max_admins"],
                    "max_branch": plan_data["max_branch"],
                    "is_active": plan_data["is_active"],
                },
            )

            # Build capacity display string
            def cap(val):
                return str(val) if val is not None else "unlimited"

            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [CREATED]  {code:12s} → {obj.name:12s} | "
                        f"students={cap(obj.max_students):10s} "
                        f"teachers={cap(obj.max_teachers):10s} "
                        f"admins={cap(obj.max_admins):10s} "
                        f"branches={cap(obj.max_branch)}"
                    )
                )
            else:
                updated_count += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"  [UPDATED]  {code:12s} → {obj.name:12s} | "
                        f"students={cap(obj.max_students):10s} "
                        f"teachers={cap(obj.max_teachers):10s} "
                        f"admins={cap(obj.max_admins):10s} "
                        f"branches={cap(obj.max_branch)}"
                    )
                )

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