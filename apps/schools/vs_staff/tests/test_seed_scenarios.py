"""The scenario seeder, run twice.

Running it once proves it builds something. Running it twice proves it builds
the same thing, which is the property that matters: a seeder that is not
idempotent invents data every time somebody refreshes their environment, and the
extra rows are indistinguishable from rows a person created.

The coverage tests below are the other half. A screen cannot be checked against
an endpoint that returns nothing, and this module's screens show states that
cannot all exist in one person: somebody invited and somebody terminated, an
account locked while its owner is plainly employed, a subject with a lead, a
subject with assistants and nobody owning it, and a subject nobody teaches. If a
state has no row behind it, the screen that renders it was never really checked.
"""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from schools.vs_staff.constants import (
    LEAVE_APPROVER_GROUP_CODE,
    EmploymentStatus,
    TeachingPart,
)
from schools.vs_staff.models import (
    LeaveRequest,
    StaffEmploymentEvent,
    StaffProfile,
    TeachingAssignment,
)
from vs_rbac.tests.helpers import (
    make_branch,
    make_role,
    make_school,
    make_school_admin,
)


def _counts(tenant):
    return {
        "staff": StaffProfile.all_objects.filter(tenant=tenant).count(),
        "events": StaffEmploymentEvent.all_objects.filter(tenant=tenant).count(),
        "teaching": TeachingAssignment.all_objects.filter(tenant=tenant).count(),
        "leave": LeaveRequest.all_objects.filter(tenant=tenant).count(),
    }


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.multi = make_school(slug="brightfield-lekki", name="Brightfield Schools")
        make_branch(cls.multi, name="Lekki", is_main=True)
        make_branch(cls.multi, name="Ikeja", is_main=False)

        # One branch AND live: the only place the recede rule can be seen.
        cls.solo_live = make_school(slug="sunrise-academy", name="Sunrise Academy")
        make_branch(cls.solo_live, name="Main", is_main=True)

        # One branch, still onboarding: the pre-live shape.
        cls.solo = make_school(slug="st-monicas", name="St. Monica's Academy")
        make_branch(cls.solo, name="Main", is_main=True)

        # The seeder attributes every row to somebody, because the services it
        # drives write an actor onto everything they create. Each school also
        # needs a role to invite people into: the seeder refuses by name rather
        # than inventing one, since a person created with no role is an account
        # that can sign in and reach nothing.
        for index, school in enumerate((cls.multi, cls.solo_live, cls.solo)):
            make_school_admin(
                None, email=f"staffseed{index}@example.test", tenant=school.tenant,
            )
            make_role(school, name="School Admin", key="school_admin")
            make_role(school, name="Teacher", key="teacher")

        # Named one at a time rather than run whole. The academics seeder has a
        # wider cast than this module's, and calling it bare would demand two
        # schools that have nothing to do with staff and refuse loudly when they
        # are absent. Asking only for the three built above keeps this fixture's
        # dependency to what it actually uses.
        for slug in ("brightfield-lekki", "sunrise-academy", "st-monicas"):
            call_command("seed_academic_scenarios", only=slug, verbosity=0)

    def test_the_cast_and_this_fixture_name_the_same_schools(self):
        """A school added to CAST and not here fails EVERY test in this file.

        Asserting the pair keeps the next addition to a single, readable failure
        rather than several confusing ones.
        """
        from schools.vs_schools.models import School
        from schools.vs_staff.management.commands.seed_staff_scenarios import CAST

        seeded = set(School.objects.values_list("slug", flat=True))
        self.assertEqual(
            sorted(set(CAST) - seeded), [],
            "these are in CAST but not built by this fixture",
        )

    def seed(self, only=None):
        out = StringIO()
        call_command(
            "seed_staff_scenarios", stdout=out,
            **({"only": only} if only else {}),
        )
        return out.getvalue()


class IdempotenceTests(_Base):
    def test_it_builds_something_in_the_first_place(self):
        """Idempotence over an empty result would be trivially true."""
        self.seed()
        for school in (self.multi, self.solo_live, self.solo):
            with self.subTest(school=school.slug):
                self.assertGreater(_counts(school.tenant)["staff"], 0)

    def test_running_it_twice_changes_nothing(self):
        self.seed()
        first = {
            s.slug: _counts(s.tenant)
            for s in (self.multi, self.solo_live, self.solo)
        }
        self.seed()
        self.assertEqual(
            {
                s.slug: _counts(s.tenant)
                for s in (self.multi, self.solo_live, self.solo)
            },
            first,
        )

    def test_an_unknown_school_is_refused_by_name(self):
        with self.assertRaises(CommandError):
            self.seed(only="not-a-school")


