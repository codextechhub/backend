from django.core.management.base import BaseCommand
from django.core.management import call_command

class Command(BaseCommand):
    help = 'Run migrations for all product databases'

    def handle(self, *args, **options):
        self.stdout.write('Migrating codex_db...')
        call_command('makemigrations', 'codex_db')
        call_command('migrate', database='default')

        self.stdout.write('Migrating vision/core...')
        call_command('makemigrations', 'core')
        call_command('migrate', database='vision_db')

        self.stdout.write('Migrating vision/students...')
        call_command('makemigrations', 'students')
        call_command('migrate', database='vision_db')

        self.stdout.write('Migrating vision/staff...')
        call_command('makemigrations', 'staff')
        call_command('migrate', database='vision_db')
        
        self.stdout.write('Migrating vision/facilities...')
        call_command('makemigrations', 'facilities')
        call_command('migrate', database='vision_db')

        self.stdout.write(self.style.SUCCESS('All migrations complete!'))
