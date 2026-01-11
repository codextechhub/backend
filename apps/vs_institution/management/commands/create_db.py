import pymysql as sql
from django.conf import settings
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = "Create multiple MySQL databases (e.g., codex_db, vision_db) using pymysql."

    def handle(self, *args, **kwargs):

        conn = sql.connect(
            host='localhost',
            user='root',
            password=''   
        )
        cursor = conn.cursor()

        databases = getattr(
            settings,
            'PRODUCT_DATABASES',
            ['codex_db', 'vision_db'] 
        )

        try:
            for db in databases:
                cursor.execute(f'CREATE DATABASE IF NOT EXISTS `{db}`')
                self.stdout.write(
                    self.style.SUCCESS(f"Database '{db}' created successfully")
                )
            conn.commit()

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error: {e}"))

        finally:
            conn.close()
            self.stdout.write(self.style.SUCCESS("All operations completed."))
