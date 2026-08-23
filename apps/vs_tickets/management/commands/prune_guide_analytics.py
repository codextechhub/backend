from django.core.management.base import BaseCommand

from vs_tickets.analytics import RETENTION_DAYS, prune


class Command(BaseCommand):
    help = f"Delete disposable guide analytics older than {RETENTION_DAYS} days."

    def handle(self, *args, **options):
        deleted = prune()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} guide analytics events."))
