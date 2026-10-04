"""The project's test runner: Django's, with a cached migrated schema and timings.

Migrated template database
--------------------------
Building the test database means replaying every migration, which costs
several minutes of CPU on this project (Django re-renders model state for
each of several hundred migrations; the SQL itself is a small part of it).
That price was paid again on every ``manage.py test`` invocation, so running
one small app took minutes before its first test started.

This runner pays it once per distinct set of migrations. The first run
migrates the test database as Django always does and then copies it into a
PostgreSQL template named after a fingerprint of the migrations. Every later
run with the same fingerprint creates its test database with
``CREATE DATABASE ... TEMPLATE``, which takes about a second.

The copy is exactly the schema a fresh migration produces, because it is one:
the fingerprint covers every migration file of every installed app, the
project modules those migrations import, the Django version and the user
model. Any change to one of those produces a new fingerprint and a fresh
migration. Nothing from an earlier run's tests survives either, since each run
starts from the pristine template rather than from the previous test database,
which is what makes this different from ``--keepdb``.

``--fresh-db`` (or ``XVS_TEST_DB_TEMPLATE=0``) skips the template and migrates
from scratch, for a run whose purpose is to prove the migrations replay.
``--keepdb`` keeps its usual meaning and bypasses the template. Sessions
sharing one PostgreSQL server share templates; an advisory lock makes a
second session wait for a template being built rather than build its own. The
lock is per template, so building one never holds up cloning another. A
run never replaces a test database another run has open: two runs given the
same ``DB_NAME`` stop the second with ``TestDatabaseInUse``.
Only the few most recent templates are kept.

Network guard
-------------
A test run may not open a socket to anything but this machine. Payment
providers, SMTP, health probes and every other outbound call are faked or
patched in the tests that exercise them, and the guard is what keeps a new test
from quietly depending on a live service: the attempt fails with
``NetworkAccessBlocked`` (a ``ConnectionRefusedError``, so code handling an
unreachable host behaves as it would in production) and the run ends with a
list of every destination that was refused. PostgreSQL is unaffected, since
libpq connects outside Python's ``socket`` module. ``XVS_TEST_ALLOW_NETWORK=1``
lifts the guard for a deliberate integration run. Under ``--parallel`` every
worker is guarded too, but a refusal inside a worker is reported by the test
it breaks rather than in the end-of-run list.

Output buffering
----------------
Output is buffered by default, as ``--buffer`` does: a passing test's prints
are discarded and a failing test's appear in its report. Many tests run seed
commands that print hundreds of lines, and without this a run's log is mostly
seeding chatter, which buries the failures and costs anybody (or any agent)
reading it. ``--no-buffer`` restores direct printing; ``--pdb`` implies it.

Timings
-------
Django's own ``--durations`` needs Python 3.12, and this project runs 3.11.
``--slowest N`` prints, after the run, the N slowest test methods and the N
slowest test classes. ``--timing-report PATH`` writes every measurement to a
JSON file, which is what a before-and-after comparison is built from.

A class is charged two things: the time its own tests take (``setUp``, the
test body and ``tearDown``) and its setup overhead, the gap between the
previous test finishing and its first test starting. That gap is where
``setUpClass`` and ``setUpTestData`` run. A class that builds its fixture once
shows it there; one that rebuilds it in ``setUp`` shows it spread across every
test instead. Timings are ignored under ``--parallel``, where results arrive
from worker processes after the fact and a wall-clock gap means nothing.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import sys
import time
import unittest
from collections import defaultdict
from importlib import import_module
from pathlib import Path

import django
from django.apps import apps
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.test.runner import DiscoverRunner, ParallelTestSuite, _init_worker

TEMPLATE_PREFIX = "xvs_test_tmpl_"
TEMPLATES_KEPT = 4


# ---------------------------------------------------------------------------
# Network guard
# ---------------------------------------------------------------------------
class NetworkAccessBlocked(ConnectionRefusedError):
    """A test tried to reach a host other than this machine."""


_LOCAL_HOSTS = {"localhost", "::1", "0.0.0.0", ""}


def _is_local(address):
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX paths and other non-inet addresses
    host = str(address[0]).lower()
    return host in _LOCAL_HOSTS or host.startswith("127.") or host.endswith(".localhost")


def _caller():
    """Where a refused connection came from: the project code, and the test.

    Code under test often catches a connection error and carries on, so the
    refusal can pass silently; naming the test is what makes it findable.
    """
    import traceback

    root = Path(settings.BASE_DIR)
    frames = [
        f for f in traceback.extract_stack()
        if f.filename.startswith(str(root)) and f.filename != __file__
    ]

    def label(frame):
        return f"{Path(frame.filename).relative_to(root)}:{frame.lineno}"

    if not frames:
        return "outside the project"
    tests = [f for f in frames if f.name.startswith("test")]
    code = label(frames[-1])
    return f"{code} (test {label(tests[-1])})" if tests and tests[-1] is not frames[-1] else code


class NetworkGuard:
    """Refuses outbound connections from ``socket.socket`` for the run."""

    def __init__(self):
        self.blocked = []
        self._originals = None

    def install(self):
        import socket

        guard = self
        connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

        def _check(sock, address):
            if not _is_local(address):
                guard.blocked.append((address, _caller()))
                raise NetworkAccessBlocked(
                    f"Test attempted a network connection to {address!r}. "
                    "Fake or patch the boundary (see core/suite_runner.py)."
                )

        def guarded_connect(sock, address):
            _check(sock, address)
            return connect(sock, address)

        def guarded_connect_ex(sock, address):
            _check(sock, address)
            return connect_ex(sock, address)

        guarded_connect.xvs_network_guard = True

        self._originals = (connect, connect_ex)
        socket.socket.connect = guarded_connect
        socket.socket.connect_ex = guarded_connect_ex

    def uninstall(self):
        import socket

        if self._originals:
            socket.socket.connect, socket.socket.connect_ex = self._originals
            self._originals = None


# ---------------------------------------------------------------------------
# Migration fingerprint
# ---------------------------------------------------------------------------
def _migration_dirs():
    """``(app name, migrations directory)`` for every installed app that has one."""
    dirs = []
    for config in apps.get_app_configs():
        try:
            module = import_module(f"{config.name}.migrations")
        except ImportError:
            continue
        dirs.extend((config.name, Path(p)) for p in getattr(module, "__path__", []))
    return sorted(dirs)


def _imported_project_files(source, project_root):
    """Project files a migration imports, at any depth inside the file."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        else:
            continue
        for name in names:
            base = project_root.joinpath(*name.split("."))
            for candidate in (base.with_suffix(".py"), base / "__init__.py"):
                if candidate.is_file():
                    found.add(candidate)
    return found