class ScenarioCoverageTests(_Base):
    def test_every_employment_status_the_screens_show_has_a_row_behind_it(self):
        """A state with no row is a screen nobody can check."""
        self.seed()
        present = set(
            StaffProfile.all_objects.filter(tenant=self.multi.tenant)
            .values_list("employment_status", flat=True).distinct(),
        )
        for expected in (
            EmploymentStatus.INVITED, EmploymentStatus.ACTIVE,
            EmploymentStatus.ON_LEAVE, EmploymentStatus.SUSPENDED,
            EmploymentStatus.RESIGNED, EmploymentStatus.TERMINATED,
        ):
            with self.subTest(status=expected):
                self.assertIn(expected, present)

    def test_every_status_was_reached_through_a_logged_transition(self):
        """The reason the seeder drives services rather than writing rows.

        A staff list assembled by writing rows would happily contain somebody
        Resigned with no employment event behind it, which the service forbids
        and which every history screen would then render as a person who left
        for no reason.
        """
        self.seed()
        for profile in StaffProfile.all_objects.filter(
            tenant=self.multi.tenant,
        ).exclude(employment_status=EmploymentStatus.INVITED):
            with self.subTest(staff=profile.pk):
                self.assertTrue(
                    profile.employment_events.filter(
                        to_status=profile.employment_status,
                    ).exists(),
                    "a status with no event behind it was written directly",
                )

    def test_somebody_is_posted_school_wide(self):
        """A null posting is a first-class value, and the roster needs one.

        Without it the School-wide group on every branch roster is empty, and
        the rule that a registrar belongs to the school rather than to a site
        has nothing to demonstrate it.
        """
        self.seed()
        self.assertTrue(
            StaffProfile.all_objects.filter(
                tenant=self.multi.tenant, branch__isnull=True,
            ).exists(),
        )

    def test_a_pairing_has_a_lead_and_another_has_only_assistants(self):
        """The two kinds of gap the coverage grid counts separately.

        A pairing being taught by assistants with nobody owning the marks is the
        state a screen is most likely to render wrongly, because it looks
        covered until you ask who is responsible.
        """
        self.seed()
        rows = TeachingAssignment.all_objects.filter(tenant=self.multi.tenant)
        self.assertTrue(rows.filter(part=TeachingPart.LEAD).exists())
        self.assertTrue(rows.filter(part=TeachingPart.ASSISTANT).exists())

        led = {
            (row.school_class_id, row.subject_id)
            for row in rows.filter(part=TeachingPart.LEAD)
        }
        assisted = {
            (row.school_class_id, row.subject_id)
            for row in rows.filter(part=TeachingPart.ASSISTANT)
        }
        self.assertTrue(
            assisted - led,
            "no pairing has assistants and no lead, so the lead-gap count on "
            "the coverage grid has nothing behind it",
        )

    def test_a_class_teacher_is_designated(self):
        from schools.vs_academics.models import SchoolClass

        self.seed()
        self.assertTrue(
            SchoolClass.all_objects.filter(
                tenant=self.multi.tenant, class_teacher__isnull=False,
            ).exists(),
        )

    def test_the_leave_ladder_is_published_and_somebody_is_in_the_group(self):
        """Both halves, because they fail identically from outside.

        A school with no ladder and a school whose ladder is right but whose
        group is empty both look like leave that does not work; the first is
        refused at submission and the second parks.
        """
        from vs_workflow.models import WorkflowApproverGroup, WorkflowTemplate

        self.seed()
        for school in (self.multi, self.solo_live, self.solo):
            with self.subTest(school=school.slug):
                self.assertTrue(
                    WorkflowTemplate.all_objects.filter(
                        tenant=school.tenant,
                        document_type="schools.leave_request",
                    ).exists(),
                )
                group = WorkflowApproverGroup.all_objects.get(
                    tenant=school.tenant, code=LEAVE_APPROVER_GROUP_CODE,
                )
                self.assertTrue(
                    group.members.exists(),
                    "the group is empty, so every request would park",
                )

    def test_leave_exists_in_both_an_approved_and_a_pending_state(self):
        self.seed()
        statuses = set(
            LeaveRequest.all_objects.filter(tenant=self.multi.tenant)
            .values_list("status", flat=True),
        )
        self.assertIn("APPROVED", statuses)
        self.assertIn("PENDING", statuses)

    def test_a_null_posting_keeps_its_meaning_at_a_one_branch_school(self):
        """School-wide means school-wide however many branches there are.

        The earlier version of this test asserted that a one-branch school posts
        everybody to its one branch, and that was the test being wrong rather
        than the seed. A null posting is a first-class value meaning "across the
        whole school", not a gap to be filled in because there happens to be
        only one place to fill it with, and it must not quietly acquire a
        different meaning at a school with one branch.

        What actually recedes at a one-branch school is the CONTROL, not the
        value: the posting is absent from what the screens read either way,
        which ``SingleBranchTests`` asserts against the endpoint.
        """
        self.seed()
        rows = StaffProfile.all_objects.filter(tenant=self.solo_live.tenant)
        self.assertGreater(rows.count(), 0, "the recede case needs a cast")
        branch = self.solo_live.tenant.branches.get()
        for profile in rows:
            with self.subTest(staff=profile.pk):
                self.assertIn(
                    profile.branch_id, (None, branch.pk),
                    "a posting is either this school's one branch or school-wide",
                )
