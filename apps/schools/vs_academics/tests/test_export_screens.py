"""The Export Centre's "export what this table is showing" bindings.

The contract's own rule is what these assert: a filter the dataset cannot carry
must be REPORTED, not dropped. Silently widening a file is the outcome the
`unmapped` field exists to prevent - somebody asks for one branch's classes,
gets every branch's, and has nothing on screen to tell them.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from schools.vs_academics.export_datasets import (
    _translate_catalogue,
    _translate_classes,
    _translate_sessions,
    _translate_subjects,
)


class TranslationTests(SimpleTestCase):
    def test_a_search_is_carried(self):
        filters, unmapped = _translate_catalogue({"search": "Sciences"})
        self.assertEqual(filters, [{"id": "search", "value": "Sciences"}])
        self.assertEqual(unmapped, [])

    def test_all_means_do_not_filter(self):
        """"All statuses" is the screen NOT filtering, not a filter called all."""
        filters, _ = _translate_catalogue({"is_active": "all"})
        self.assertEqual(filters, [])

    def test_active_and_archived_are_both_carried(self):
        self.assertEqual(
            _translate_catalogue({"is_active": "true"})[0],
            [{"id": "is_active", "value": True}],
        )
        # False, not absent: "show me the archived ones" is a real filter, and
        # dropping it would export the active ones instead.
        self.assertEqual(
            _translate_catalogue({"is_active": "false"})[0],
            [{"id": "is_active", "value": False}],
        )

    def test_the_branch_lens_is_reported_rather_than_carried(self):
        """The lens is "school-wide PLUS this branch", an OR no FilterDef says.

        Carrying it as a name match would drop every school-wide row - most of a
        catalogue - and hand back a file NARROWER than the screen. So it is
        named as unmapped, which is what turns `exact` false.
        """
        filters, unmapped = _translate_catalogue({"branch": "17"})
        self.assertEqual(filters, [])
        self.assertEqual(unmapped, ["branch"])

    def test_a_level_filter_is_reported_rather_than_guessed(self):
        # The screen filters by level id; the dataset filters on level NAME, and
        # a translator has no tenant to resolve one into the other.
        _, unmapped = _translate_classes({"level": "44"})
        self.assertIn("level", unmapped)

    def test_core_and_elective_are_carried(self):
        self.assertEqual(
            _translate_subjects({"is_core": "true"})[0],
            [{"id": "is_core", "value": True}],
        )
        self.assertEqual(
            _translate_subjects({"is_core": "false"})[0],
            [{"id": "is_core", "value": False}],
        )

    def test_a_session_status_is_carried_as_a_choice(self):
        filters, _ = _translate_sessions({"status": "ACTIVE"})
        self.assertEqual(filters, [{"id": "status", "value": ["ACTIVE"]}])

    def test_a_page_number_is_not_a_narrowing(self):
        # Listed in `ignore`, so it is neither carried nor reported as dropped.
        filters, unmapped = _translate_catalogue({"page": "2"})
        self.assertEqual(filters, [])
        self.assertEqual(unmapped, [])


class RegistrationTests(SimpleTestCase):
    def test_every_academics_screen_is_bound_to_a_published_dataset(self):
        """A binding naming an unpublished dataset fails at REQUEST time.

        It resolves to None inside the view and answers 404, which is why
        register_screens runs after register_datasets rather than beside it.
        """
        from vs_exports.catalogue import get_screen

        for key in (
            "academics.sessions", "academics.departments", "academics.programs",
            "academics.levels", "academics.classes", "academics.subjects",
        ):
            binding = get_screen(key)
            self.assertIsNotNone(binding, f"{key} is not registered")
            self.assertIsNotNone(binding.dataset, f"{key} names no live dataset")
