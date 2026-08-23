# =============================================================================
# vs_notifications / management / commands / seed_notification_templates.py
#
# Creates default NotificationTemplate records for all active event types.
# Uses get_or_create - never overwrites templates Vision Staff have customised.
#
# Usage:
#   python manage.py seed_notification_templates
# =============================================================================

from django.core.management.base import BaseCommand

from vs_notifications.services.seed import seed_notification_templates


class Command(BaseCommand):
    help = (
        "Seed default NotificationTemplate records for all active event types "
        "and their supported channels. Uses get_or_create - Vision Staff "
        "customisations are never overwritten. Pass --overwrite to resync every "
        "seeded template back to the shipped default (this DOES discard staff "
        "edits, including any bespoke html_body)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Reset every seeded template to the shipped default copy.",
        )

    def handle(self, *args, **options):
        overwrite = options["overwrite"]
        self.stdout.write(
            "Resyncing notification templates to defaults..." if overwrite
            else "Seeding default notification templates..."
        )
        result = seed_notification_templates(overwrite=overwrite)
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created: {result['created']}, "
                f"Updated: {result['updated']}, Skipped: {result['skipped']}."
            )
        )
