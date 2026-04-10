# del_migration.py
import os
import glob
from django.core.management.base import BaseCommand
from django.conf import settings

class Command(BaseCommand):
    help = 'Deletes all migration files across all apps'

    def handle(self, *args, **kwargs):
        # Iterate through all installed apps
        deleted_files = []
        installed_apps = ['vs_admin_console', 'vs_user', 'vs_schools', 'vs_rbac', 'vs_audit']  # List your apps here

        # Ask for confirmation before deleting anything
        prompt = "This will delete all migration .py files in the following apps: {}. Continue? [y/N]: ".format(', '.join(installed_apps))
        try:
            confirm = input(prompt)
        except (EOFError, KeyboardInterrupt):
            self.stdout.write(self.style.WARNING("No input received. Operation cancelled."))
            return

        if confirm.strip().lower() not in ('y', 'yes'):
            self.stdout.write(self.style.WARNING("Operation cancelled by user."))
            return

        for app in installed_apps:
            # Only consider apps that have a 'migrations' folder
            migration_dir = os.path.join(os.getcwd(), app, 'migrations')
            
            if os.path.exists(migration_dir):
                # Get all Python files in the migrations folder, excluding __init__.py
                migration_files = glob.glob(os.path.join(migration_dir, "*.py"))
                
                for file_path in migration_files:
                    # Skip __init__.py because it's required for the folder to be a package
                    if os.path.basename(file_path) == '__init__.py':
                        continue
                    try:
                        os.remove(file_path)
                        deleted_files.append(file_path)
                        self.stdout.write(self.style.SUCCESS(f"Deleted {file_path}"))
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"Failed to delete {file_path}: {str(e)}"))

        if deleted_files:
            self.stdout.write(self.style.SUCCESS(f"Deleted {len(deleted_files)} migration files"))
        else:
            self.stdout.write(self.style.SUCCESS("No migration files found to delete"))
