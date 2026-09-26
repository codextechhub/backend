"""Testing a migration against the schema the database had before it.

A migration's data step can only be tested on the tables it was written for,
which are gone by the time the suite runs: the test database is built at the
latest migration. :class:`RewoundSchemaTestCase` rewinds one app to ``BEFORE``
once, when the class starts, inside the transaction :class:`TestCase` already
holds around the whole class. PostgreSQL rolls schema changes back like any
other write, so when the class ends the rollback restores every table, column,
index and ``django_migrations`` row the rewind touched. Nothing is replayed
forward and nothing is flushed.

That is the whole reason for the class. The pattern it replaces rewound the
schema in every test's ``setUp`` and replayed every leaf in ``tearDown``, under
``TransactionTestCase`` with ``serialized_rollback``. Rewinding an app unapplies
everything that depends on it, and each migration unapplied means re-rendering
the historical model state of the whole project, so the cost grew with every
later migration anybody wrote. Rewinding ``vs_user`` to 0008 had come to
unapply nineteen migrations across four apps, at about three minutes a test.

Inside a test, two moves are available, both within that test's savepoint,
whose rollback returns the schema to ``BEFORE`` for the next test:

* :meth:`migrate_to` does what ``manage.py migrate APP target`` does: forward,
  it applies the target and its ancestors; backward, it unapplies everything
  that depends on the migrations after the target. It is the move a test of one
  migration's effect wants, and the cheap one.
* :meth:`settle_at` brings the whole database to the state it is in when
  ``APP`` stands at the target: every migration in the project applied except
  those that depend on one of ``APP``'s migrations after it. That is where a
  rewind from the latest schema lands, and it is where the class starts. A
  test that seeds at a later point than ``BEFORE`` settles there first, or it
  starts from a database no deploy ever had: going forward from ``BEFORE``
  applies only the target's ancestors and leaves out the migrations beside it.

Two things differ from an ordinary :class:`TestCase`, both because schema
changes and row changes share one transaction here:

* Constraints are made ``IMMEDIATE`` at the start of every migration step.
  PostgreSQL refuses to ``ALTER`` a table with deferred foreign-key checks
  still pending from rows written earlier in the same transaction, and a
  foreign key a migration creates starts out deferred again. A real deploy
  commits between migrations, which fires those checks; this is the same
  boundary, drawn without the commit.
* A test that needs the latest schema, to go through the live models, calls
  :meth:`migrate_to_latest`. The live models declare every current column, so
  they fit no other schema.

Rows must be built through :attr:`historical` models for the rewound app and
for any app its rewind reached; only apps the rewind left alone keep their
latest tables.
"""
from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase


class RewoundSchemaTestCase(TestCase):
    """A ``TestCase`` whose tests run with ``APP`` rewound to ``BEFORE``."""

    APP = ""
    BEFORE = ""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not cls.BEFORE:
            return
        try:
            cls._settle(cls.BEFORE)
            cls.historical = cls.historical_apps(cls.BEFORE)
        except BaseException:
            cls.tearDownClass()
            raise

    @staticmethod
    def _immediate_constraints():
        """Fire pending foreign-key checks now, so a table can be altered."""
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    @classmethod
    def _executor(cls) -> MigrationExecutor:
        """An executor that fires pending constraint checks before each step."""
        def between_steps(action, *args, **kwargs):
            if action in ("apply_start", "unapply_start"):
                cls._immediate_constraints()

        executor = MigrationExecutor(connection, progress_callback=between_steps)
        executor.loader.build_graph()
        return executor

    @classmethod
    def historical_apps(cls, target):
        """The model registry as it stood at ``APP``'s *target* migration."""
        return cls._executor().loader.project_state((cls.APP, target)).apps

    @classmethod
    def _settle(cls, target):
        """Bring the database to the state it has when ``APP`` stands at *target*."""
        cls._immediate_constraints()
        executor = cls._executor()
        executor.migrate(cls._targets_for(executor.loader.graph, target))

    @classmethod
    def _targets_for(cls, graph, target):
        """Per app, the last migration kept when ``APP`` stands at *target*.

        Kept is every migration that does not depend on one of ``APP``'s
        migrations after *target*. An app with nothing kept is unapplied
        entirely, which ``migrate`` spells as a ``None`` target.
        """
        later = {
            node for node in graph.backwards_plan((cls.APP, target))
            if node[0] == cls.APP and node != (cls.APP, target)
        }
        dropped = set().union(*(graph.backwards_plan(node) for node in later))
        kept = set(graph.nodes) - dropped
        targets = []
        for app in sorted({node[0] for node in graph.nodes}):
            own = {node for node in kept if node[0] == app}
            if not own:
                targets.append((app, None))
                continue
            targets.extend(sorted(
                node for node in own
                if not any(child.key in own for child in graph.node_map[node].children)
            ))
        return targets

    def migrate_to(self, target):
        """Migrate ``APP`` to *target*, as ``manage.py migrate`` would, for this test."""
        self._immediate_constraints()
        self._executor().migrate([(self.APP, target)])

    def settle_at(self, target):
        """Bring the whole database to ``APP`` at *target*, for this test."""
        self._settle(target)

    def migrate_to_latest(self):
        """Bring every app to its latest migration for the rest of this test."""
        self._immediate_constraints()
        executor = self._executor()
        executor.migrate(executor.loader.graph.leaf_nodes())
