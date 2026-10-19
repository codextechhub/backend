"""``seed_parent_portal_scenarios``: every shape is reached, and a second run changes nothing.

The command hangs parents off the schools the sibling seeders build, so this
class builds them the same way (permissions, notification catalogue,
onboarding, academic structure, students, staff, results), with the school
clock set to the day the dev database was first seeded, then runs the parent
seeder on a Monday six weeks later. Both shapes of school are covered: Sunrise
has one branch and Lagoon View has two.

Tagged ``slow`` because the sibling seeders and the permission seed cost
several seconds between them.
"""
from __future__ import annotations

import datetime as dt
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, tag

from schools.vs_gradebook.tests.base import school_clock
from schools.vs_notices.models import Notice
from schools.vs_notices.services import notices as notice_service
from schools.vs_schools.dev.fixtures import build_school
from schools.vs_schools.models import School
from schools.vs_student_portal.fees import fee_summary
from schools.vs_students.constants import StudentStatus
from schools.vs_students.models import Guardian, Student, StudentGuardian
from schools.vs_students.services import guardian_portal_access as access
from vs_finance.models import Invoice
from vs_payments.constants import VirtualAccountStatus
from vs_payments.exceptions import ProviderError
from vs_payments.models import VirtualAccount
from vs_user.models import User

from ..management.commands.seed_parent_portal_scenarios import (
    CLASS_NOTICE,
    PARENT_PASSWORD,
    SCHOOL_NOTICE,
)

SEEDED_ON = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
MONDAY = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)
SCHOOLS = {
    "sunrise-academy": ("Sunrise Academy", False),
    "lagoon-view": ("Lagoon View Academy", True),
}


def _build(name, **options) -> None:
    """Run a prerequisite seeder with its chatter thrown away."""
    sink = StringIO()
    with redirect_stdout(sink):
        call_command(name, stdout=sink, stderr=sink, **options)


def _paystack_offline():
    """Paystack answering as an outage at the provider boundary, never a socket."""
    offline = ProviderError("Paystack is not reachable from tests.", provider="PAYSTACK")
    return mock.patch("vs_payments.providers.paystack.request_json", side_effect=offline)


def _counts() -> dict:
    return {
        "guardians": Guardian.all_objects.count(),
        "users": User.objects.count(),
        "links": StudentGuardian.objects.count(),
        "invoices": Invoice.objects.count(),
        "notices": Notice.objects.count(),
        "transfer accounts": VirtualAccount.objects.count(),
    }


