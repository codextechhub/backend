from django.core.management.base import BaseCommand

from vs_rbac.models import PrebuiltRolePermission, PrebuiltRoleTemplate


PREBUILT_ROLES = [
    {
        "key": "school_admin",
        "name": "School Admin",
        "scope": "institution",
        "tier": "A",
        "description": "Primary administrator for a single school. Provisioned automatically when a school is onboarded; manages branches, staff, and school-wide settings.",
    },
    {
        "key": "branch_admin",
        "name": "Branch Admin",
        "scope": "branch",
        "tier": "A",
        "description": "Administrative manager of a single branch.",
    },
    {
        "key": "teacher",
        "name": "Teacher",
        "scope": "branch",
        "tier": "B",
        "description": "Teaching staff member scoped to a branch. Sensible default role for STAFF-type users invited as teachers.",
    },
]


class Command(BaseCommand):
    help = "Seed PrebuiltRoleTemplate records (school_admin and branch_admin, no permissions attached)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print actions without writing.")
        parser.add_argument("--reset", action="store_true", help="Delete all prebuilt roles and re-seed. Dev only.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        reset = options["reset"]

        if reset:
            if dry_run:
                self.stdout.write(self.style.WARNING("--reset ignored in --dry-run mode."))
            else:
                deleted_perms, _ = PrebuiltRolePermission.objects.all().delete()
                deleted_roles, _ = PrebuiltRoleTemplate.objects.all().delete()
                self.stdout.write(self.style.WARNING(f"Reset: deleted {deleted_roles} roles, {deleted_perms} permission links."))

        created = 0
        updated = 0

        for data in PREBUILT_ROLES:
            key = data["key"]
            if dry_run:
                self.stdout.write(f"  [dry-run] Would upsert PrebuiltRoleTemplate key={key}")
                continue

            _, was_created = PrebuiltRoleTemplate.objects.update_or_create(
                key=key,
                defaults={
                    "name": data["name"],
                    "description": data.get("description", ""),
                    "scope": data["scope"],
                    "tier": data["tier"],
                    "is_active": True,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1
            self.stdout.write(f"  {'Created' if was_created else 'Updated'}: {key}")

        self.stdout.write(self.style.SUCCESS(f"\nDone. created={created} updated={updated}"))
