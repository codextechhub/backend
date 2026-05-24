# =============================================================================
# vs_notifications / management / commands / seed_notification_settings.py
#
# Creates SchoolNotificationSetting records for one school or all schools.
# Uses get_or_create — never overwrites existing admin-configured settings.
#
# Usage:
#   python manage.py seed_notification_settings --school <slug>
#   python manage.py seed_notification_settings --all
# =============================================================================

from django.core.management.base import BaseCommand, CommandError

from vs_notifications.services.seed import seed_school_settings


class Command(BaseCommand):
    help = (
        "Seed SchoolNotificationSetting records for a school. "
        "Pass --school <slug> for one school, or --all for every school. "
        "Uses get_or_create — existing settings are never overwritten."
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--school",
            type=str,
            metavar="SLUG",
            help="School slug to seed settings for.",
        )
        group.add_argument(
            "--all",
            action="store_true",
            dest="all_schools",
            help="Seed settings for every active school.",
        )

    def handle(self, *args, **options):
        from vs_schools.models import School  # Late import — avoids coupling at module load

        if options["all_schools"]:
            schools = School.objects.filter(is_active=True)
            self.stdout.write(f"Seeding notification settings for {schools.count()} school(s)...")
            total_created = 0
            total_skipped = 0
            for school in schools:
                result = seed_school_settings(school)
                total_created += result["created"]
                total_skipped += result["skipped"]
                self.stdout.write(
                    f"  {school.slug}: created={result['created']}, skipped={result['skipped']}"
                )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. Total created: {total_created}, Total skipped: {total_skipped}."
                )
            )

        else:
            slug = options["school"]
            try:
                school = School.objects.get(slug=slug)
            except School.DoesNotExist:
                raise CommandError(f"School with slug '{slug}' not found.")

            self.stdout.write(f"Seeding notification settings for school '{slug}'...")
            result = seed_school_settings(school)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. Created: {result['created']}, Skipped: {result['skipped']}."
                )
            )
