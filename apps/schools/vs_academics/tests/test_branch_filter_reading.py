"""Filtering by a branch includes what the whole school shares.

The bug this pins: `?branch=<id>` filtered on equality, so it returned rows
belonging to that branch and NOTHING ELSE. Most of a catalogue is school-wide -
that is the normal way a school writes one - so picking a branch emptied the
screen, and the reading was wrong in a way that looked like missing data rather
than a wrong filter.

A null branch does not mean "unassigned". It means EVERY branch. The tree, the
overview and the export datasets all read it that way already; the five list
endpoints were the odd ones out, and they share one `_filtered`, so they were
wrong together and are right together.
"""
from __future__ import annotations

from .test_duplicate_messages import _AllAcademics
from schools.vs_academics.models import Level, SchoolClass, Subject


class InclusiveBranchFilterTests(_AllAcademics):
    def test_a_branch_filter_keeps_the_school_wide_rows(self):
        self.dept("Sciences", "SCI")                       # school-wide
        self.dept("General Studies", "GST", branch=self.ikeja)
        self.dept("Lekki Only", "LKO", branch=self.lekki)

        response = self.get(
            self.admin, "academics-department-list", {"branch": self.ikeja.pk},
        )
        names = sorted(d["name"] for d in response.data["data"])
        # Sciences belongs to Ikeja too - that is what school-wide MEANS.
        self.assertEqual(names, ["General Studies", "Sciences"])

    def test_another_branchs_rows_are_still_excluded(self):
        """Inclusive is not "show everything" - the filter still filters."""
        self.dept("Sciences", "SCI")
        self.dept("Lekki Only", "LKO", branch=self.lekki)

        response = self.get(
            self.admin, "academics-department-list", {"branch": self.ikeja.pk},
        )
        names = sorted(d["name"] for d in response.data["data"])
        self.assertEqual(names, ["Sciences"])

    def test_shared_asks_for_the_school_wide_rows_alone(self):
        # The one exclusive reading, and it is asked for by name.
        self.dept("Sciences", "SCI")
        self.dept("General Studies", "GST", branch=self.ikeja)

        response = self.get(
            self.admin, "academics-department-list", {"branch": "shared"},
        )
        names = sorted(d["name"] for d in response.data["data"])
        self.assertEqual(names, ["Sciences"])

    def test_the_same_reading_reaches_programmes_levels_classes_subjects(self):
        """One `_filtered`, so all five move together - or all five drift."""
        program = self.program("Junior Secondary", "JSS")
        level = Level.all_objects.create(
            tenant=self.tenant, program=program, name="JSS1", code="JSS1",
            order_index=1,
        )
        SchoolClass.all_objects.create(
            tenant=self.tenant, level=level, name="JSS1 A", code="JSS1-A",
        )
        Subject.all_objects.create(tenant=self.tenant, name="Maths", code="MTH")

        for route in (
            "academics-program-list",
            "academics-class-list",
            "academics-subject-list",
        ):
            response = self.get(self.admin, route, {"branch": self.ikeja.pk})
            self.assertEqual(response.status_code, 200, route)
            self.assertTrue(response.data["data"], f"{route} came back empty")

        levels = self.get(
            self.admin, "academics-level-list",
            {"branch": self.ikeja.pk}, pk=program.pk,
        )
        self.assertTrue(levels.data["data"], "levels came back empty")

    def test_a_branch_that_is_not_this_school_s_is_refused(self):
        """Inclusive is about NULL, not about accepting any id.

        A branch reference that resolves to nothing is still an error - widening
        the reading must not turn a bad request into a silent full-table read.
        """
        self.dept("Sciences", "SCI")
        response = self.get(
            self.admin, "academics-department-list", {"branch": "99999"},
        )
        self.assertEqual(response.status_code, 400, response.data)


class OfferedAtAnswersToTheFilterTests(_AllAcademics):
    """A count shown under a filter has to answer to it.

    The offerings prefetch was named `visible_offerings` and was not narrowed at
    all, so a subject card under a branch lens reported every level the subject
    is taught at - including levels that branch does not have. The row filter was
    right and the number beside it was not, which is the harder kind of wrong to
    notice.
    """

    def setUp(self):
        super().setUp()
        from schools.vs_academics.models import SubjectOffering

        shared_prog = self.program("Junior Secondary", "JSS")
        ikeja_prog = self.program("Vocational", "VOC", branch=self.ikeja)
        self.shared_level = Level.all_objects.create(
            tenant=self.tenant, program=shared_prog, name="JSS1", code="JSS1",
            order_index=1,
        )
        self.ikeja_level = Level.all_objects.create(
            tenant=self.tenant, program=ikeja_prog, name="Vocational 1",
            code="VOC1", order_index=1, branch=self.ikeja,
        )
        self.subject = Subject.all_objects.create(
            tenant=self.tenant, name="English", code="ENG",
        )
        for level in (self.shared_level, self.ikeja_level):
            SubjectOffering.all_objects.create(
                tenant=self.tenant, subject=self.subject, level=level,
            )

    def counts_at(self, params):
        response = self.get(self.admin, "academics-subject-list", params)
        row = next(s for s in response.data["data"] if s["name"] == "English")
        return row["level_count"], row["offered_label"]

    def test_unfiltered_counts_every_level_it_is_taught_at(self):
        count, label = self.counts_at({})
        self.assertEqual(count, 2)
        self.assertIn("Vocational 1", label)

    def test_a_branch_lens_drops_the_levels_that_branch_does_not_have(self):
        # Lekki has JSS1 (school-wide) and not Vocational 1 (Ikeja's).
        count, label = self.counts_at({"branch": self.lekki.pk})
        self.assertEqual(count, 1)
        self.assertNotIn("Vocational", label)

    def test_the_branch_that_owns_the_level_still_counts_it(self):
        count, _ = self.counts_at({"branch": self.ikeja.pk})
        self.assertEqual(count, 2)
