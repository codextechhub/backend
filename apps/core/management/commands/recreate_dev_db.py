"""Drop and create the development database, as Django connects to it.

``reseed-dev.sh`` starts here. The database dropped is resolved from the same
settings every later step connects with (``DB_NAME``, ``DB_USER``,
``DB_PASSWORD``, ``DB_HOST`` and ``DB_PORT``, from the environment or
``apps/.env``), so the database recreated is always the one then migrated and
seeded. ``psql`` and ``dropdb`` left to their own defaults connect as the
operating-system user over the local socket and read none of those, so they can
recreate one database while Django migrates another, or fail to sign in at all.

The statements run on PostgreSQL's maintenance database, the way Django's test
runner creates a test database, because a database cannot be dropped over a
connection to itself. Other sessions on it (an open psql or DBeaver tab) are
terminated first, or ``DROP DATABASE`` waits on them.

The refusals are ``reset_db``'s (``resolve_safe_target``): development and test
settings only, and only a database named in ``RESET_DB_ALLOWED_DATABASES``.
Without ``--noinput`` the database's name must be typed back.
"""

from django.core.management.base import BaseCommand, CommandError

from .reset_db import resolve_safe_target


class Command(BaseCommand):
    help = (
        "DESTRUCTIVE: drop and create the allowlisted development database "
        "Django is configured to use"
    )

    # The database may not exist yet; nothing here may need it before it does.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="default",
            help='Database alias to use (default: "default")',
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Do not ask for the database name to be typed back.",
        )

    def handle(self, *args, **options):
        connection, name, host = resolve_safe_target(options["database"])
        if connection.vendor != "postgresql":
            raise CommandError(
                f"Only PostgreSQL databases are recreated, not {connection.vendor}."
            )

        if options["interactive"]:
            try:
                typed = input(f"Type {name!r} on {host} to drop and recreate it: ")
            except (EOFError, KeyboardInterrupt) as exc:
                raise CommandError("No database-name confirmation received.") from exc
            if typed.strip() != name:
                raise CommandError("Database-name confirmation did not match. Aborting.")

        connection.close()
        quoted = connection.ops.quote_name(name)
        with connection._nodb_cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                [name],
            )
            cursor.execute(f"DROP DATABASE IF EXISTS {quoted}")
            cursor.execute(f"CREATE DATABASE {quoted}")

        self.stdout.write(f"Recreated {name!r} on {host}.")
