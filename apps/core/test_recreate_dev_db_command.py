"""``recreate_dev_db``: the refusals run before anything is dropped.

The connection is a stand-in, so no test here touches a database: what is
checked is which statements would run, and that none do when the command
refuses.
"""
import os
from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings


def _connection(name="cx_disposable"):
    cursor = Mock()

    @contextmanager
    def nodb_cursor():
        yield cursor

    connection = SimpleNamespace(
        settings_dict={"NAME": name, "HOST": "localhost"},
        vendor="postgresql",
        ops=SimpleNamespace(quote_name=lambda value: f'"{value}"'),
        close=Mock(),
        _nodb_cursor=nodb_cursor,
    )
    return connection, cursor


@override_settings(DEBUG=True, RESET_DB_ALLOWED_DATABASES={"cx_disposable"})
class RecreateDevDatabaseTests(SimpleTestCase):
    def _run(self, connection, *args, settings_module="apps.settings.local"):
        with (
            patch.dict(os.environ, {"DJANGO_SETTINGS_MODULE": settings_module}),
            patch(
                "core.management.commands.reset_db.connections",
                {"default": connection},
            ),
        ):
            call_command("recreate_dev_db", *args, stdout=StringIO())

    def test_recreates_the_configured_database_on_the_maintenance_connection(self):
        connection, cursor = _connection()

        self._run(connection, "--noinput")

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("pg_terminate_backend", statements[0])
        self.assertEqual(cursor.execute.call_args_list[0].args[1], ["cx_disposable"])
        self.assertEqual(statements[1:], [
            'DROP DATABASE IF EXISTS "cx_disposable"',
            'CREATE DATABASE "cx_disposable"',
        ])

    def test_refuses_a_database_outside_the_allowlist_before_asking(self):
        connection, cursor = _connection(name="cx_live_copy")

        with patch("builtins.input") as prompt:
            with self.assertRaisesMessage(CommandError, "not allowlisted"):
                self._run(connection)

        prompt.assert_not_called()
        cursor.execute.assert_not_called()

    def test_refuses_outside_the_development_and_test_settings(self):
        connection, cursor = _connection()

        with self.assertRaisesMessage(CommandError, "Refusing to run outside"):
            self._run(connection, "--noinput", settings_module="apps.settings.staging")

        cursor.execute.assert_not_called()

    def test_a_mistyped_name_drops_nothing(self):
        connection, cursor = _connection()

        with patch("builtins.input", return_value="cx_db"):
            with self.assertRaisesMessage(CommandError, "did not match"):
                self._run(connection)

        cursor.execute.assert_not_called()
