"""Seed default finance reference data for an entity.

Usage:
    manage.py seed_finance                 # seeds the CODEX platform entity
    manage.py seed_finance --entity LEKKI  # seeds a specific entity by code
    manage.py seed_finance --all           # seeds every active entity

Always seeds the default currencies, then a starter Chart of Accounts for the chosen
entity/entities. Idempotent — safe to re-run.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from vs_finance.models import LedgerEntity
from vs_finance.seed import seed_chart_of_accounts, seed_currencies


class Command(BaseCommand):
    help = "Seed default currencies and a starter Chart of Accounts for an entity."

    def add_arguments(self, parser):
        parser.add_argument("--entity", help="LedgerEntity code to seed (defaults to the platform entity).")
        parser.add_argument("--all", action="store_true", help="Seed all active entities.")

    def handle(self, *args, **options):
        n = seed_currencies()
        self.stdout.write(self.style.SUCCESS(f"Currencies seeded ({n})."))

        if options["all"]:
            entities = list(LedgerEntity.objects.filter(is_active=True))
        elif options["entity"]:
            entity = LedgerEntity.objects.filter(code=options["entity"]).first()
            if entity is None:
                raise CommandError(f"No LedgerEntity with code '{options['entity']}'.")
            entities = [entity]
        else:
            platform = LedgerEntity.objects.platform()
            if platform is None:
                raise CommandError("No platform entity found; run migrations first.")
            entities = [platform]

        for entity in entities:
            accounts = seed_chart_of_accounts(entity)
            self.stdout.write(
                self.style.SUCCESS(f"  {entity.code}: chart of accounts seeded ({len(accounts)} accounts).")
            )
