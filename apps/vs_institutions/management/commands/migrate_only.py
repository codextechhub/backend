from django.core.management.base import BaseCommand
from django.core.management import call_command

class Command(BaseCommand):
    help = 'Run migrations for all product databases'

    def handle(self, *args, **options):
        self.stdout.write('Migrating cx_db...')
        call_command('migrate', database='default')

        self.stdout.write(self.style.SUCCESS('All migrations complete!'))
