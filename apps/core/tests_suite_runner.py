"""The test runner's own guarantees: no outbound network, a sound template key,
and template housekeeping that is safe beside other runs.

The template database is only as trustworthy as its fingerprint: a migration
that imports a project module must have that module's source in the digest,
or a change to it would leave every run cloning a schema built by the old code.
"""
import io
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from core.suite_runner import (
    REFUSAL_LOG_ENV,
    GuardedParallelTestSuite,
    NetworkAccessBlocked,
    NetworkGuard,
    XvsTestRunner,
    _database_exists,
    _imported_project_files,
    _is_local,
    _prune_templates,
    migration_fingerprint,
    read_refusal_log,
    refused_connections,
)


class ParallelBufferingTests(SimpleTestCase):
    def test_class_setup_error_without_active_output_buffer_is_reported(self):
        result = unittest.TextTestRunner(stream=io.StringIO(), buffer=True, verbosity=2)._makeResult()
        result.buffer = True
        suite = GuardedParallelTestSuite([], processes=2)
        error = RuntimeError("class setup failed")
        event = ("addError", -1, "setUpClass (broken.Case)", (RuntimeError, error, None))

        with io.TextIOWrapper(io.BytesIO()) as terminal:
            with mock.patch("sys.stdout", terminal), mock.patch("sys.stderr", terminal):
                suite.handle_event(result, [], event)

        self.assertEqual(len(result.errors), 1)
        self.assertIn("class setup failed", result.errors[0][1])
        self.assertTrue(result.buffer)


class LocalAddressTests(SimpleTestCase):
    def test_loopback_and_localhost_names_are_local(self):
        for address in (
            ("127.0.0.1", 5432), ("127.0.1.1", 80), ("::1", 6379, 0, 0),
            ("localhost", 8000), ("corona.localhost", 5174), "/tmp/.s.PGSQL.5432",
        ):
            with self.subTest(address=address):
                self.assertTrue(_is_local(address))

    def test_any_other_host_is_not(self):
        for address in (("203.0.113.7", 443), ("api.paystack.co", 443), ("10.0.0.5", 25)):
            with self.subTest(address=address):
                self.assertFalse(_is_local(address))


@unittest.skipIf(
    os.environ.get("XVS_TEST_ALLOW_NETWORK") == "1",
    "the network guard is deliberately lifted for this run",
)
class NetworkGuardTests(SimpleTestCase):
    def test_an_outbound_connection_is_refused_before_it_leaves(self):
        # 203.0.113.0/24 is TEST-NET-3: never routable, so nothing is reached
        # even if the guard were missing and the refusal came from elsewhere.
        with self.assertRaises(NetworkAccessBlocked):
            socket.create_connection(("203.0.113.7", 443), timeout=0.1)

    def test_the_refusal_is_a_connection_error_code_already_handles(self):
        self.assertTrue(issubclass(NetworkAccessBlocked, ConnectionRefusedError))


