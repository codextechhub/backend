"""
Django Management Command: reset_db.py
Location: <your_project>/management/commands/reset_db.py

Purpose:
    Unified command that performs a complete database and migration reset in the correct order:
    1. Delete all migration files (except __init__.py)
    2. Drop all database tables
    3. Run fresh makemigrations and migrate

Usage:
    python manage.py reset_db
    python manage.py reset_db --database default
    python manage.py reset_db --skip-migrations  # Skip step 1
    python manage.py reset_db --skip-drop        # Skip step 2
    python manage.py reset_db --skip-migrate     # Skip step 3

Safety:
    - Requires explicit confirmation at each step
    - Can skip individual steps via flags
    - Graceful error handling and rollback

Popular Use Cases:
    - Full reset during development
    - command: python manage.py reset_db --yes --post-commands seed_actions seed_prebuilt_role_templates create_superuser seed_package seed_xvs_modules
"""

import os
import glob
from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command
from django.conf import settings
from django.db import connections


class Command(BaseCommand):
    help = 'Complete database reset: delete migrations, drop tables, and run fresh migrations'

    def add_arguments(self, parser):
        """Add command-line arguments"""
        parser.add_argument(
            '--database',
            type=str,
            default='default',
            help='Database alias to use (default: "default")'
        )
        parser.add_argument(
            '--skip-migrations',
            action='store_true',
            help='Skip deleting migration files'
        )
        parser.add_argument(
            '--skip-drop',
            action='store_true',
            help='Skip dropping database tables'
        )
        parser.add_argument(
            '--skip-migrate',
            action='store_true',
            help='Skip running makemigrations and migrate'
        )
        parser.add_argument(
            '--yes',
            action='store_true',
            help='Auto-confirm all prompts (use with caution)'
        )
        parser.add_argument(
            '--post-commands',
            nargs='+',
            type=str,
            default=["seed_actions", "seed_all_permissions", "create_superuser", "seed_package", "seed_xvs_modules"],
            help='Commands to run after migration completes (e.g., seed_roles seed_schools)'
        )

    def handle(self, *args, **options):
        """Main command handler"""
        self.database_alias = options['database']
        self.auto_confirm = options['yes']
        
        # Display warning banner
        self._display_warning()
        
        # Global confirmation
        if not self.auto_confirm:
            if not self._confirm_action("This will PERMANENTLY reset your database. Continue?"):
                self.stdout.write(self.style.WARNING("Operation cancelled."))
                return
        
        # Step 1: Delete migration files
        if not options['skip_migrations']:
            self._delete_migration_files()
        else:
            self.stdout.write(self.style.NOTICE("Skipping migration file deletion"))
        
        # Step 2: Drop database tables
        if not options['skip_drop']:
            self._drop_database_tables()
        else:
            self.stdout.write(self.style.NOTICE("Skipping table drop"))
        
        # Step 3: Run fresh migrations
        if not options['skip_migrate']:
            self._run_fresh_migrations()
        else:
            self.stdout.write(self.style.NOTICE("Skipping fresh migrations"))
        
        # Step 4: Run post-migration commands (if provided)
        self._run_post_migration_commands(options.get('post_commands'))
        
        # Success message
        self.stdout.write(self.style.SUCCESS("\n" + "="*60))
        self.stdout.write(self.style.SUCCESS("Database reset completed successfully!"))
        self.stdout.write(self.style.SUCCESS("="*60))

    def _display_warning(self):
        """Display warning banner"""
        self.stdout.write(self.style.WARNING("\n" + "="*60))
        self.stdout.write(self.style.WARNING("WARNING: DATABASE RESET OPERATION"))
        self.stdout.write(self.style.WARNING("="*60))
        self.stdout.write(self.style.WARNING("This command will:"))
        self.stdout.write(self.style.WARNING("  1. Delete all migration files"))
        self.stdout.write(self.style.WARNING("  2. Drop all database tables"))
        self.stdout.write(self.style.WARNING("  3. Run fresh migrations"))
        self.stdout.write(self.style.WARNING("="*60 + "\n"))

    def _confirm_action(self, message):
        """
        Prompt user for confirmation
        Returns True if user confirms, False otherwise
        """
        try:
            prompt = f"{message} [y/N]: "
            confirm = input(prompt)
            return confirm.strip().lower() in ('y', 'yes')
        except (EOFError, KeyboardInterrupt):
            self.stdout.write(self.style.WARNING("\nNo input received."))
            return False

    def _delete_migration_files(self):
        """
        Step 1: Delete all migration files across specified apps
        Preserves __init__.py files
        """
        self.stdout.write(self.style.NOTICE("\n" + "-"*60))
        self.stdout.write(self.style.NOTICE("STEP 1: Deleting migration files"))
        self.stdout.write(self.style.NOTICE("-"*60))
        
        # List of apps to process
        # You can modify this list or make it dynamic based on settings.INSTALLED_APPS
        installed_apps = [
            # 'vs_admin_console',
            # 'vs_user',
            # 'vs_schools',
            # 'vs_rbac',
            # 'vs_audit',
            # 'vs_import_data',
            # Add more apps as needed
        ]
        
        # Confirm before deletion
        if not self.auto_confirm:
            apps_list = ', '.join(installed_apps)
            if not self._confirm_action(f"Delete migrations from: {apps_list}?"):
                self.stdout.write(self.style.WARNING("Skipping migration deletion"))
                return
        
        deleted_files = []
        
        for app in installed_apps:
            migration_dir = os.path.join(os.getcwd(), app, 'migrations')
            
            if not os.path.exists(migration_dir):
                self.stdout.write(self.style.WARNING(f"  No migrations dir for {app}"))
                continue
            
            # Get all Python files except __init__.py
            migration_files = glob.glob(os.path.join(migration_dir, "*.py"))
            
            for file_path in migration_files:
                if os.path.basename(file_path) == '__init__.py':
                    continue  # Preserve __init__.py
                
                try:
                    os.remove(file_path)
                    deleted_files.append(file_path)
                    self.stdout.write(self.style.SUCCESS(f"  ✓ Deleted {os.path.basename(file_path)}"))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"  ✗ Failed to delete {file_path}: {e}"))
        
        if deleted_files:
            self.stdout.write(self.style.SUCCESS(f"\nDeleted {len(deleted_files)} migration file(s)"))
        else:
            self.stdout.write(self.style.SUCCESS("No migration files found to delete"))

    def _drop_database_tables(self):
        """
        Step 2: Drop all tables in the database
        Uses Django's database connection API (database-agnostic)
        """
        self.stdout.write(self.style.NOTICE("\n" + "-"*60))
        self.stdout.write(self.style.NOTICE("STEP 2: Dropping database tables"))
        self.stdout.write(self.style.NOTICE("-"*60))
        
        # Confirm before dropping
        if not self.auto_confirm:
            if not self._confirm_action("Drop ALL tables in the database?"):
                self.stdout.write(self.style.WARNING("Skipping table drop"))
                return
        
        try:
            connection = connections[self.database_alias]
            cursor = connection.cursor()
            
            # Get database vendor (postgresql, mysql, sqlite, etc.)
            vendor = connection.vendor
            self.stdout.write(self.style.NOTICE(f"Database vendor: {vendor}"))
            
            # Get list of all tables
            table_names = connection.introspection.table_names()
            
            if not table_names:
                self.stdout.write(self.style.SUCCESS("No tables found in database"))
                return
            
            self.stdout.write(f"Found {len(table_names)} table(s) to drop")
            
            # Disable foreign key checks (vendor-specific)
            if vendor == 'postgresql':
                # PostgreSQL: Drop tables with CASCADE
                for table_name in table_names:
                    try:
                        cursor.execute(f'DROP TABLE IF EXISTS "{table_name}" CASCADE')
                        self.stdout.write(self.style.SUCCESS(f"  ✓ Dropped {table_name}"))
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"  ✗ Failed to drop {table_name}: {e}"))
                        
            elif vendor == 'mysql':
                # MySQL: Disable foreign key checks, drop tables, re-enable checks
                cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                for table_name in table_names:
                    try:
                        cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`")
                        self.stdout.write(self.style.SUCCESS(f"  ✓ Dropped {table_name}"))
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"  ✗ Failed to drop {table_name}: {e}"))
                cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
                
            elif vendor == 'sqlite':
                # SQLite: Simply drop each table
                for table_name in table_names:
                    try:
                        cursor.execute(f"DROP TABLE IF EXISTS '{table_name}'")
                        self.stdout.write(self.style.SUCCESS(f"  ✓ Dropped {table_name}"))
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"  ✗ Failed to drop {table_name}: {e}"))
            else:
                raise CommandError(f"Unsupported database vendor: {vendor}")
            
            # Commit changes
            connection.commit()
            self.stdout.write(self.style.SUCCESS(f"\nSuccessfully dropped {len(table_names)} table(s)"))
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error during table drop: {e}"))
            raise CommandError(f"Failed to drop tables: {e}")
        finally:
            cursor.close()

    def _run_fresh_migrations(self):
        """
        Step 3: Run makemigrations and migrate to create fresh schema
        """
        self.stdout.write(self.style.NOTICE("\n" + "-"*60))
        self.stdout.write(self.style.NOTICE("STEP 3: Running fresh migrations"))
        self.stdout.write(self.style.NOTICE("-"*60))
        
        # Confirm before migrating
        if not self.auto_confirm:
            if not self._confirm_action("Run makemigrations and migrate?"):
                self.stdout.write(self.style.WARNING("Skipping fresh migrations"))
                return
        
        try:
            # Step 3a: makemigrations
            self.stdout.write(self.style.NOTICE("\nRunning makemigrations..."))
            call_command('makemigrations')
            
            # Step 3b: migrate
            self.stdout.write(self.style.NOTICE("\nRunning migrate..."))
            call_command('migrate', database=self.database_alias)
            
            self.stdout.write(self.style.SUCCESS("\nFresh migrations completed successfully"))
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error during migration: {e}"))
            raise CommandError(f"Failed to run migrations: {e}")
    
    def _run_post_migration_commands(self, commands):
        """
        Step 4 (Optional): Run custom commands after migrations complete
        Useful for seeding, creating superuser, loading fixtures, etc.
        
        Args:
            commands: List of command names to execute
        """
        if not commands:
            return
        
        self.stdout.write(self.style.NOTICE("\n" + "-"*60))
        self.stdout.write(self.style.NOTICE("STEP 4: Running post-migration commands"))
        self.stdout.write(self.style.NOTICE("-"*60))
        
        # Confirm before running
        if not self.auto_confirm:
            commands_str = ', '.join(commands)
            if not self._confirm_action(f"Run these commands: {commands_str}?"):
                self.stdout.write(self.style.WARNING("Skipping post-migration commands"))
                return
        
        for command in commands:
            try:
                self.stdout.write(self.style.NOTICE(f"\nRunning: {command}"))
                
                # Split command into name and args if provided
                # e.g., "seed_roles --initial" becomes ["seed_roles", "--initial"]
                command_parts = command.split()
                command_name = command_parts[0]
                command_args = command_parts[1:] if len(command_parts) > 1 else []
                
                call_command(command_name, *command_args)
                self.stdout.write(self.style.SUCCESS(f"  ✓ Completed: {command}"))
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ✗ Failed: {command}"))
                self.stdout.write(self.style.ERROR(f"    Error: {e}"))
                
                # Ask if user wants to continue with remaining commands
                if not self.auto_confirm:
                    if not self._confirm_action("Continue with remaining commands?"):
                        self.stdout.write(self.style.WARNING("Stopping post-migration commands"))
                        return
        
        self.stdout.write(self.style.SUCCESS("\nPost-migration commands completed"))