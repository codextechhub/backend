# =============================================================================
# vs_notifications / management / commands / seed_notification_settings.py
#
# Seeds the platform-wide (school=NULL) NotificationSetting rows from each
# active event type's default_enabled. These are the platform defaults that
# resolve_channels() layers school overrides on top of.
#
# Optionally materialises per-school override rows with --school <slug> / --all
# (an explicit override path; platform defaults already cover every school).
# Uses get_or_create — existing admin-configured rows are never overwritten.
#
# Usage:
#   python manage.py seed_notification_settings                 # platform rows
#   python manage.py seed_notification_settings --school <slug> # + one school
#   python manage.py seed_notification_settings --all           # + every school
# =============================================================================

from django.core.management.base import BaseCommand, CommandError

from vs_notifications.services.seed import seed_platform_settings, seed_school_settings


class Command(BaseCommand):
    help = (
        "Seed platform-wide NotificationSetting rows (school=NULL) from each "
        "event type's default_enabled. Optionally add per-school override rows "
        "with --school <slug> or --all. Uses get_or_create — existing settings "
        "are never overwritten."
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=False)
        group.add_argument(
            "--school",
            type=str,
            metavar="SLUG",
            help="Also materialise per-school override rows for this school slug.",
        )
        group.add_argument(
            "--all",
            action="store_true",
            dest="all_schools",
            help="Also materialise per-school override rows for every active school.",
        )

    def handle(self, *args, **options):
        # ── Platform defaults (always) ─────────────────────────────────────
        self.stdout.write("Seeding platform notification settings (school=NULL)...")
        platform = seed_platform_settings()
        self.stdout.write(
            self.style.SUCCESS(
                f"  platform: created={platform['created']}, skipped={platform['skipped']}."
            )
        )

        if not options.get("school") and not options.get("all_schools"):
            return

        from vs_schools.models import School  # Late import — avoids coupling at module load

        if options["all_schools"]:
            schools = School.objects.filter(status="ACTIVE")
            self.stdout.write(f"Seeding per-school override rows for {schools.count()} school(s)...")
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

            self.stdout.write(f"Seeding per-school override rows for school '{slug}'...")
            result = seed_school_settings(school)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. Created: {result['created']}, Skipped: {result['skipped']}."
                )
            )
