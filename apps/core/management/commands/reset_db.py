import os
import glob
import sys
import pymysql as sql

from django.core.management.base import BaseCommand
from django.core.management import call_command


class Command(BaseCommand):
    help = "Delete migration files, drop database tables, and migrate again"

    def add_arguments(self, parser):
        parser.add_argument(
            "db_name",
            type=str,
            help="Name of the database to reset",
        )

    def handle(self, *args, **options):
        db_name = options["db_name"]

        # Update this list to match your actual apps
        installed_apps = [
            "vs_admin_console",
            "vs_user",
            "vs_institutions",
            "vs_rbac",
            "vs_audit",
        ]

        self.stdout.write(self.style.WARNING(
            f"\nThis will do the following on database `{db_name}`:\n"
            f"1. Delete migration files in: {', '.join(installed_apps)}\n"
            f"2. Drop all tables in the database\n"
            f"3. Run makemigrations\n"
            f"4. Run migrate\n"
        ))

        self.stdout.write("Continue? [y/N]: ")
        confirm = sys.stdin.readline().strip().lower()

        if confirm.strip().lower() not in ("y", "yes"):
            self.stdout.write(self.style.WARNING("Operation cancelled by user."))
            return

        # -----------------------------
        # STEP 1: DELETE MIGRATION FILES
        # -----------------------------
        self.stdout.write(self.style.NOTICE("\nDeleting migration files..."))
        deleted_files = []

        for app in installed_apps:
            migration_dir = os.path.join(os.getcwd(), app, "migrations")

            if os.path.exists(migration_dir):
                migration_files = glob.glob(os.path.join(migration_dir, "*.py"))

                for file_path in migration_files:
                    if os.path.basename(file_path) == "__init__.py":
                        continue

                    try:
                        os.remove(file_path)
                        deleted_files.append(file_path)
                        self.stdout.write(self.style.SUCCESS(f"Deleted {file_path}"))
                    except Exception as e:
                        self.stdout.write(
                            self.style.ERROR(f"Failed to delete {file_path}: {str(e)}")
                        )

        if deleted_files:
            self.stdout.write(
                self.style.SUCCESS(f"Deleted {len(deleted_files)} migration file(s).")
            )
        else:
            self.stdout.write(self.style.WARNING("No migration files found to delete."))

        # -----------------------------
        # STEP 2: DROP ALL TABLES
        # -----------------------------
        self.stdout.write(self.style.NOTICE(f"\nDropping all tables in `{db_name}`..."))

        conn = None
        try:
            conn = sql.connect(
                host="localhost",
                user="root",
                password="",
                database=db_name,
            )
            cursor = conn.cursor()

            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            cursor.execute("SHOW TABLES")
            tables = cursor.fetchall()

            for table in tables:
                table_name = table[0]
                cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`")
                self.stdout.write(self.style.SUCCESS(f"Dropped table `{table_name}`"))

            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
            conn.commit()

            self.stdout.write(self.style.SUCCESS("All tables dropped successfully."))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error while dropping tables: {e}"))
            return

        finally:
            if conn:
                conn.close()

        # -----------------------------
        # STEP 3: MAKE MIGRATIONS
        # -----------------------------
        self.stdout.write(self.style.NOTICE("\nMaking migrations..."))
        call_command("makemigrations")

        # -----------------------------
        # STEP 4: MIGRATE
        # -----------------------------
        self.stdout.write(self.style.NOTICE("\nRunning migrations..."))
        call_command("migrate", database="default")

        self.stdout.write(self.style.SUCCESS("\nDatabase reset complete."))