"""The student directory's "export what this table is showing" binding.

The contract's rule is what these assert: a filter the dataset cannot carry must
be REPORTED, not dropped. Silently widening a file is the outcome `unmapped`
exists to prevent - somebody asks for JSS1 A and gets the whole school, with
nothing on screen to tell them.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from schools.vs_students.export_datasets import _translate_directory


class DirectoryTranslationTests(SimpleTestCase):
    def test_a_search_is_carried(self):
        filters, unmapped = _translate_directory({"search": "Chiamaka"})
        self.assertEqual(filters, [{"id": "search", "value": "Chiamaka"}])
        self.assertEqual(unmapped, [])

    def test_a_status_is_carried(self):
        filters, unmapped = _translate_directory({"status": "ACTIVE"})
        self.assertEqual(filters, [{"id": "status", "value": ["ACTIVE"]}])
        self.assertEqual(unmapped, [])

    def test_all_means_do_not_filter(self):
        """"All statuses" is the screen NOT filtering, not a filter called all."""
        self.assertEqual(_translate_directory({"status": "all"})[0], [])
        self.assertEqual(_translate_directory({"class": "all"})[1], [])

    def test_the_branch_lens_is_carried_here_unlike_the_catalogue_screens(self):
        """A student belongs to ONE branch, so the lens narrows exactly.

        The academics screens report the same lens as unmapped, and the
        difference is real rather than an inconsistency: a catalogue row can be
        school-wide, and "this branch plus the shared ones" is an OR no
        FilterDef expresses. A student is never school-wide.
        """
        filters, unmapped = _translate_directory(
            {"branch_name": "Lagoon View Academy Annex"},
        )
        self.assertEqual(
            filters,
            [{"id": "branch__name", "value": "Lagoon View Academy Annex"}],
        )
        self.assertEqual(unmapped, [])

    def test_a_branch_id_alone_is_reported_rather_than_guessed(self):
        """Resolving an id would need a tenant, which a translator has not got."""
        filters, unmapped = _translate_directory({"branch": "22"})
        self.assertEqual(filters, [])
        self.assertEqual([u.param for u in unmapped], ["branch"])

    def test_a_class_filter_is_reported_because_the_dataset_has_no_class(self):
        """The dangerous one: the table shows 26 children, the file would show 90."""
        filters, unmapped = _translate_directory({"class": "82"})
        self.assertEqual(filters, [])
        self.assertEqual([u.param for u in unmapped], ["class"])

    def test_a_level_filter_is_reported_too(self):
        _, unmapped = _translate_directory({"level": "4"})
        self.assertEqual([u.param for u in unmapped], ["level"])

    def test_everything_at_once(self):
        filters, unmapped = _translate_directory({
            "search": "Okafor", "status": "ACTIVE", "class": "82",
            "level": "4", "branch_name": "Main", "page": "3",
        })
        self.assertEqual(
            filters,
            [
                {"id": "search", "value": "Okafor"},
                {"id": "status", "value": ["ACTIVE"]},
                {"id": "branch__name", "value": "Main"},
            ],
        )
        # A page is not a narrowing, so it is neither carried nor reported.
        self.assertEqual(sorted(u.param for u in unmapped), ["class", "level"])


class BindingTests(SimpleTestCase):
    def test_the_screen_is_registered_against_the_students_dataset(self):
        """Without this the directory's Export button has nothing behind it."""
        from vs_exports.catalogue import get_screen

        binding = get_screen("students.directory")
        self.assertIsNotNone(binding)
        self.assertEqual(binding.dataset_key, "school.students")
