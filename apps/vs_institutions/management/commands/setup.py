import os
import sys
import subprocess
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = 'Sets up a new development environment for the staff (creates venv, installs requirements, and runs setup tasks)'

    def handle(self, *args, **kwargs):
        # Define the virtual environment name and paths, ensure it's created in the root folder
        venv_name = 'cx'
        venv_path = os.path.join(os.getcwd()[:len(os.getcwd())-10], venv_name)

        # Change directory
        self.stdout.write(self.style.NOTICE(f"Changing Directory: CodeX"))
        os.chdir(os.getcwd()[:len(os.getcwd())-10])

        # Check if the virtual environment already exists
        if not os.path.exists(venv_path):
            self.stdout.write(self.style.NOTICE(f"Creating virtual environment: {venv_name}"))
            subprocess.run(['python', '-m', 'venv', venv_name])
            self.stdout.write(self.style.NOTICE(f"Virtual Environment Created -> {venv_name}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Virtual environment '{venv_name}' already exists."))

        # Activate the virtual environment and install the requirements
        self.stdout.write(self.style.NOTICE("Installing dependencies from requirements.txt..."))
        self._install_requirements()

        # Run other base commands like db creation
        self.stdout.write(self.style.NOTICE("Running base commands (e.g., database creation)..."))
        self._run_base_commands()

        self.stdout.write(self.style.SUCCESS("Development environment setup completed successfully."))

        # Activation instructions
        if os.name == 'nt':
            activate_cmd = f"{venv_name}\Scripts\\activate"
        else:
            activate_cmd = f"source {venv_name}/bin/activate"

        self.stdout.write(self.style.WARNING(f"\nTo activate your virtual environment, run:\n  {activate_cmd}\n"))

    def _install_requirements(self):
        """Install dependencies from requirements.txt."""
        if os.name == 'nt':  # Windows
            pip_path = os.path.join(os.getcwd(), 'cx', 'Scripts', 'pip')
        else:  # macOS / Linux
            pip_path = os.path.join(os.getcwd(), 'cx', 'bin', 'pip')

        pip_command = [pip_path, 'install', '-r', os.path.join(os.getcwd(), 'requirements.txt')]
        subprocess.run(pip_command, check=True)
        self.stdout.write(self.style.SUCCESS(f"\nRequirments setup completed successfully."))

        # Change back directory
        self.stdout.write(self.style.NOTICE(f"Changing Directory: codex_hub"))
        os.chdir(os.path.join(os.getcwd(), "codex_hub"))

    def _run_base_commands(self):
        """Run base management commands like database creation."""
        # Run database creation and any other necessary commands
        subprocess.run([sys.executable, 'manage.py', 'create_db'], check=True)
        subprocess.run([sys.executable, 'manage.py', 'migrate_products_only'], check=True)
        # subprocess.run(['python', 'manage.py', 'createsuperuser'], check=True)  # Optional: creates a superuser
