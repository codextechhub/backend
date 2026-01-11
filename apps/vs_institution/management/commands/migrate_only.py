from django.core.management.base import BaseCommand
from django.core.management import call_command

class Command(BaseCommand):
    help = 'Run migrations for all product databases'

    def handle(self, *args, **options):
        self.stdout.write('Migrating codex_db...')
        call_command('migrate', 'codex_db', database='default')

        self.stdout.write('Migrating vision/core...')
        call_command('migrate', 'core', database='vision_db')

        self.stdout.write('Migrating vision/students...')
        call_command('migrate', 'students', database='vision_db')

        self.stdout.write('Migrating vision/staff...')
        call_command('migrate', 'staff', database='vision_db')

        self.stdout.write(self.style.SUCCESS('All migrations complete!'))
