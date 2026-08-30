"""The scenario seeder, run twice.

Running it once proves it builds something. Running it twice proves it builds
the same thing, which is the property that matters: a seeder that is not
idempotent invents data every time somebody refreshes their environment, and
the extra rows are indistinguishable from rows a person created.
"""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from schools.vs_academics.models import AcademicSession
from schools.vs_students.constants import StudentStatus
from schools.vs_students.models import ClassEnrolment, Guardian, Student
from vs_rbac.tests.helpers import make_branch, make_school, make_school_admin


def _counts(tenant):
    return {
        "students": Student.all_objects.filter(tenant=tenant).count(),
        "guardians": Guardian.all_objects.filter(tenant=tenant).count(),
        "enrolments": ClassEnrolment.all_objects.filter(tenant=tenant).count(),
    }


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.multi = make_school(slug="brightfield-lekki", name="Brightfield Schools")
        make_branch(cls.multi, name="Lekki", is_main=True)
        make_branch(cls.multi, name="Ikeja", is_main=False)

        cls.solo = make_school(slug="st-monicas", name="St. Monica's Academy")
        make_branch(cls.solo, name="Main", is_main=True)

        cls.live = make_school(slug="holy-cross", name="Holy Cross College")
        make_branch(cls.live, name="Holy Cross Main", is_main=True)
        make_branch(cls.live, name="Holy Cross Annex", is_main=False)

        # The seeder attributes every enrolment to somebody, because the
        # services it drives write an actor onto every row they create.
        for index, school in enumerate((cls.multi, cls.solo, cls.live)):
            make_school_admin(
                None, email=f"seeder{index}@example.test", tenant=school.tenant,
            )

        call_command("seed_academic_scenarios", verbosity=0)

    def test_the_cast_and_this_fixture_name_the_same_schools(self):
        """A school added to CAST and not here fails EVERY test in this file.

        Asserting the pair keeps the next addition to a single, readable
        failure rather than three confusing ones.
        """
        from schools.vs_schools.models import School
        from schools.vs_students.management.commands.seed_student_scenarios import (
            CAST,
        )

        seeded = set(School.objects.values_list("slug", flat=True))
        self.assertEqual(
            sorted(set(CAST) - seeded), [],
            "these are in CAST but not built by this fixture",
        )

    def seed(self, only=None):
        out = StringIO()
        call_command(
            "seed_student_scenarios", stdout=out,
            **({"only": only} if only else {}),
        )
        return out.getvalue()


class IdempotenceTests(_Base):
    def test_it_builds_something_in_the_first_place(self):
        """Idempotence over an empty result would be trivially true."""
        self.seed()
        for school in (self.multi, self.solo, self.live):
            with self.subTest(school=school.slug):
                self.assertGreater(_counts(school.tenant)["students"], 0)

    def test_running_it_twice_changes_nothing(self):
        self.seed()
        first = {s.slug: _counts(s.tenant) for s in (self.multi, self.solo, self.live)}
        self.seed()
        self.assertEqual(
            {s.slug: _counts(s.tenant) for s in (self.multi, self.solo, self.live)},
            first,
        )


class ScenarioCoverageTests(_Base):
    def test_every_status_the_screens_show_has_a_row_behind_it(self):
        """A state with no row is a screen nobody can check."""
        self.seed()
        present = set(
            Student.all_objects.values_list("status", flat=True).distinct(),
        )
        for expected in (
            StudentStatus.ACTIVE, StudentStatus.ENROLLED,
            StudentStatus.APPLICANT, StudentStatus.SUSPENDED,
            StudentStatus.WITHDRAWN, StudentStatus.TRANSFERRED,
            StudentStatus.REJECTED,
        ):
            with self.subTest(status=expected):
                self.assertIn(expected, present)

    def test_a_guardian_stands_for_children_at_two_branches(self):
        """The case a school-level guardian exists for.

        A student-scoped guardian could not express it, and a seeder that never
        produced one would leave the Guardians screen looking correct against
        data that could not test it.
        """
        self.seed()
        shared = [
            g for g in Guardian.all_objects.prefetch_related(
                "student_links__student",
            )
            if len({link.student.branch_id for link in g.student_links.all()}) > 1
        ]
        self.assertTrue(shared)

    def test_at_least_one_student_is_on_the_roll_with_no_class(self):
        """So Classes and transfers has something to place."""
        self.seed()
        self.assertTrue(
            Student.all_objects.filter(
                status=StudentStatus.ENROLLED,
            ).exclude(enrolments__is_active=True).exists(),
        )

    def test_the_single_branch_school_puts_every_child_at_its_only_branch(self):
        self.seed()
        branches = set(
            Student.all_objects.filter(tenant=self.solo.tenant)
            .values_list("branch_id", flat=True),
        )
        self.assertEqual(len(branches), 1)

    def test_a_suspended_student_keeps_their_seat(self):
        """Driven through the state machine, so this could not be faked."""
        self.seed()
        suspended = Student.all_objects.filter(
            status=StudentStatus.SUSPENDED,
        ).first()
        self.assertIsNotNone(suspended)
        self.assertTrue(suspended.enrolments.filter(is_active=True).exists())

    def test_a_withdrawn_student_has_released_theirs(self):
        self.seed()
        withdrawn = Student.all_objects.filter(
            status=StudentStatus.WITHDRAWN,
        ).first()
        self.assertIsNotNone(withdrawn)
        self.assertFalse(withdrawn.enrolments.filter(is_active=True).exists())
        self.assertTrue(withdrawn.enrolments.exists())


class RefusalTests(_Base):
    def test_it_refuses_rather_than_half_seeding_without_a_year(self):
        """A state that cannot be reached honestly fails loudly.

        A student is placed into a year. Writing rows that look right without
        one is exactly what this command exists not to do.
        """
        AcademicSession.all_objects.filter(
            tenant=self.solo.tenant,
        ).update(status="DRAFT")
        with self.assertRaises(CommandError):
            self.seed(only="st-monicas")
        self.assertEqual(_counts(self.solo.tenant)["students"], 0)

    def test_an_unknown_slug_names_the_cast(self):
        with self.assertRaises(CommandError) as caught:
            self.seed(only="nowhere")
        self.assertIn("brightfield-lekki", str(caught.exception))