def migration_fingerprint():
    """A digest that changes whenever a fresh migration could come out differently.

    Only names relative to an app or to the project enter the digest, never an
    absolute path, so every checkout of the same code (a worktree, a CI runner)
    arrives at the same template.
    """
    project_root = Path(settings.BASE_DIR)
    digest = hashlib.sha256()
    digest.update(django.get_version().encode())
    digest.update(str(settings.AUTH_USER_MODEL).encode())
    imported = set()
    for app_name, directory in _migration_dirs():
        for path in sorted(directory.glob("*.py")):
            source = path.read_bytes()
            digest.update(f"{app_name}/{path.name}".encode())
            digest.update(source)
            if path.is_relative_to(project_root):
                imported |= _imported_project_files(source, project_root)
    for path in sorted(imported):
        digest.update(path.relative_to(project_root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Timings
# ---------------------------------------------------------------------------
def _guarded_init_worker(*args, **kwargs):
    """Django's worker setup, plus the network guard.

    A worker started by ``fork`` inherits the guard the parent installed; one
    started by ``spawn`` (the default on macOS) begins with a fresh ``socket``
    module and would otherwise run unguarded.
    """
    import socket

    _init_worker(*args, **kwargs)
    guarded = getattr(socket.socket.connect, "xvs_network_guard", False)
    if not guarded and os.environ.get("XVS_TEST_ALLOW_NETWORK") != "1":
        NetworkGuard().install()


class GuardedParallelTestSuite(ParallelTestSuite):
    """Guard workers and report fixture errors outside a test's output buffer.

    A class fixture may fail before ``startTest`` or after ``stopTest``. Django
    replays that worker error in the parent, where unittest's buffered result
    otherwise assumes stdout and stderr are StringIO objects.
    """

    init_worker = _guarded_init_worker

    def handle_event(self, result, tests, event):
        error_events = {"addError", "addFailure", "addSubTest", "addExpectedFailure"}
        has_buffer = hasattr(sys.stdout, "getvalue") and hasattr(sys.stderr, "getvalue")
        if event[0] not in error_events or not getattr(result, "buffer", False) or has_buffer:
            return super().handle_event(result, tests, event)

        result.buffer = False
        try:
            return super().handle_event(result, tests, event)
        finally:
            result.buffer = True


class TimingTextTestResult(unittest.TextTestResult):
    """A text result that records how long each test and each class took."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_times = {}
        self.class_setup = defaultdict(float)
        self._last_stop = None
        self._started = None

    def startTestRun(self):
        super().startTestRun()
        # The first class's setUpClass runs after this, so it is charged too.
        self._last_stop = time.perf_counter()

    def startTest(self, test):
        now = time.perf_counter()
        if self._last_stop is not None:
            self.class_setup[_class_label(test)] += now - self._last_stop
        self._started = now
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        now = time.perf_counter()
        if self._started is not None:
            self.test_times[test.id()] = now - self._started
        self._last_stop = now


def _class_label(test):
    return f"{type(test).__module__}.{type(test).__qualname__}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
class XvsTestRunner(DiscoverRunner):
    """``DiscoverRunner`` with a template test database and timing reports."""

    parallel_test_suite = GuardedParallelTestSuite

    def __init__(self, slowest=0, timing_report=None, fresh_db=False,
                 no_buffer=False, **kwargs):
        if not no_buffer and not kwargs.get("pdb"):
            kwargs["buffer"] = True
        super().__init__(**kwargs)
        self.slowest = slowest or 0
        self.timing_report = timing_report
        self.fresh_db = fresh_db
        self._timing_result = None
        self._network_guard = None

    @classmethod
    def add_arguments(cls, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--slowest", type=int, default=0, metavar="N",
            help="After the run, list the N slowest tests and test classes.",
        )
        parser.add_argument(
            "--timing-report", default=None, metavar="PATH",
            help="Write per-test and per-class timings to PATH as JSON.",
        )
        parser.add_argument(
            "--no-buffer", action="store_true",
            help="Let tests print straight to the terminal instead of keeping "
                 "their output for the report of a failing test.",
        )
        parser.add_argument(
            "--fresh-db", action="store_true",
            help="Migrate the test database from scratch instead of cloning "
                 "the cached migrated template.",
        )

    # -- network guard -----------------------------------------------------
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        if os.environ.get("XVS_TEST_ALLOW_NETWORK") != "1":
            self._network_guard = NetworkGuard()
            self._network_guard.install()

    def teardown_test_environment(self, **kwargs):
        guard, self._network_guard = self._network_guard, None
        if guard is not None:
            guard.uninstall()
            # TEST-NET-3 is what the guard's own tests aim at; nothing real lives there.
            blocked = {
                (repr(a), c) for a, c in guard.blocked
                if not str(a[0]).startswith("203.0.113.")
            }
            if blocked:
                print("\nNetwork connections refused during this run:")
                for address, caller in sorted(blocked):
                    print(f"  {address}  from {caller}")
        super().teardown_test_environment(**kwargs)

    # -- template database -------------------------------------------------
    def _template_enabled(self):
        if self.fresh_db or self.keepdb:
            return False
        if os.environ.get("XVS_TEST_DB_TEMPLATE", "1") == "0":
            return False
        return connections[DEFAULT_DB_ALIAS].vendor == "postgresql"

    def setup_databases(self, **kwargs):
        if DEFAULT_DB_ALIAS not in (kwargs.get("aliases") or ()):
            return super().setup_databases(**kwargs)
        if not self._template_enabled():
            return super().setup_databases(**kwargs)

        connection = connections[DEFAULT_DB_ALIAS]
        template = TEMPLATE_PREFIX + migration_fingerprint()
        test_name = connection.creation._get_test_db_name()
        qn = connection.ops.quote_name

        with connection._nodb_cursor() as lock:
            lock.execute("SELECT pg_advisory_lock(hashtext(%s))", [template])
            try:
                if _database_exists(lock, template):
                    if self.verbosity >= 1:
                        print(f"Cloning test database from template {template}...")
                    # Worker copies must come from this clone, never a previous run.
                    names = [test_name]
                    if self.parallel > 1:
                        names += [f"{test_name}_{i}" for i in range(1, self.parallel + 1)]
                    for name in names:
                        _drop_unused(lock, qn, name)
                    lock.execute(f"CREATE DATABASE {qn(test_name)} TEMPLATE {qn(template)}")
                    self.keepdb = True
                    try:
                        return super().setup_databases(**kwargs)
                    finally:
                        self.keepdb = False

                old_config = super().setup_databases(**kwargs)
                if self.parallel > 1:
                    # Worker copies already hold connections to the source.
                    return old_config
                for alias in connections:
                    connections[alias].close()
                try:
                    lock.execute(f"CREATE DATABASE {qn(template)} TEMPLATE {qn(test_name)}")
                    lock.execute(
                        f"ALTER DATABASE {qn(template)} "
                        "WITH IS_TEMPLATE true ALLOW_CONNECTIONS false"
                    )
                    _prune_templates(lock, qn)
                except Exception as exc:  # the run itself is unaffected
                    print(f"Could not save template {template}: {exc}")
                else:
                    if self.verbosity >= 1:
                        print(f"Saved migrated schema as template {template}.")
                return old_config
            finally:
                lock.execute("SELECT pg_advisory_unlock(hashtext(%s))", [template])

    # -- timings -----------------------------------------------------------
    def _timing_enabled(self):
        return (self.slowest or self.timing_report) and self.parallel <= 1

    def get_resultclass(self):
        base = super().get_resultclass()
        if not self._timing_enabled() or base is not None:
            return base
        runner = self

        class _Result(TimingTextTestResult):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                runner._timing_result = self

        return _Result

    def suite_result(self, suite, result, **kwargs):
        if self._timing_result is not None:
            self._report(self._timing_result)
        return super().suite_result(suite, result, **kwargs)

    def _report(self, result):
        per_class = defaultdict(lambda: {"tests": 0, "test_time": 0.0})
        for test_id, seconds in result.test_times.items():
            label = test_id.rsplit(".", 1)[0]
            per_class[label]["tests"] += 1
            per_class[label]["test_time"] += seconds
        for label, seconds in result.class_setup.items():
            per_class[label]["setup_time"] = seconds
        for row in per_class.values():
            row.setdefault("setup_time", 0.0)
            row["total"] = row["test_time"] + row["setup_time"]

        if self.timing_report:
            with open(self.timing_report, "w") as fh:
                json.dump({"tests": result.test_times, "classes": per_class}, fh, indent=1)

        if self.slowest:
            out = result.stream
            out.writeln(f"\nSlowest {self.slowest} tests:")
            for test_id, seconds in sorted(
                result.test_times.items(), key=lambda kv: -kv[1],
            )[: self.slowest]:
                out.writeln(f"  {seconds:8.3f}s  {test_id}")
            out.writeln(f"\nSlowest {self.slowest} test classes (tests + setup):")
            for label, row in sorted(
                per_class.items(), key=lambda kv: -kv[1]["total"],
            )[: self.slowest]:
                out.writeln(
                    f"  {row['total']:8.3f}s  ({row['tests']:3d} tests, "
                    f"setup {row['setup_time']:7.3f}s)  {label}"
                )


class TestDatabaseInUse(RuntimeError):
    """Another run is using the test database this run would replace."""


def _drop_unused(cursor, qn, name):
    """Drop a leftover test database, refusing one another run still has open.

    Two sessions given the same ``DB_NAME`` share a test database name. Django
    would stop the second at ``DROP DATABASE`` with "being accessed by other
    users"; this keeps that protection rather than forcing the first run's
    connections closed and failing every test it has left.
    """
    cursor.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = %s", [name],
    )
    if cursor.fetchone()[0]:
        raise TestDatabaseInUse(
            f"Test database {name!r} is in use by another run. Give this run "
            "its own DB_NAME."
        )
    cursor.execute(f"DROP DATABASE IF EXISTS {qn(name)}")


def _database_exists(cursor, name):
    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
    return cursor.fetchone() is not None


def _prune_templates(cursor, qn, prefix=TEMPLATE_PREFIX, kept=TEMPLATES_KEPT):
    """Drop all but the most recent templates, skipping any still in use."""
    cursor.execute(
        "SELECT datname FROM pg_database WHERE datname LIKE %s "
        "ORDER BY oid DESC OFFSET %s",
        [prefix + "%", kept],
    )
    for (name,) in cursor.fetchall():
        with contextlib.suppress(Exception):
            cursor.execute(f"ALTER DATABASE {qn(name)} WITH IS_TEMPLATE false")
            cursor.execute(f"DROP DATABASE {qn(name)}")