class RefusalReportingTests(SimpleTestCase):
    """A refusal is recorded, shared with the parent run and fails the result.

    These call the guard's own methods on a private instance, so no socket is
    opened and the run's real guard records nothing.
    """

    PUBLIC = ("198.51.100.9", 443)  # TEST-NET-2: not exempt, never routable
    TEST_NET_3 = ("203.0.113.7", 443)

    def setUp(self):
        handle, self.log = tempfile.mkstemp(suffix=".jsonl")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.log) and os.remove(self.log))

    def test_a_refusal_is_recorded_with_where_it_came_from(self):
        guard = NetworkGuard(log_path=self.log)
        with self.assertRaises(NetworkAccessBlocked):
            guard.check(self.PUBLIC)
        [(address, caller)] = refused_connections(guard)
        self.assertEqual(address, repr(self.PUBLIC))
        self.assertIn("tests_suite_runner.py", caller)

    def test_a_local_address_is_not_a_refusal(self):
        guard = NetworkGuard(log_path=self.log)
        guard.check(("127.0.0.1", 5432))
        self.assertEqual(guard.blocked, [])
        self.assertEqual(read_refusal_log(self.log), [])

    def test_the_test_net_3_range_is_refused_but_never_reported(self):
        guard = NetworkGuard(log_path=self.log)
        with self.assertRaises(NetworkAccessBlocked):
            guard.check(self.TEST_NET_3)
        self.assertEqual(len(guard.blocked), 1)
        self.assertEqual(refused_connections(guard, self.log), [])

    def test_a_worker_refusal_written_to_the_shared_log_reaches_the_parent(self):
        parent = NetworkGuard(log_path=self.log)
        with mock.patch.dict(os.environ, {REFUSAL_LOG_ENV: self.log}):
            worker = NetworkGuard()  # a spawned worker learns the path from its environment
        self.assertEqual(worker.log_path, self.log)
        with self.assertRaises(NetworkAccessBlocked):
            worker.check(self.PUBLIC)
        self.assertEqual(parent.blocked, [])
        [(address, _caller)] = refused_connections(parent, self.log)
        self.assertEqual(address, repr(self.PUBLIC))

    def test_the_same_refusal_seen_by_parent_and_worker_is_listed_once(self):
        guard = NetworkGuard(log_path=self.log)  # a forked worker shares the parent's guard and path
        with self.assertRaises(NetworkAccessBlocked):
            guard.check(self.PUBLIC)
        self.assertEqual(len(read_refusal_log(self.log)), 1)
        self.assertEqual(len(refused_connections(guard, self.log)), 1)

    def test_a_damaged_log_line_does_not_hide_the_others(self):
        guard = NetworkGuard(log_path=self.log)
        with self.assertRaises(NetworkAccessBlocked):
            guard.check(self.PUBLIC)
        with open(self.log, "a", encoding="utf-8") as log:
            log.write("not json\n{}\n")
        self.assertEqual(len(read_refusal_log(self.log)), 1)
        self.assertEqual(read_refusal_log(self.log + ".missing"), [])

    def test_an_unwritable_log_does_not_hide_the_refusal(self):
        guard = NetworkGuard(log_path="/nonexistent-directory/refusals.jsonl")
        with self.assertRaises(NetworkAccessBlocked):
            guard.check(self.PUBLIC)
        self.assertEqual(len(guard.blocked), 1)

    @unittest.skipIf(
        os.environ.get("XVS_TEST_ALLOW_NETWORK") == "1",
        "the network guard is deliberately lifted for this run",
    )
    def test_the_runner_reports_a_refusal_and_fails_the_result(self):
        runner = XvsTestRunner(verbosity=0)
        before = os.environ.get(REFUSAL_LOG_ENV)
        with mock.patch("core.suite_runner.DiscoverRunner.setup_test_environment"), \
                mock.patch("core.suite_runner.DiscoverRunner.teardown_test_environment"), \
                mock.patch.object(NetworkGuard, "install"), \
                mock.patch.object(NetworkGuard, "uninstall"), \
                mock.patch("core.suite_runner.DiscoverRunner.suite_result", return_value=0):
            runner.setup_test_environment()
            log = runner._refusal_log
            self.assertEqual(os.environ[REFUSAL_LOG_ENV], log)
            with self.assertRaises(NetworkAccessBlocked):
                runner._network_guard.check(self.PUBLIC)
            with mock.patch.dict(os.environ, {REFUSAL_LOG_ENV: log}):
                with self.assertRaises(NetworkAccessBlocked):
                    NetworkGuard().check(("198.51.100.10", 25))  # a worker
            with mock.patch("builtins.print") as printed:
                runner.teardown_test_environment()
            self.assertEqual(
                {address for address, _ in runner.refused},
                {repr(self.PUBLIC), repr(("198.51.100.10", 25))},
            )
            self.assertFalse(os.path.exists(log))
            self.assertEqual(os.environ.get(REFUSAL_LOG_ENV), before)
            report = "\n".join(str(call.args[0]) for call in printed.call_args_list)
            self.assertIn("198.51.100.10", report)
            self.assertEqual(runner.suite_result(None, None), 1)

    def test_a_run_with_no_refusal_keeps_its_own_result(self):
        runner = XvsTestRunner(verbosity=0)
        with mock.patch("core.suite_runner.DiscoverRunner.suite_result", return_value=3):
            self.assertEqual(runner.suite_result(None, None), 3)


