from django.core.management.base import BaseCommand
from django.db import transaction

from vs_institutions.models import XVSModules


# ---------------------------------------------------------------------------
# Seed data
# Each entry maps to one XVSModules row.
# `key`  → machine identifier used in API payloads and RBAC checks (slug)
# `name` → human-readable label shown in the UI
# `description` → tooltip / help text shown in Package Setup
# ---------------------------------------------------------------------------

MODULES = [
    {
        "key": "students",
        "name": "Students Management",
        "description": (
            "Manage student records, enrollment, class assignments, "
            "promotions, and student profiles across all branches."
        ),
        "is_active": True,
    },
    {
        "key": "teachers",
        "name": "Teachers Management",
        "description": (
            "Manage teacher profiles, subject assignments, timetables, "
            "and staff records for teaching personnel."
        ),
        "is_active": True,
    },
    {
        "key": "parents",
        "name": "Parents Management",
        "description": (
            "Manage parent and guardian profiles, link them to student "
            "records, and enable the Parent Portal access."
        ),
        "is_active": True,
    },
    {
        "key": "attendance",
        "name": "Attendance Tracking",
        "description": (
            "Track and report daily student and staff attendance across "
            "branches, with absence notifications and analytics."
        ),
        "is_active": True,
    },
    {
        "key": "finance",
        "name": "Finance",
        "description": (
            "Manage school fees, invoices, payment collection, receipts, "
            "and financial reporting for institutions."
        ),
        "is_active": True,
    },
    {
        "key": "procurement",
        "name": "Procurement",
        "description": (
            "Handle purchase requests, vendor orders, and supply chain "
            "workflows for institution procurement teams."
        ),
        "is_active": True,
    },
    {
        "key": "vendors",
        "name": "Vendors Management",
        "description": (
            "Maintain a registry of approved vendors, track contracts, "
            "and manage vendor relationships across the institution."
        ),
        "is_active": True,
    },
]


class Command(BaseCommand):
    help = (
        "Seeds the XVSModules table with the platform's default module catalog. "
        "Safe to run multiple times — uses update_or_create on `key`."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--deactivate-unlisted",
            action="store_true",
            default=False,
            help=(
                "If passed, any existing XVSModules rows whose key is NOT "
                "in this seed list will be marked is_active=False. "
                "Use with caution on production."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("Seeding XVS platform modules..."))

        created_count = 0
        updated_count = 0
        seeded_keys = []

        for module_data in MODULES:
            key = module_data["key"]
            seeded_keys.append(key)

            obj, created = XVSModules.objects.update_or_create(
                key=key,
                defaults={
                    "name": module_data["name"],
                    "description": module_data["description"],
                    "is_active": module_data["is_active"],
                },
            )

            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  [CREATED]  {key:20s} → {obj.name}")
                )
            else:
                updated_count += 1
                self.stdout.write(
                    self.style.WARNING(f"  [UPDATED]  {key:20s} → {obj.name}")
                )

        # Optional: deactivate modules not in seed list
        if options["deactivate_unlisted"]:
            deactivated = XVSModules.objects.exclude(key__in=seeded_keys).update(
                is_active=False
            )
            if deactivated:
                self.stdout.write(
                    self.style.ERROR(
                        f"\n  [DEACTIVATED] {deactivated} module(s) not in seed list."
                    )
                )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {created_count} created, {updated_count} updated."
            )
        )