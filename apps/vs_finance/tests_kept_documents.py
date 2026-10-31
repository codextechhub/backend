"""The database itself refuses to delete or un-post a kept document.

``tests_record_retention`` proves every ORM path refuses a kept row. These
tests go round the ORM with plain SQL, the way a mistaken console command or a
raw query would, and meet the triggers of ``vs_finance`` 0078 and
``vs_procurement`` 0049.
"""
from __future__ import annotations

import datetime

from django.apps import apps
from django.db import DatabaseError, connection, transaction

from .models import JournalEntry, JournalLine
from .models.core import FinanceDocument
from .tests_record_retention import _Books

LINE_TABLES = ("vs_finance_journalline", "vs_finance_invoiceline")


def _sql(statement, *params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


class KeptDocumentTriggerTests(_Books):
    def entry(self, *, draft=False):
        return self.post(
            self.ikeja, datetime.date(2026, 3, 10),
            [("1100", 50_000_00, 0), ("4100", 0, 50_000_00)], draft=draft,
        )

    def assert_refused(self, statement, *params, says="kept record"):
        with self.assertRaises(DatabaseError) as caught, transaction.atomic():
            _sql(statement, *params)
        self.assertIn(says, str(caught.exception))

    def test_sql_cannot_delete_a_posted_journal(self):
        entry = self.entry()
        self.assert_refused("DELETE FROM vs_finance_journalentry WHERE id = %s", entry.pk)
        self.assertTrue(JournalEntry.objects.filter(pk=entry.pk).exists())

    def test_sql_cannot_delete_the_lines_of_a_posted_journal(self):
        entry = self.entry()
        self.assert_refused(
            "DELETE FROM vs_finance_journalline WHERE entry_id = %s", entry.pk,
            says="cannot be deleted",
        )
        self.assertEqual(JournalLine.objects.filter(entry=entry).count(), 2)

    def test_sql_cannot_move_a_posted_journal_back_to_draft(self):
        entry = self.entry()
        for status in ("DRAFT", "PENDING_APPROVAL", "APPROVED"):
            with self.subTest(status=status):
                self.assert_refused(
                    "UPDATE vs_finance_journalentry SET status = %s WHERE id = %s",
                    status, entry.pk, says="cannot go back",
                )
        _sql("UPDATE vs_finance_journalentry SET status = 'REVERSED' WHERE id = %s", entry.pk)

    def test_a_draft_and_its_lines_can_still_be_deleted(self):
        draft = self.entry(draft=True)
        _sql("DELETE FROM vs_finance_journalline WHERE entry_id = %s", draft.pk)
        _sql("DELETE FROM vs_finance_journalentry WHERE id = %s", draft.pk)
        self.assertFalse(JournalEntry.objects.filter(pk=draft.pk).exists())

    def test_every_document_table_and_line_table_carries_its_triggers(self):
        """A new document table fails here until a migration gives it the triggers."""
        expected = set()
        for model in apps.get_models():
            if issubclass(model, FinanceDocument):
                table = model._meta.db_table
                expected |= {f"{table}_kept_no_delete", f"{table}_kept_no_unpost"}
        expected |= {f"{table}_kept_no_delete" for table in LINE_TABLES}
        with connection.cursor() as cursor:
            cursor.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE %s", ["%_kept_no_%"])
            present = {row[0] for row in cursor.fetchall()}
        self.assertEqual(sorted(expected - present), [])
