# =============================================================================
# vs_notifications / management / commands / seed_notification_templates.py
#
# Creates default NotificationTemplate records for all active event types.
# Uses get_or_create — never overwrites templates Vision Staff have customised.
#
# Usage:
#   python manage.py seed_notification_templates
# =============================================================================

from django.core.management.base import BaseCommand

from vs_notifications.services.seed import seed_notification_templates


class Command(BaseCommand):
    help = (
        "Seed default NotificationTemplate records for all active event types "
        "and their supported channels. Uses get_or_create — Vision Staff "
        "customisations are never overwritten."
    )

    def handle(self, *args, **options):
        self.stdout.write("Seeding default notification templates...")
        result = seed_notification_templates()
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created: {result['created']}, Skipped: {result['skipped']}."
            )
        )
