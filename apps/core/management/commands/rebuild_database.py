"""Drop and recreate the public schema so migrations apply from zero.

Written for the tenant-refactor cutover: the migration history was squashed to
fresh 0001 chains, so any database that carries the old django_migrations rows
must be rebuilt rather than migrated forward. DESTROYS ALL DATA — guarded twice
(the --yes flag AND the RESET_DB env var must both be present) so a stray
invocation in a build pipeline can never wipe an environment by accident.
"""
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = (
        "DESTRUCTIVE: drop and recreate the public schema (PostgreSQL) so the "
        "squashed migration chain applies from zero. Requires --yes AND "
        "RESET_DB=true in the environment."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Confirm you intend to destroy every table in this database.",
        )

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError("Refusing to run without --yes (destroys all data).")
        if os.environ.get("RESET_DB", "").lower() != "true":
            raise CommandError(
                "Refusing to run without RESET_DB=true in the environment "
                "(second guard against accidental pipeline wipes)."
            )
        if connection.vendor != "postgresql":
            raise CommandError("Only implemented for PostgreSQL.")

        db_name = connection.settings_dict.get("NAME")
        self.stdout.write(self.style.WARNING(
            f"Dropping and recreating schema 'public' on database {db_name!r}…"
        ))
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        self.stdout.write(self.style.SUCCESS(
            "Schema rebuilt. Run `migrate` and the seed commands next."
        ))
