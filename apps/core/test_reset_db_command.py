import os
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from core.management.commands.reset_db import Command


class ResetDatabaseSafetyTests(SimpleTestCase):
    def setUp(self):
        self.connection = SimpleNamespace(
            settings_dict={"NAME": "cx_disposable", "HOST": "localhost"},
            vendor="postgresql",
        )

    def _options(self, **overrides):
        options = {
            "database": "default",
            "yes": True,
            "skip_drop": True,
            "skip_migrate": True,
            "post_commands": [],
        }
        options.update(overrides)
        return options

    def _command(self):
        return Command(stdout=StringIO(), stderr=StringIO())

    @override_settings(DEBUG=True, RESET_DB_ALLOWED_DATABASES={"cx_disposable"})
    def test_refuses_unapproved_settings_even_when_debug_and_yes_are_enabled(self):
        command = self._command()

        with (
            patch.dict(
                os.environ,
                {"DJANGO_SETTINGS_MODULE": "apps.settings.staging"},
            ),
            patch(
                "core.management.commands.reset_db.connections",
                {"default": self.connection},
            ),
        ):
            with self.assertRaisesMessage(
                CommandError,
                "Refusing to run outside the development and test settings modules",
            ):
                command.handle(**self._options())

    @override_settings(DEBUG=False, RESET_DB_ALLOWED_DATABASES={"cx_disposable"})
    def test_refuses_debug_false_local_settings(self):
        command = self._command()

        with (
            patch.dict(
                os.environ,
                {"DJANGO_SETTINGS_MODULE": "apps.settings.local"},
            ),
            patch(
                "core.management.commands.reset_db.connections",
                {"default": self.connection},
            ),
        ):
            with self.assertRaisesMessage(CommandError, "DEBUG=False"):
                command.handle(**self._options())

    @override_settings(DEBUG=False, RESET_DB_ALLOWED_DATABASES={"cx_ci"})
    def test_allows_ci_as_an_approved_test_environment(self):
        connection = SimpleNamespace(
            settings_dict={"NAME": "cx_ci", "HOST": "127.0.0.1"},
            vendor="postgresql",
        )
        command = self._command()

        with (
            patch.dict(
                os.environ,
                {"DJANGO_SETTINGS_MODULE": "apps.settings.ci"},
            ),
            patch(
                "core.management.commands.reset_db.connections",
                {"default": connection},
            ),
            patch("builtins.input", return_value="cx_ci"),
            patch.object(command, "_drop_database_tables") as drop_tables,
            patch.object(command, "_run_fresh_migrations") as run_migrations,
        ):
            command.handle(
                **self._options(skip_drop=False, skip_migrate=False)
            )

        drop_tables.assert_called_once_with()
        run_migrations.assert_called_once_with()

    @override_settings(DEBUG=True, RESET_DB_ALLOWED_DATABASES={"cx_disposable"})
    def test_refuses_database_name_outside_allowlist_before_prompting(self):
        connection = SimpleNamespace(
            settings_dict={"NAME": "xvs_production", "HOST": "prod-db"},
            vendor="postgresql",
        )
        command = self._command()

        with (
            patch.dict(
                os.environ,
                {"DJANGO_SETTINGS_MODULE": "apps.settings.local"},
            ),
            patch(
                "core.management.commands.reset_db.connections",
                {"default": connection},
            ),
            patch("builtins.input") as prompt,
        ):
            with self.assertRaisesMessage(CommandError, "is not allowlisted"):
                command.handle(**self._options())

        prompt.assert_not_called()

    @override_settings(DEBUG=True, RESET_DB_ALLOWED_DATABASES={"cx_disposable"})
    def test_yes_does_not_bypass_exact_database_name_confirmation(self):
        command = self._command()

        with (
            patch.dict(
                os.environ,
                {"DJANGO_SETTINGS_MODULE": "apps.settings.local"},
            ),
            patch(
                "core.management.commands.reset_db.connections",
                {"default": self.connection},
            ),
            patch("builtins.input", return_value="default"),
            patch.object(command, "_drop_database_tables") as drop_tables,
        ):
            with self.assertRaisesMessage(
                CommandError,
                "Database-name confirmation did not match",
            ):
                command.handle(**self._options())

        drop_tables.assert_not_called()

    @override_settings(DEBUG=True, RESET_DB_ALLOWED_DATABASES={"cx_disposable"})
    def test_names_resolved_target_and_runs_after_exact_confirmation(self):
        command = self._command()

        with (
            patch.dict(
                os.environ,
                {"DJANGO_SETTINGS_MODULE": "apps.settings.local"},
            ),
            patch(
                "core.management.commands.reset_db.connections",
                {"default": self.connection},
            ),
            patch("builtins.input", return_value="cx_disposable") as prompt,
            patch.object(command, "_drop_database_tables") as drop_tables,
            patch.object(command, "_run_fresh_migrations") as run_migrations,
        ):
            command.handle(
                **self._options(skip_drop=False, skip_migrate=False)
            )

        output = command.stdout.getvalue()
        self.assertIn("Database name: cx_disposable", output)
        self.assertIn("Database host: localhost", output)
        self.assertIn("cx_disposable", prompt.call_args.args[0])
        drop_tables.assert_called_once_with()
        run_migrations.assert_called_once_with()

    def test_connection_failure_preserves_the_real_error(self):
        connection = SimpleNamespace(
            vendor="postgresql",
            introspection=SimpleNamespace(table_names=lambda: ["tenant"]),
            cursor=Mock(side_effect=RuntimeError("connection unavailable")),
        )
        command = self._command()
        command.auto_confirm = True
        command.connection = connection
        command.database_name = "cx_disposable"

        with self.assertRaisesMessage(CommandError, "connection unavailable"):
            command._drop_database_tables()

    def test_render_build_removes_reset_command_from_deployed_artifact(self):
        build_script = Path(settings.BASE_DIR).parent / "build.sh"
        content = build_script.read_text(encoding="utf-8")

        self.assertIn('if [[ "${RENDER:-}" == "true" ]]', content)
        self.assertIn(
            "rm -f core/management/commands/reset_db.py",
            content,
        )
