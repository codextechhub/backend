from django.core.management.base import BaseCommand

from vs_health import seed


class Command(BaseCommand):
    help = (
        "Seed Health configuration: services, checks, alert rules, SLO targets, and "
        "RBAC permissions. Never writes telemetry — all measurements come from the "
        "live collectors (request middleware, probes, queue snapshots)."
    )

    def handle(self, *args, **options):
        seed.run(stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS("vs_health seeded."))
