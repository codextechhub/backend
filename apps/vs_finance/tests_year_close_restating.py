"""The year-end close and re-open answers honour the net result switch.

Every line of a closing journal moves one profit-and-loss account into retained
earnings, so the journal's lines and totals are the year's net result seen
another way (``finance.fiscalyear.net_income``). A caller who may not read the
net result is served the journals' identity and state without them, in the
response to closing a year and in the response to re-opening it.

Lagoon View runs Ikeja and Lekki; Harbour Primary runs one branch.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.models import FiscalPeriod

from .tests_dashboard_field_access import _CloseFixture

JOURNAL_FIGURES = ("lines", "total_debit", "total_credit")


class YearCloseJournalAccessTests(_CloseFixture):

    def year_user(self, label, *fields, harbour=False):
        if harbour:
            return self.harbour_staff(label, *fields, resource="finance.fiscalyear")
        return self.lagoon_staff(label, *fields, resource="finance.fiscalyear")

    def close(self, user, books=None, **body):
        books = books or self.books
        FiscalPeriod.objects.filter(
            fiscal_year=self.year(books), is_closing=False,
        ).update(status="CLOSED")
        return TenantAPIClient(user=user).post(
            f"/v1/finance/fiscal-years/{self.year(books).pk}/close/?entity={books.code}",
            body, format="json",
        )

    def reopen(self, user, books=None, **body):
        books = books or self.books
        return TenantAPIClient(user=user).post(
            f"/v1/finance/fiscal-years/{self.year(books).pk}/reopen/?entity={books.code}",
            {"reason": "A late invoice", **body}, format="json",
        )

    def test_an_open_role_reads_the_closing_journal_lines(self):
        self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)

        response = self.close(self.year_user("open"), branch=self.ikeja.pk)

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertIn("net_income", data)
        for journal in (*data["closing_journals"], data["closing_journal"]):
            for name in JOURNAL_FIGURES:
                self.assertIn(name, journal)
        self.assertTrue(data["closing_journal"]["lines"])

    def test_a_hidden_net_result_takes_the_lines_and_totals_with_it(self):
        self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)

        response = self.close(self.year_user("netless", "net_income"), branch=self.ikeja.pk)

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertNotIn("net_income", data)
        self.assertEqual(len(data["closing_journals"]), 1)
        for journal in (*data["closing_journals"], data["closing_journal"]):
            for name in JOURNAL_FIGURES:
                self.assertNotIn(name, journal)
            self.assertTrue(journal["document_number"])
            self.assertEqual(journal["status"], "POSTED")

    def test_the_reopen_answer_follows_the_same_switch(self):
        self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)
        self.close(self.year_user("closer"), branch=self.ikeja.pk)

        hidden = self.reopen(self.year_user("netless-reopen", "net_income"), branch=self.ikeja.pk)

        self.assertEqual(hidden.status_code, 200, hidden.data)
        reversals = hidden.data["data"]["reversals"]
        self.assertTrue(reversals)
        for journal in reversals:
            for name in JOURNAL_FIGURES:
                self.assertNotIn(name, journal)
            self.assertTrue(journal["document_number"])

    def test_an_open_role_reads_the_reversal_lines(self):
        self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)
        self.close(self.year_user("closer-open"), branch=self.ikeja.pk)

        response = self.reopen(self.year_user("opener"), branch=self.ikeja.pk)

        self.assertEqual(response.status_code, 200, response.data)
        for journal in response.data["data"]["reversals"]:
            self.assertTrue(journal["lines"])

    def test_a_one_branch_school_follows_the_same_switch(self):
        books = self.harbour_books
        self.fees_in(books)

        hidden = self.close(self.year_user("solo-hidden", "net_income", harbour=True), books)

        self.assertEqual(hidden.status_code, 200, hidden.data)
        for journal in hidden.data["data"]["closing_journals"]:
            for name in JOURNAL_FIGURES:
                self.assertNotIn(name, journal)

    def fees_in(self, books):
        """Post one fee income in Harbour's only branch so its year has a result to close."""
        self.fees(self.harbour_main, datetime.date(2026, 1, 15), 100_000, books=books)