@tag("slow")
class SeedParentPortalScenariosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with school_clock(SEEDED_ON), _paystack_offline():
            _build("seed_all_permissions")
            _build("seed_notification_event_types")
            _build("seed_notification_templates")
            for slug, (name, branched) in SCHOOLS.items():
                build_school(
                    slug=slug, name=name, live=True, extra_branch=branched,
                    with_onboarding=False,
                )
                _build("seed_academic_scenarios", only=slug)
                _build("seed_student_scenarios", only=slug)
            _build("seed_staff_scenarios", only="sunrise-academy")
            _build("seed_attendance_scenarios", only="lagoon-view")
            _build("seed_gradebook_scenarios", only="lagoon-view")
        cls.tenants = {slug: School.objects.get(slug=slug).tenant for slug in SCHOOLS}
        cls.first_run = cls.run_seed()
        cls.after_first_run = _counts()

    @classmethod
    def run_seed(cls, **options) -> str:
        """Run the seeder with Paystack answering as unreachable.

        Billing a child's first invoice asks the provider for a transfer
        account, so every seeder here runs under :func:`_paystack_offline`. It
        is entered first so it is still in force when the on-commit callbacks
        run, which is where the provider is asked.
        """
        out = StringIO()
        with (
            _paystack_offline(), school_clock(MONDAY),
            cls.captureOnCommitCallbacks(execute=True),
        ):
            call_command("seed_parent_portal_scenarios", stdout=out, **options)
        return out.getvalue()

    def guardian(self, slug, email):
        return Guardian.all_objects.select_related("user").get(
            tenant=self.tenants[slug], email__iexact=email,
        )

    def children(self, guardian):
        """The guardian's attending children, those the seeder bills and addresses."""
        return list(
            Student.all_objects.filter(
                guardian_links__guardian=guardian, status=StudentStatus.ACTIVE,
            ).select_related("branch").order_by("pk"),
        )

    def test_a_second_run_creates_nothing_and_prints_the_same_summary(self):
        second = self.run_seed()

        self.assertEqual(_counts(), self.after_first_run)
        self.assertEqual(second, self.first_run)

    def test_the_summary_names_each_sign_in_and_how_to_use_it(self):
        self.assertIn("patricia.okafor@", self.first_run)
        self.assertIn("emeka.nnadi@", self.first_run)
        self.assertIn(PARENT_PASSWORD, self.first_run)
        self.assertIn("Sign in at the school's own address", self.first_run)

    def test_one_run_can_fill_one_school(self):
        out = self.run_seed(only="sunrise-academy")
        self.assertIn("Sunrise Academy", out)
        self.assertNotIn("Lagoon View Academy", out)

    def test_a_school_with_one_branch_has_a_parent_with_one_owing_child(self):
        guardian = self.guardian("sunrise-academy", "emeka.nnadi@example.ng")
        self.assertEqual(access.state_of(guardian), access.ACTIVE)
        self.assertIsNone(guardian.user.branch_id)
        self.assertTrue(guardian.user.check_password(PARENT_PASSWORD))
        (child,) = self.children(guardian)
        self.assertGreater(fee_summary(child)["balance"], 0)
        self.assertEqual(self.tenants["sunrise-academy"].branches.count(), 1)

    def test_mrs_okafor_has_two_children_at_two_branches_one_owing_and_one_paid(self):
        guardian = self.guardian("lagoon-view", "patricia.okafor@example.ng")
        self.assertEqual(access.state_of(guardian), access.ACTIVE)
        self.assertIsNone(guardian.user.branch_id)
        self.assertTrue(guardian.user.check_password(PARENT_PASSWORD))
        children = self.children(guardian)
        self.assertEqual(len(children), 2)
        self.assertEqual(len({child.branch_id for child in children}), 2)
        self.assertEqual(
            sorted(fee_summary(child)["balance"] == 0 for child in children),
            [False, True],
        )

    def test_each_of_her_children_has_a_transfer_account_of_their_own(self):
        children = self.children(self.guardian("lagoon-view", "patricia.okafor@example.ng"))
        accounts = [
            VirtualAccount.objects.get(
                customer__source_id=str(child.pk), status=VirtualAccountStatus.ACTIVE,
            )
            for child in children
        ]
        self.assertEqual({account.provider for account in accounts}, {"FAKE"})
        self.assertEqual(len({account.account_number for account in accounts}), 2)
        for child, account in zip(children, accounts):
            with self.subTest(child=child.full_name):
                self.assertIn(account.account_number, self.first_run)

    def test_one_of_her_children_has_a_released_result_and_a_day_to_note(self):
        from schools.vs_attendance.services.student_tab import pupil_attendance
        from schools.vs_student_portal.results import term_choices

        children = self.children(self.guardian("lagoon-view", "patricia.okafor@example.ng"))
        released = [
            child for child in children
            if any(term["state"] == "released" for term in term_choices(child))
        ]
        self.assertEqual(len(released), 1)
        self.assertTrue(pupil_attendance(self.tenants["lagoon-view"], released[0])["notes"])

    def test_the_school_notice_reaches_both_children_and_the_class_notice_one(self):
        guardian = self.guardian("lagoon-view", "patricia.okafor@example.ng")
        children = self.children(guardian)
        placements = notice_service.pupil_placements(children)
        tenant = self.tenants["lagoon-view"]
        wide = Notice.objects.get(tenant=tenant, title=SCHOOL_NOTICE[0])
        classed = Notice.objects.get(tenant=tenant, title=CLASS_NOTICE[0])
        self.assertEqual(
            len(notice_service.children_addressed(wide, children, placements)), 2,
        )
        self.assertEqual(
            len(notice_service.children_addressed(classed, children, placements)), 1,
        )

    def test_a_teacher_who_is_a_guardian_keeps_the_one_account(self):
        staff_guardians = [
            g for g in Guardian.all_objects.filter(
                tenant=self.tenants["lagoon-view"], user__isnull=False,
            ).select_related("user")
            if hasattr(g.user, "staff_profile")
        ]
        self.assertEqual(len(staff_guardians), 1)
        user = staff_guardians[0].user
        self.assertEqual(
            User.objects.filter(tenant=self.tenants["lagoon-view"], email__iexact=user.email).count(),
            1,
        )
        self.assertEqual(access.state_of(staff_guardians[0]), access.ACTIVE)
        self.assertEqual(len(self.children(staff_guardians[0])), 1)

    def test_an_invited_guardian_has_not_activated(self):
        guardian = self.guardian("lagoon-view", "ifeoma.danjuma@example.ng")
        self.assertEqual(access.state_of(guardian), access.INVITED)
        self.assertEqual(guardian.user.status, User.Status.PENDING)
        self.assertIsNone(guardian.user.branch_id)
        self.assertFalse(guardian.user.has_usable_password())
