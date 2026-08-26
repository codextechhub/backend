"""Every paginated list is ordered.

Django drops a model's Meta.ordering the moment a queryset is annotated, and
every list in this module annotates a count. An unordered queryset cannot be
paged: Postgres is free to return rows in any order it likes, so the same
department can land on page 1 and page 2 while another lands on neither.

Asserted on the querysets rather than by paging, because paging would pass
whenever Postgres happened to return them in a helpful order - which it usually
does, right up until the table grows.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from schools.vs_academics.views.classes import _classes_for, _subjects_for
from schools.vs_academics.views.sessions import _sessions_for
from schools.vs_academics.views.structure import (
    _departments_for,
    _levels_for,
    _programs_for,
)


class ListOrderingTests(SimpleTestCase):
    def test_every_list_queryset_is_ordered(self):
        for label, qs in (
            ("departments", _departments_for(tenant=None)),
            ("departments in a year", _departments_for(tenant=None, session=1)),
            ("programmes", _programs_for(tenant=None)),
            ("levels", _levels_for(tenant=None)),
            ("classes", _classes_for(tenant=None)),
            ("subjects", _subjects_for(tenant=None)),
            ("sessions", _sessions_for(tenant=None)),
        ):
            self.assertTrue(
                qs.ordered,
                f"the {label} list is not ordered, so it cannot be paged",
            )
