"""Export filters and sorts follow the reader's Field Access.

A filter on a column answers a question about it even when the column is left
out of the file: a reader who cannot see last names, searching for "Okafor",
learns from the row count that somebody by that name exists. The engine
therefore judges every filter against the person the export runs as
(:mod:`vs_exports.engine`): a search is narrowed to the columns they may read
and matches nothing when none is left, a single-column filter on a hidden
column is refused, and a sort on one is dropped.

The dataset here is a throwaway one over the user model, never registered, and
the reader's access is a predicate patched in, so the rules are proved without
a database and without any one module's registry.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.test import SimpleTestCase

from vs_exports import engine
from vs_exports.catalogue import (
    FILTER_CHOICE,
    FILTER_DATE_RANGE,
    FILTER_SEARCH,
    FILTER_TEXT,
    KIND_DATE,
    KIND_TEXT,
    Dataset,
    Field,
    FilterDef,
    ScopeContext,
    compile_filter,
    describe_filter,
)
from vs_exports.constants import DatasetScope, FailureCode

FIRST = "test.person.first_name"
LAST = "test.person.last_name"
JOINED = "test.person.date_joined"

SEARCH = FilterDef("search", "Search", FILTER_SEARCH, searches=(
    ("first_name", "First name"), ("last_name", "Last name"), ("email", "Email"),
))
JOINED_FILTER = FilterDef("joined", "Joined", FILTER_DATE_RANGE, source="created_at")
SURNAME_FILTER = FilterDef("last_name", "Surname", FILTER_TEXT)
STATUS_FILTER = FilterDef("status", "Status", FILTER_CHOICE, choices={"ACTIVE": "Active"})
EXPLICIT = FilterDef(
    "nickname", "Nickname", FILTER_TEXT, source="username", access=FIRST,
)


def _dataset():
    """People with three governed columns and one ungoverned one."""
    return Dataset(
        key="test.people", module="Test", name="People", description="",
        base=lambda scope: get_user_model().objects.all(),
        scope=DatasetScope.TENANT,
        fields=(
            Field("email", "Email", "Person", KIND_TEXT, locked=True),
            Field("first_name", "First name", "Person", KIND_TEXT, access=FIRST),
            Field("last_name", "Last name", "Person", KIND_TEXT, access=LAST),
            Field("created_at", "Joined", "Person", KIND_DATE, access=JOINED),
            Field("status", "Status", "Person", KIND_TEXT),
        ),
        filters=(SEARCH, JOINED_FILTER, SURNAME_FILTER, STATUS_FILTER, EXPLICIT),
    )


def _hiding(*hidden):
    """A Field Access predicate that reads everything except *hidden*."""
    return lambda key: key not in hidden


def _scope(*hidden):
    """A scope run as somebody, with the engine told they cannot read *hidden*."""
    tenant = SimpleNamespace(name="Bright Star School", pk=1)
    return ScopeContext(tenant=tenant, user=object()), _hiding(*hidden)


class GoverningFieldTests(SimpleTestCase):
    """A filter is governed by the column it reads, so an author cannot forget it."""

    def test_a_filter_takes_the_key_of_the_column_it_reads(self):
        dataset = _dataset()
        self.assertEqual(dataset.filter_access(SURNAME_FILTER), LAST)
        self.assertEqual(dataset.filter_access(JOINED_FILTER), JOINED)

    def test_a_filter_on_an_ungoverned_column_has_no_key(self):
        self.assertEqual(_dataset().filter_access(STATUS_FILTER), "")

    def test_an_explicit_key_governs_a_path_that_is_no_column(self):
        self.assertEqual(_dataset().filter_access(EXPLICIT), FIRST)

    def test_a_derived_column_needs_every_governing_key(self):
        dataset = Dataset(
            key="test.combined", module="Test", name="Combined", description="",
            base=lambda scope: None,
            fields=(Field("first_name", "Combined", "Person", KIND_TEXT,
                          access=(FIRST, LAST)),),
            filters=(FilterDef("first_name", "Combined", FILTER_TEXT),),
        )
        self.assertEqual(dataset.describe(readable=_hiding(LAST))["fields"], [])
        self.assertFalse(dataset.filter_readable(dataset.filters[0], _hiding(LAST)))
        self.assertEqual(len(dataset.describe(readable=_hiding())["fields"]), 1)


class SearchNarrowingTests(SimpleTestCase):
    """A search matches only the columns its reader may read."""

    def compile(self, readable, value="Okafor"):
        return compile_filter(
            _dataset(), {"id": "search", "value": value}, readable=readable,
        )

    def test_a_hidden_column_is_not_searched(self):
        self.assertEqual(
            self.compile(_hiding(LAST)),
            Q(first_name__icontains="Okafor") | Q(email__icontains="Okafor"),
        )

    def test_a_reader_who_may_read_every_column_searches_them_all(self):
        self.assertEqual(
            self.compile(_hiding()),
            Q(first_name__icontains="Okafor") | Q(last_name__icontains="Okafor")
            | Q(email__icontains="Okafor"),
        )

    def test_nobody_to_judge_searches_every_column(self):
        self.assertEqual(self.compile(None), self.compile(_hiding()))

    def test_no_readable_column_matches_nothing(self):
        dataset = Dataset(
            key="test.names", module="Test", name="Names", description="",
            base=lambda scope: None,
            fields=(Field("last_name", "Last name", "Person", KIND_TEXT, access=LAST),),
            filters=(FilterDef("search", "Search", FILTER_SEARCH,
                               searches=(("last_name", "Last name"),)),),
        )
        compiled = compile_filter(
            dataset, {"id": "search", "value": "Okafor"}, readable=_hiding(LAST),
        )
        self.assertEqual(compiled, Q(pk__in=[]))

    def test_a_path_named_on_the_filter_is_governed_without_a_column_key(self):
        dataset = Dataset(
            key="test.names", module="Test", name="Names", description="",
            base=lambda scope: None,
            fields=(Field("last_name", "Last name", "Person", KIND_TEXT),
                    Field("email", "Email", "Person", KIND_TEXT)),
            filters=(FilterDef("search", "Search", FILTER_SEARCH,
                               searches=(("last_name", "Last name"), ("email", "Email")),
                               access_by_path={"last_name": LAST}),),
        )
        compiled = compile_filter(
            dataset, {"id": "search", "value": "Okafor"}, readable=_hiding(LAST),
        )
        self.assertEqual(compiled, Q() | Q(email__icontains="Okafor"))

    def test_an_empty_search_still_narrows_nothing(self):
        self.assertEqual(self.compile(_hiding(LAST), value=""), Q())

    def test_the_sentence_names_only_the_columns_searched(self):
        sentence = describe_filter(
            _dataset(), {"id": "search", "value": "Okafor"}, readable=_hiding(LAST),
        )
        self.assertEqual(sentence, "First name, Email mentions “Okafor”")


class SingleColumnRefusalTests(SimpleTestCase):
    """A filter on one hidden column is refused, never dropped and never emptied."""

    def test_a_hidden_column_filter_with_a_value_is_refused(self):
        with self.assertRaises(engine.FilterReadDenied) as caught:
            engine.assert_filters_readable(
                _dataset(), [
                    {"id": "joined", "start": "2026-01-01", "end": "2026-03-31"},
                    {"id": "last_name", "value": "Oka"},
                ], _hiding(LAST, JOINED),
            )
        self.assertEqual(caught.exception.code, FailureCode.FILTER_FORBIDDEN)
        self.assertEqual(
            caught.exception.filters, {"joined": "Joined", "last_name": "Surname"},
        )
        self.assertIn("Joined and Surname", caught.exception.message)

    def test_an_explicitly_governed_filter_is_refused_too(self):
        with self.assertRaises(engine.FilterReadDenied):
            engine.assert_filters_readable(
                _dataset(), [{"id": "nickname", "value": "Oka"}], _hiding(FIRST),
            )

    def test_a_blank_filter_narrows_nothing_and_passes(self):
        engine.assert_filters_readable(
            _dataset(),
            [{"id": "joined", "start": "", "end": None}, {"id": "last_name", "value": ""}],
            _hiding(LAST, JOINED),
        )

    def test_readable_and_ungoverned_filters_pass(self):
        engine.assert_filters_readable(
            _dataset(),
            [{"id": "status", "values": ["ACTIVE"]}, {"id": "search", "value": "Oka"},
             {"id": "last_name", "value": "Oka"}],
            _hiding(JOINED, FIRST),
        )

    def test_build_queryset_refuses_before_reading(self):
        scope, readable = _scope(JOINED)
        with mock.patch.object(engine, "readable_fields", return_value=readable):
            with self.assertRaises(engine.FilterReadDenied):
                engine.build_queryset(
                    _dataset(), scope,
                    [{"id": "joined", "start": "2026-01-01", "end": "2026-03-31"}],
                )

    def test_a_scope_with_nobody_in_it_is_not_judged(self):
        scope = ScopeContext(tenant=SimpleNamespace(name="Bright Star School"))
        qs = engine.build_queryset(
            _dataset(), scope,
            [{"id": "joined", "start": "2026-01-01", "end": "2026-03-31"}],
        )
        self.assertIn("created_at", str(qs.query.where))


class SortTests(SimpleTestCase):
    """A sort on a hidden column would rank rows by the hidden value."""

    def test_a_hidden_column_does_not_order_the_rows(self):
        scope, readable = _scope(LAST)
        with mock.patch.object(engine, "readable_fields", return_value=readable):
            qs = engine.build_queryset(
                _dataset(), scope, [],
                sort=[{"field": "last_name"}, {"field": "first_name", "direction": "desc"}],
            )
        self.assertEqual(qs.query.order_by, ("-first_name", "pk"))


class WhatIsReadBackTests(SimpleTestCase):
    """The Filters sheet, the review sentence and the builder's catalogue."""

    CONFIG = {"filters": [
        {"id": "joined", "start": "2026-01-01", "end": "2026-03-31"},
        {"id": "search", "value": "Okafor"},
    ]}

    def setUp(self):
        # A school's date format is read from its settings; ISO is enough here.
        patcher = mock.patch(
            "vs_exports.catalogue.format_date", side_effect=lambda value, tenant: str(value),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def summary(self, *hidden):
        scope, readable = _scope(*hidden)
        with mock.patch.object(engine, "readable_fields", return_value=readable):
            return engine.filters_summary(_dataset(), self.CONFIG, scope)

    def test_the_filters_sheet_leaves_out_a_hidden_filter(self):
        filters = [value for label, value in self.summary(JOINED, LAST) if label == "Filter"]
        self.assertEqual(filters, ["First name, Email mentions “Okafor”"])

    def test_the_filters_sheet_is_unchanged_for_a_default_reader(self):
        filters = [value for label, value in self.summary() if label == "Filter"]
        self.assertEqual(len(filters), 2)
        self.assertEqual(filters[1], "First name, Last name, Email mentions “Okafor”")

    def test_the_review_sentence_leaves_it_out_too(self):
        scope, readable = _scope(JOINED, LAST)
        with mock.patch.object(engine, "readable_fields", return_value=readable):
            sentence = engine.plain_sentence(_dataset(), self.CONFIG, scope)
        self.assertNotIn("Joined", sentence)
        self.assertNotIn("Last name", sentence)

    def test_the_catalogue_offers_neither_the_filter_nor_the_hidden_search_column(self):
        described = _dataset().describe(readable=_hiding(JOINED, LAST))
        filters = {f["id"]: f for f in described["filters"]}
        self.assertNotIn("joined", filters)
        self.assertNotIn("last_name", filters)
        self.assertEqual(filters["search"]["searches"], ["First name", "Email"])

    def test_a_search_with_no_readable_column_is_not_offered(self):
        described = _dataset().describe(readable=_hiding(FIRST, LAST))
        self.assertEqual(
            {f["id"]: f["searches"] for f in described["filters"]}["search"], ["Email"],
        )
        dataset = Dataset(
            key="test.names", module="Test", name="Names", description="",
            base=lambda scope: None,
            fields=(Field("last_name", "Last name", "Person", KIND_TEXT, access=LAST),),
            filters=(FilterDef("search", "Search", FILTER_SEARCH,
                               searches=(("last_name", "Last name"),)),),
        )
        self.assertEqual(dataset.describe(readable=_hiding(LAST))["filters"], [])


class FilterAccessKeysAreRegisteredTests(SimpleTestCase):
    """Every key a published filter names is a real Field Access field.

    A misspelt key reads as an unregistered field, which is open to everyone,
    so a typo would quietly switch the filter gate off. Column keys are held
    by ``vs_rbac.tests.test_field_registry``.
    """

    def test_every_named_key_is_declared(self):
        from vs_exports.catalogue import all_datasets
        from vs_rbac.field_registry import all_declarations

        declared = {
            declaration.key_for(spec)
            for declaration in all_declarations() for spec in declaration.fields
        }
        for dataset in all_datasets():
            named = [f.access for f in dataset.filters if f.access]
            named += [key for f in dataset.filters for key in f.access_by_path.values()]
            for key in named:
                with self.subTest(dataset=dataset.key, key=key):
                    self.assertIn(key, declared)
