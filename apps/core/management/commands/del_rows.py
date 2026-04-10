from django.core.management.base import BaseCommand
from ...models import Product, School, GlobalUser, UserRole, PlatformStaff, SchoolUser, UserActivityLog, APIKey, SystemSetting
from django.db import connection

class Command(BaseCommand):
    help = 'Truncate specified tables by deleting all records. For MySQL this disables FK checks and uses TRUNCATE.'

    def handle(self, *args, **kwargs):
        tables_to_truncate = [
            Product,
            School,
            GlobalUser,
            UserRole,
            PlatformStaff,
            SchoolUser,
            UserActivityLog,
            APIKey,
            SystemSetting,
        ]

        model_names = ", ".join([m.__name__ for m in tables_to_truncate])
        confirm = input(
            f"\nAre you sure you want to DELETE ALL RECORDS from the following models: {model_names}\n"
            "Type 'yes' to confirm: "
        )
        if confirm.lower() != 'yes':
            self.stdout.write(self.style.WARNING("Deletion cancelled."))
            return

        if connection.vendor == 'mysql':
            self.stdout.write("Using MySQL path: disabling FOREIGN_KEY_CHECKS and truncating tables.")
            cursor = connection.cursor()
            try:
                cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")
                for model in tables_to_truncate:
                    table_name = model._meta.db_table
                    qname = connection.ops.quote_name(table_name)
                    self.stdout.write(f"Truncating table: {table_name}")
                    try:
                        cursor.execute(f"TRUNCATE TABLE {qname};")
                        self.stdout.write(self.style.SUCCESS(f"Truncated {model.__name__} ({table_name})"))
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"Error truncating {model.__name__} ({table_name}): {e}"))
                cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")
            finally:
                # ensure FK checks are re-enabled if something went wrong before the last statement
                try:
                    cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")
                except Exception:
                    pass
                cursor.close()
        else:
            # fallback: use Django ORM delete (respects on_delete behavior)
            self.stdout.write("Non-MySQL DB detected: using Django ORM delete() for each model.")
            for model in tables_to_truncate:
                self.stdout.write(f"Deleting rows for model: {model.__name__}")
                try:
                    model.objects.all().delete()
                    self.stdout.write(self.style.SUCCESS(f"Deleted all rows for {model.__name__}"))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Error deleting {model.__name__}: {e}"))

        self.stdout.write(self.style.SUCCESS("Operation completed."))
