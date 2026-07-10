from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Compatibility alias for seed_config_catalogue."

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING(
            "seed_xvs_modules is deprecated; running seed_config_catalogue."
        ))
        call_command("seed_config_catalogue", stdout=self.stdout, stderr=self.stderr)
