"""Reset a disposable development or test database.

This command destroys every table reached through the selected Django database
alias, rebuilds the schema, and runs the requested seed commands. It is local
development and test tooling only. Safety checks run before every prompt and
cannot be bypassed with ``--yes``.
"""

import os

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.utils import ConnectionDoesNotExist


SAFE_SETTINGS_MODULES = frozenset(
    {
        "apps.settings.local",
        "apps.settings.test",
        "apps.settings.ci",
    }
)
TEST_SETTINGS_MODULES = frozenset(
    {
        "apps.settings.test",
        "apps.settings.ci",
    }
)


class Command(BaseCommand):
    help = (
        "DESTRUCTIVE: reset an explicitly allowlisted development or test "
        "database, rebuild its schema, and run seed commands"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="default",
            help='Database alias to use (default: "default")',
        )
        parser.add_argument(
            "--skip-drop",
            action="store_true",
            help="Skip dropping database tables",
        )
        parser.add_argument(
            "--skip-migrate",
            action="store_true",
            help="Skip running migrations",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help=(
                "Auto-confirm step prompts after the mandatory database-name "
                "confirmation"
            ),
        )
        parser.add_argument(
            "--post-commands",
            nargs="+",
            default=[
                "seed_actions",
                "seed_all_permissions",
                "create_superuser",
                "seed_package",
                "seed_config_catalogue",
            ],
            help="Commands to run after migrations complete",
        )

    def handle(self, *args, **options):
        self.database_alias = options["database"]
        self.auto_confirm = options["yes"]
        self.connection, self.database_name, self.database_host = (
            self._resolve_safe_target()
        )

        self._display_warning()
        self._require_database_name_confirmation()

        if not options["skip_drop"]:
            self._drop_database_tables()
        else:
            self.stdout.write(self.style.NOTICE("Skipping table drop"))

        if not options["skip_migrate"]:
            self._run_fresh_migrations()
        else:
            self.stdout.write(self.style.NOTICE("Skipping migrations"))

        self._run_post_migration_commands(options.get("post_commands"))

        self.stdout.write(self.style.SUCCESS("\n" + "=" * 60))
        self.stdout.write(self.style.SUCCESS("Database reset completed successfully!"))
        self.stdout.write(self.style.SUCCESS("=" * 60))

    def _resolve_safe_target(self):
        settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
        if settings_module not in SAFE_SETTINGS_MODULES:
            raise CommandError(
                "Refusing to run outside the development and test settings "
                f"modules. Active settings: {settings_module or '<unknown>'}."
            )

        if not settings.DEBUG and settings_module not in TEST_SETTINGS_MODULES:
            raise CommandError(
                "Refusing to run with DEBUG=False outside an approved test "
                "settings module."
            )

        try:
            connection = connections[self.database_alias]
        except ConnectionDoesNotExist as exc:
            raise CommandError(
                f"Unknown database alias: {self.database_alias}."
            ) from exc

        database_name = str(connection.settings_dict.get("NAME") or "").strip()
        database_host = str(connection.settings_dict.get("HOST") or "localhost")
        allowed_names = {
            str(name).strip()
            for name in getattr(settings, "RESET_DB_ALLOWED_DATABASES", ())
            if str(name).strip()
        }

        if not database_name:
            raise CommandError("Refusing to run without a resolved database name.")
        if database_name not in allowed_names:
            allowed_label = ", ".join(sorted(allowed_names)) or "<none configured>"
            raise CommandError(
                f"Database {database_name!r} is not allowlisted for reset. "
                f"Allowed names: {allowed_label}."
            )

        return connection, database_name, database_host

    def _display_warning(self):
        self.stdout.write(self.style.WARNING("\n" + "=" * 60))
        self.stdout.write(self.style.WARNING("WARNING: DATABASE RESET OPERATION"))
        self.stdout.write(self.style.WARNING("=" * 60))
        self.stdout.write(
            self.style.WARNING(f"Database alias: {self.database_alias}")
        )
        self.stdout.write(self.style.WARNING(f"Database name: {self.database_name}"))
        self.stdout.write(self.style.WARNING(f"Database host: {self.database_host}"))
        self.stdout.write(self.style.WARNING("This command will:"))
        self.stdout.write(self.style.WARNING("  1. Drop all database tables"))
        self.stdout.write(self.style.WARNING("  2. Run migrations"))
        self.stdout.write(self.style.WARNING("  3. Run seed commands"))
        self.stdout.write(self.style.WARNING("=" * 60 + "\n"))

    def _require_database_name_confirmation(self):
        prompt = (
            f"Type the resolved database name {self.database_name!r} to continue: "
        )
        try:
            confirmation = input(prompt)
        except (EOFError, KeyboardInterrupt) as exc:
            raise CommandError("No database-name confirmation received.") from exc

        if confirmation.strip() != self.database_name:
            raise CommandError("Database-name confirmation did not match. Aborting.")

    def _confirm_action(self, message):
        try:
            confirmation = input(f"{message} [y/N]: ")
            return confirmation.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            self.stdout.write(self.style.WARNING("\nNo input received."))
            return False

    def _drop_database_tables(self):
        self.stdout.write(self.style.NOTICE("\n" + "-" * 60))
        self.stdout.write(self.style.NOTICE("STEP 1: Dropping database tables"))
        self.stdout.write(self.style.NOTICE("-" * 60))

        if not self.auto_confirm and not self._confirm_action(
            f"Drop ALL tables from {self.database_name!r}?"
        ):
            self.stdout.write(self.style.WARNING("Skipping table drop"))
            return

        connection = self.connection
        try:
            vendor = connection.vendor
            self.stdout.write(self.style.NOTICE(f"Database vendor: {vendor}"))
            table_names = connection.introspection.table_names()

            if not table_names:
                self.stdout.write(self.style.SUCCESS("No tables found in database"))
                return

            self.stdout.write(f"Found {len(table_names)} table(s) to drop")
            with connection.cursor() as cursor:
                if vendor == "postgresql":
                    for table_name in table_names:
                        cursor.execute(
                            f'DROP TABLE IF EXISTS "{table_name}" CASCADE'
                        )
                        self.stdout.write(
                            self.style.SUCCESS(f"  Dropped {table_name}")
                        )
                elif vendor == "mysql":
                    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                    try:
                        for table_name in table_names:
                            cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`")
                            self.stdout.write(
                                self.style.SUCCESS(f"  Dropped {table_name}")
                            )
                    finally:
                        cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
                elif vendor == "sqlite":
                    for table_name in table_names:
                        cursor.execute(f'DROP TABLE IF EXISTS "{table_name}"')
                        self.stdout.write(
                            self.style.SUCCESS(f"  Dropped {table_name}")
                        )
                else:
                    raise CommandError(f"Unsupported database vendor: {vendor}")

            connection.commit()
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nSuccessfully dropped {len(table_names)} table(s)"
                )
            )
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f"Failed to drop tables: {exc}") from exc

    def _run_fresh_migrations(self):
        self.stdout.write(self.style.NOTICE("\n" + "-" * 60))
        self.stdout.write(self.style.NOTICE("STEP 2: Running migrations"))
        self.stdout.write(self.style.NOTICE("-" * 60))

        if not self.auto_confirm and not self._confirm_action(
            f"Run migrations on {self.database_name!r}?"
        ):
            self.stdout.write(self.style.WARNING("Skipping migrations"))
            return

        try:
            call_command("migrate", database=self.database_alias)
            self.stdout.write(self.style.SUCCESS("\nMigrations completed successfully"))
        except Exception as exc:
            raise CommandError(f"Failed to run migrations: {exc}") from exc

    def _run_post_migration_commands(self, commands):
        if not commands:
            return

        self.stdout.write(self.style.NOTICE("\n" + "-" * 60))
        self.stdout.write(self.style.NOTICE("STEP 3: Running seed commands"))
        self.stdout.write(self.style.NOTICE("-" * 60))

        if not self.auto_confirm:
            commands_label = ", ".join(commands)
            if not self._confirm_action(f"Run these commands: {commands_label}?"):
                self.stdout.write(self.style.WARNING("Skipping seed commands"))
                return

        for command in commands:
            try:
                self.stdout.write(self.style.NOTICE(f"\nRunning: {command}"))
                command_parts = command.split()
                call_command(command_parts[0], *command_parts[1:])
                self.stdout.write(self.style.SUCCESS(f"  Completed: {command}"))
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  Failed: {command}"))
                self.stdout.write(self.style.ERROR(f"    Error: {exc}"))
                if not self.auto_confirm and not self._confirm_action(
                    "Continue with remaining commands?"
                ):
                    self.stdout.write(
                        self.style.WARNING("Stopping remaining seed commands")
                    )
                    return

        self.stdout.write(self.style.SUCCESS("\nSeed commands completed"))
