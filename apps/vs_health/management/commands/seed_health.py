from django.core.management.base import BaseCommand

from vs_health import seed


class Command(BaseCommand):
    help = "Seed Health: services, checks, alert rules, SLOs, RBAC permissions, and synthetic history."

    def handle(self, *args, **options):
        seed.run(stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS("vs_health seeded."))
