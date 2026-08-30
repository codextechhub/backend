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

from vs_rbac.tests.helpers import make_branch, make_school
from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    Department,
    Level,
    Program,
    SchoolClass,
    SessionStatus,
    Subject,
    SubjectOffering,
)


def _counts(tenant):
    return {
        "sessions": AcademicSession.all_objects.filter(tenant=tenant).count(),
        "terms": AcademicTerm.all_objects.filter(tenant=tenant).count(),
        "departments": Department.all_objects.filter(tenant=tenant).count(),
        "programs": Program.all_objects.filter(tenant=tenant).count(),
        "levels": Level.all_objects.filter(tenant=tenant).count(),
        "classes": SchoolClass.all_objects.filter(tenant=tenant).count(),
        "subjects": Subject.all_objects.filter(tenant=tenant).count(),
        "offerings": SubjectOffering.all_objects.filter(tenant=tenant).count(),
    }


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.multi = make_school(slug="brightfield-lekki", name="Brightfield Schools")
        make_branch(cls.multi, name="Lekki Campus", is_main=True)
        make_branch(cls.multi, name="Ikeja Campus", is_main=False)

        cls.solo = make_school(slug="st-monicas", name="St. Monica's Academy")
        make_branch(cls.solo, name="Main Campus", is_main=True)

        cls.live = make_school(slug="holy-cross", name="Holy Cross College")
        make_branch(cls.live, name="Holy Cross Main Branch", is_main=True)
        make_branch(cls.live, name="Holy Cross Annex", is_main=False)

        # One branch AND live. The single-branch shape on a school the API will
        # actually answer for.
        cls.solo_live = make_school(slug="sunrise-academy", name="Sunrise Academy")
        make_branch(cls.solo_live, name="Main Branch", is_main=True)

        # Two branches AND live: the branch-filter case on a school the API
        # answers for.
        cls.multi_live = make_school(slug="lagoon-view", name="Lagoon View Academy")
        make_branch(cls.multi_live, name="Lagoon View Main", is_main=True)
        make_branch(cls.multi_live, name="Lagoon View Annex", is_main=False)

    def test_the_cast_and_this_fixture_name_the_same_schools(self):
        """A school added to CAST and not here fails EVERY test in this file.

        The seeder refuses a slug it cannot find, so the failure is eight
        CommandErrors rather than one clear miss - which is what it did when
        holy-cross joined the cast. Asserting the pair keeps the next addition
        to a single, readable failure.
        """
        from schools.vs_academics.management.commands.seed_academic_scenarios import (
            CAST,
        )

        from schools.vs_schools.models import School

        seeded = set(School.objects.values_list("slug", flat=True))
        self.assertEqual(
            sorted(set(CAST) - seeded), [],
            "these are in CAST but not built by this fixture",
        )

    def seed(self, only=None):
        out = StringIO()
        call_command(
            "seed_academic_scenarios", stdout=out,
            **({"only": only} if only else {}),
        )
        return out.getvalue()


class IdempotenceTests(_Base):
    def test_running_it_twice_changes_nothing(self):
        self.seed()
        first = {
            "multi": _counts(self.multi.tenant),
            "solo": _counts(self.solo.tenant),
        }
        self.seed()
        self.assertEqual(
            {"multi": _counts(self.multi.tenant), "solo": _counts(self.solo.tenant)},
            first,
        )

    def test_it_builds_something_in_the_first_place(self):
        """Idempotence over an empty result would be trivially true."""
        self.seed()
        counts = _counts(self.multi.tenant)
        self.assertGreater(counts["programs"], 0)
        self.assertGreater(counts["levels"], 0)
        self.assertGreater(counts["classes"], 0)
        self.assertGreater(counts["offerings"], 0)

    def test_an_unknown_slug_is_refused_rather_than_silently_doing_nothing(self):
        with self.assertRaises(CommandError):
            self.seed(only="no-such-school")

    def test_a_missing_school_says_which_command_builds_it(self):
        School = type(self.multi)
        School.objects.filter(slug="st-monicas").delete()
        with self.assertRaises(CommandError) as ctx:
            self.seed(only="st-monicas")
        self.assertIn("seed_onboarding_scenarios", str(ctx.exception))


class ShapeTests(_Base):
    """The two shapes exist because one tenant cannot be both."""

    def setUp(self):
        self.seed()

    def test_the_multi_branch_school_has_rows_that_are_not_shared(self):
        """Or every scope chip and branch filter would have nothing behind it."""
        tenant = self.multi.tenant
        self.assertTrue(
            Department.all_objects.filter(
                tenant=tenant, branch__isnull=False).exists(),
        )
        self.assertTrue(
            Program.all_objects.filter(tenant=tenant, branch__isnull=False).exists(),
        )
        self.assertTrue(
            Subject.all_objects.filter(tenant=tenant, branch__isnull=False).exists(),
        )

    def test_the_single_branch_school_shares_everything(self):
        """Which is what a school that has never divided its catalogue writes."""
        tenant = self.solo.tenant
        for model in (Department, Program, Level, SchoolClass, Subject):
            self.assertFalse(
                model.all_objects.filter(tenant=tenant, branch__isnull=False).exists(),
                f"{model.__name__} should be school-wide at a one-branch school",
            )

    def test_each_school_has_one_live_year_and_a_past_and_a_future_one(self):
        for tenant in (self.multi.tenant, self.solo.tenant):
            statuses = list(
                AcademicSession.all_objects.filter(tenant=tenant)
                .values_list("status", flat=True)
            )
            self.assertEqual(statuses.count(SessionStatus.ACTIVE), 1)
            self.assertEqual(statuses.count(SessionStatus.ARCHIVED), 1)
            self.assertEqual(statuses.count(SessionStatus.DRAFT), 1)

    def test_the_archived_year_has_archived_terms(self):
        """Because it was archived through the service, not written that way."""
        archived = AcademicSession.all_objects.get(
            tenant=self.multi.tenant, status=SessionStatus.ARCHIVED,
        )
        terms = AcademicTerm.all_objects.filter(session=archived)
        self.assertTrue(terms.exists())
        self.assertFalse(terms.filter(archived_at__isnull=True).exists())

    def test_the_live_year_has_no_archived_terms(self):
        """The invariant activation exists to hold, checked on real seeded data."""
        live = AcademicSession.all_objects.get(
            tenant=self.multi.tenant, status=SessionStatus.ACTIVE,
        )
        self.assertFalse(
            AcademicTerm.all_objects.filter(
                session=live, archived_at__isnull=False).exists(),
        )

    def test_every_term_sits_inside_its_session(self):
        """The seeder runs its dates through the real validator, so this holds."""
        for term in AcademicTerm.all_objects.all():
            self.assertGreaterEqual(term.start_date, term.session.start_date)
            self.assertLessEqual(term.end_date, term.session.end_date)
            self.assertLess(term.start_date, term.end_date)
