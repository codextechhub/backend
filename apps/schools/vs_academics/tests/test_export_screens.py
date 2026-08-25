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
        # A typed Unmapped, not a bare string: resolve_screen reads `.param`,
        # and it carries its own reason so the drawer has a sentence to show.
        self.assertEqual([u.param for u in unmapped], ["branch"])
        self.assertIn("every branch you can see", unmapped[0].reason)

    def test_a_level_filter_is_reported_rather_than_guessed(self):
        # The screen filters by level id; the dataset filters on level NAME, and
        # a translator has no tenant to resolve one into the other.
        _, unmapped = _translate_classes({"level": "44"})
        self.assertIn("level", [u.param for u in unmapped])

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

    def test_a_value_the_translator_cannot_read_is_reported(self):
        """The trap the platform's own suite catches, and it is subtle.

        A param listed in `handles` that the translator silently ignores is
        counted as CARRIED by resolve_screen - so the reader is told their
        filter was applied when nothing was. "all" is different: that is the
        screen not filtering, so there is nothing to carry and nothing to say.
        """
        filters, unmapped = _translate_subjects({"is_core": "maybe"})
        self.assertEqual(filters, [])
        self.assertEqual([u.param for u in unmapped], ["is_core"])

        filters, unmapped = _translate_subjects({"is_core": "all"})
        self.assertEqual(filters, [])
        self.assertEqual(unmapped, [])

    def test_a_session_status_the_export_does_not_know_is_reported(self):
        _, unmapped = _translate_sessions({"status": "CLOSED"})
        self.assertEqual([u.param for u in unmapped], ["status"])

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


class EndToEndTests(SimpleTestCase):
    def test_resolve_screen_accepts_what_these_translators_return(self):
        """The shape, not just the values.

        `resolve_screen` reads `.param` off every unmapped entry. Returning bare
        strings type-checks fine, passes every unit test that inspects the list,
        and 500s the endpoint - which is exactly what it did.
        """
        from vs_exports.catalogue import get_screen, resolve_screen

        resolved = resolve_screen(
            get_screen("academics.classes"),
            {"branch": "17", "level": "44", "is_active": "true"},
        )
        self.assertFalse(resolved["exact"])
        self.assertEqual(
            sorted(u["param"] for u in resolved["unmapped"]), ["branch", "level"],
        )
        self.assertIn({"id": "is_active", "value": True}, resolved["filters"])

    def test_a_screen_with_nothing_dropped_is_exact(self):
        from vs_exports.catalogue import get_screen, resolve_screen

        resolved = resolve_screen(
            get_screen("academics.subjects"), {"is_core": "true"},
        )
        self.assertTrue(resolved["exact"])
        self.assertEqual(resolved["unmapped"], [])
