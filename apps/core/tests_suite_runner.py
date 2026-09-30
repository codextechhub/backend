"""The test runner's own guarantees: no outbound network, and a sound template key.

The template database is only as trustworthy as its fingerprint: a migration
that imports a project module must have that module's source in the digest,
or a change to it would leave every run cloning a schema built by the old code.
"""
import os
import socket
import tempfile
import unittest
from pathlib import Path

from django.test import SimpleTestCase

from core.suite_runner import (
    NetworkAccessBlocked,
    _imported_project_files,
    _is_local,
    migration_fingerprint,
)


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
