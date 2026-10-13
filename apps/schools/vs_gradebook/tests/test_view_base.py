"""What every gradebook view reads from the request: the term and the branch."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from rest_framework.exceptions import NotFound
from rest_framework.test import APIRequestFactory

from ..views.base import GradebookViewMixin
from .base import GradebookFixture, school_clock


class _Probe(GradebookViewMixin):
    def __init__(self, user, tenant, query):
        request = APIRequestFactory().get("/", query)
        self.request = SimpleNamespace(
            query_params=request.GET, user=user, tenant=tenant,
        )


class TermFromQueryTests(GradebookFixture):
    def probe(self, query=None, tenant=None, user=None):
        return _Probe(user or self.admin, tenant or self.tenant, query or {})

    def test_no_term_named_is_the_current_term_by_date(self):
        self.assertEqual(self.probe().term_from_query(), self.first_term)
        with school_clock(dt.datetime(2026, 1, 20, 12, 0, tzinfo=dt.timezone.utc)):
            self.assertEqual(self.probe().term_from_query(), self.second_term)

    def test_a_term_named_is_that_term(self):
        found = self.probe({"term": self.second_term.pk}).term_from_query()
        self.assertEqual(found, self.second_term)

    def test_another_schools_term_and_nonsense_are_a_404(self):
        for raw in (self.solo_term.pk, 999999, "abc", "-1", "9" * 30):
            with self.subTest(term=raw), self.assertRaises(NotFound):
                self.probe({"term": raw}).term_from_query()

    def test_a_school_with_no_year_has_no_current_term(self):
        from vs_rbac.tests.helpers import make_school

        empty = make_school(slug="empty", name="Empty").tenant
        with self.assertRaises(NotFound):
            self.probe(tenant=empty).term_from_query()


class BranchFromQueryTests(GradebookFixture):
    def test_no_branch_named_is_every_branch(self):
        self.assertIsNone(_Probe(self.admin, self.tenant, {}).branch_from_query())

    def test_a_branch_the_caller_can_use(self):
        probe = _Probe(self.admin, self.tenant, {"branch": self.ikeja.pk})
        self.assertEqual(probe.branch_from_query(), self.ikeja)

    def test_another_schools_branch_and_nonsense_are_a_404(self):
        for raw in (self.solo_branch.pk, "abc", 999999):
            with self.subTest(branch=raw), self.assertRaises(NotFound):
                _Probe(self.admin, self.tenant, {"branch": raw}).branch_from_query()