class MigrationFingerprintTests(SimpleTestCase):
    def test_the_fingerprint_is_stable_between_calls(self):
        self.assertEqual(migration_fingerprint(), migration_fingerprint())

    def test_a_project_module_imported_by_a_migration_is_part_of_the_key(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "books").mkdir()
            (root / "books" / "money.py").write_text("KOBO = 100\n")
            (root / "books" / "rules").mkdir()
            (root / "books" / "rules" / "__init__.py").write_text("")
            source = (
                "import books.money\n"
                "def forwards(apps, schema_editor):\n"
                "    from books.rules import something\n"
                "from django.db import migrations\n"
            )
            found = _imported_project_files(source, root)
        self.assertEqual(
            {path.relative_to(root).as_posix() for path in found},
            {"books/money.py", "books/rules/__init__.py"},
        )


class TemplatePruningTests(SimpleTestCase):
    """Pruning old templates never costs a run its own session or another's template.

    Under ``--parallel`` every worker that builds a rewind template prunes the
    old ones, each under the advisory lock of its own template, so two workers
    could prune the same old template at once. PostgreSQL marks a database
    invalid while ``DROP DATABASE`` is under way, and ``ALTER DATABASE`` on an
    invalid database is a FATAL error, which ends the session. The pruner's
    session was the one the worker went on to clone with, so its next statement
    failed with "the connection is lost" and the class errored in
    ``setUpClass``.

    These tests make real databases on the local server, so they hold no test
    transaction, and every database they make is dropped again.
    """

    databases = {"default"}
    PREFIX = "xvs_test_prunecheck_"

    def setUp(self):
        from django.db import connection

        self.qn = connection.ops.quote_name
        self.prefix = f"{self.PREFIX}{os.getpid()}_"
        self.names = []
        with connection._nodb_cursor() as cursor:
            cursor.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
            if not cursor.fetchone()[0]:
                self.skipTest("marking a database invalid needs a superuser")

    def tearDown(self):
        from django.db import connection

        with connection._nodb_cursor() as cursor:
            for name in self.names:
                # An invalid template can be neither altered nor dropped as it is.
                cursor.execute(
                    "UPDATE pg_database SET datistemplate = false WHERE datname = %s",
                    [name],
                )
                cursor.execute(f"DROP DATABASE IF EXISTS {self.qn(name)}")

    def make_template(self, suffix, *, invalid=False):
        from django.db import connection

        name = self.prefix + suffix
        self.names.append(name)
        with connection._nodb_cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {self.qn(name)}")
            cursor.execute(
                f"ALTER DATABASE {self.qn(name)} "
                "WITH IS_TEMPLATE true ALLOW_CONNECTIONS false"
            )
            if invalid:
                # What a DROP DATABASE under way, or interrupted, leaves behind.
                cursor.execute(
                    "UPDATE pg_database SET datconnlimit = -2 WHERE datname = %s",
                    [name],
                )
        return name

    def exists(self, name):
        from django.db import connection

        with connection._nodb_cursor() as cursor:
            return _database_exists(cursor, name)

    def test_an_invalid_template_is_skipped_and_the_callers_session_survives(self):
        """The worker's session is still open after pruning beside a DROP under way."""
        from django.db import connection

        invalid = self.make_template("invalid", invalid=True)
        spare = self.make_template("spare")
        with connection._nodb_cursor() as cursor:
            _prune_templates(self.qn, prefix=self.prefix, kept=0)
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone()[0], 1)
        self.assertTrue(self.exists(invalid))
        self.assertFalse(self.exists(spare))

    def test_a_template_another_session_holds_is_left_alone(self):
        """A template being built, cloned or pruned elsewhere is under its lock."""
        from django.db import connection

        held = self.make_template("held")
        spare = self.make_template("spare")
        with connection._nodb_cursor() as other:
            other.execute("SELECT pg_advisory_lock(hashtext(%s))", [held])
            try:
                _prune_templates(self.qn, prefix=self.prefix, kept=0)
            finally:
                other.execute("SELECT pg_advisory_unlock(hashtext(%s))", [held])
        self.assertTrue(self.exists(held))
        self.assertFalse(self.exists(spare))
