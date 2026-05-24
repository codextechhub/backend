# =============================================================================
# vs_notifications / management / commands / seed_notification_event_types.py
#
# Creates or updates all NotificationEventType records from EVENT_TYPE_REGISTRY.
# Safe to run repeatedly — uses update_or_create on the key field.
#
# Usage:
#   python manage.py seed_notification_event_types
# =============================================================================

from django.core.management.base import BaseCommand

from vs_notifications.services.seed import seed_event_types


class Command(BaseCommand):
    help = (
        "Seed or update all NotificationEventType records from the "
        "EVENT_TYPE_REGISTRY defined in vs_notifications/constants.py. "
        "Safe to run repeatedly — existing records are updated, not duplicated."
    )

    def handle(self, *args, **options):
        self.stdout.write("Seeding notification event types...")
        result = seed_event_types()
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created: {result['created']}, Updated: {result['updated']}."
            )
        )
