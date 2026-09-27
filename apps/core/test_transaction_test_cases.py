"""Every ``TransactionTestCase`` in this project restores the migration seed.

A ``TransactionTestCase`` empties the database when each test ends. What it does
next depends on ``serialized_rollback``:

- With it, the flush skips ``post_migrate`` and the class reloads the snapshot
  of migration-seeded rows (the codex platform tenant, content types,
  permissions) that the runner took before the suite started.
- Without it, the flush runs ``post_migrate``, which recreates content types
  and permissions straight away, and leaves the migration-seeded rows missing.

The two cannot sit in one suite. When a class without the flag runs first, it
leaves content types behind, and the next class with the flag fails in
``setUpClass`` loading its snapshot over them: a duplicate key on
``django_content_type``. The error lands on the innocent class, and on every
serialized class after it, never on the one that caused it.

This module imports every test module under ``apps/`` and names each
``TransactionTestCase`` subclass (``TestCase`` excluded, since it rolls back
rather than flushing) that leaves ``serialized_rollback`` off.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from django.test import SimpleTestCase, TestCase, TransactionTestCase

APPS_DIR = Path(__file__).resolve().parent.parent

SKIPPED_DIR_NAMES = frozenset({"migrations", "__pycache__", "static", "media"})


def _test_module_names() -> list[str]:
    """Dotted names of the modules the runner discovers under ``apps/``."""
    names = []
    for path in sorted(APPS_DIR.rglob("test*.py")):
        relative = path.relative_to(APPS_DIR)
        if SKIPPED_DIR_NAMES.intersection(relative.parts):
            continue
        names.append(".".join(relative.with_suffix("").parts))
    return names


def _defined_in_apps(cls: type) -> bool:
    """True for a class written in this project, not one a library ships."""
    source = getattr(sys.modules.get(cls.__module__), "__file__", None)
    return source is not None and Path(source).resolve().is_relative_to(APPS_DIR)


def _subclasses(cls: type) -> set[type]:
    found = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= _subclasses(sub)
    return found


class TransactionTestCasesRestoreTheSeedTests(SimpleTestCase):
    def test_every_transaction_test_case_sets_serialized_rollback(self):
        for name in _test_module_names():
            importlib.import_module(name)

        offenders = sorted(
            f"{cls.__module__}.{cls.__qualname__}"
            for cls in _subclasses(TransactionTestCase)
            if not issubclass(cls, TestCase)
            and _defined_in_apps(cls)
            and not cls.serialized_rollback
        )
        self.assertEqual(
            offenders, [],
            "These TransactionTestCase classes flush without restoring the "
            "migration seed. Set serialized_rollback = True on each.",
        )
