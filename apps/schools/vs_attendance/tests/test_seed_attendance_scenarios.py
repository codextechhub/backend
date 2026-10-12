"""``seed_attendance_scenarios``: every state is reached, and a second run changes nothing.

The command hangs registers off the schools the sibling seeders build, so this
class builds them the same way (onboarding, academic structure, students), with
the school clock set to the day the dev database was first seeded, then runs the
attendance seeder on a Monday six weeks later. Both shapes of school are
covered: Sunrise has one branch and Lagoon View has two.

Tagged ``slow`` because the sibling seeders and the permission seed cost
several seconds between them.
"""
from __future__ import annotations

import datetime as dt
from contextlib import redirect_stdout
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, tag
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_calendar.models import CalendarEvent
from schools.vs_schools.dev.fixtures import build_school
from schools.vs_schools.models import School
from schools.vs_staff.models import StaffProfile
from schools.vs_students.models import Student
from vs_audit.models import AuditEvent
from vs_notifications.models import Notification
from vs_user.models import User

from ..constants import ChangeVia, CorrectionStatus, Mark, NoticeState
from ..models import (
    ClassRegister,
    CorrectionRequest,
    MarkChange,
    ReasonCode,
    RegisterReminder,
    StudentMark,
)
from ..services.roster import roster_as_at
from .base import school_clock

SEEDED_ON = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
MONDAY = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)
TODAY = MONDAY.date()
SCHOOLS = {
    "sunrise-academy": ("Sunrise Academy", False),
    "lagoon-view": ("Lagoon View Academy", True),
}
SLUGS = tuple(SCHOOLS)


def _build(name, **options) -> None:
    """Run a prerequisite seeder with its chatter thrown away."""
    sink = StringIO()
    with redirect_stdout(sink):
        call_command(name, stdout=sink, stderr=sink, **options)


def _counts(tenant) -> dict:
    return {
        "registers": ClassRegister.objects.filter(tenant=tenant).count(),
        "marks": StudentMark.objects.filter(tenant=tenant).count(),
        "changes": MarkChange.objects.filter(tenant=tenant).count(),
        "corrections": CorrectionRequest.objects.filter(tenant=tenant).count(),
        "reminders": RegisterReminder.objects.filter(tenant=tenant).count(),
        "events": CalendarEvent.all_objects.filter(tenant=tenant).count(),
        "retired": ReasonCode.objects.filter(
            tenant=tenant, retired_at__isnull=False,
        ).count(),
        "students": Student.all_objects.filter(tenant=tenant).count(),
        "accounts": User.objects.filter(tenant=tenant).count(),
        "staff": StaffProfile.all_objects.filter(tenant=tenant).count(),
        "audit": AuditEvent.objects.filter(tenant=tenant).count(),
        "notifications": Notification.objects.filter(tenant=tenant).count(),
    }


@tag("slow")
class SeedAttendanceScenariosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with school_clock(SEEDED_ON):
            _build("seed_all_permissions")
            _build("seed_notification_event_types")
            _build("seed_notification_templates")
            for slug, (name, branched) in SCHOOLS.items():
                # The onboarding seeder's own builder, without the go-live
                # review that needs a platform operator: the school is made
                # live directly, which is all the attendance routes ask.
                build_school(
                    slug=slug, name=name, live=True, extra_branch=branched,
                    with_onboarding=False,
                )
                _build("seed_academic_scenarios", only=slug)
                _build("seed_student_scenarios", only=slug)
        cls.tenants = {
            slug: School.objects.get(slug=slug).tenant for slug in SLUGS
        }
        cls.first_run = cls.run_seed()
        cls.after_first_run = {slug: _counts(t) for slug, t in cls.tenants.items()}

    @classmethod
    def run_seed(cls, **options) -> str:
        """Run the seeder on the Monday, with the notices it triggers sent as if committed."""
        out = StringIO()
        with school_clock(MONDAY), cls.captureOnCommitCallbacks(execute=True):
            call_command("seed_attendance_scenarios", stdout=out, **options)
        return out.getvalue()

    def test_it_refuses_a_school_outside_the_cast(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.run_seed(only="holy-cross")

    def test_a_second_run_creates_nothing_in_either_shape_of_school(self):
        self.assertNotIn("Created this run: 0", self.first_run)

        second = self.run_seed()

        self.assertIn("Created this run: 0", second)
        self.assertNotIn("  + ", second)
        for slug, tenant in self.tenants.items():
            with self.subTest(school=slug):
                self.assertEqual(_counts(tenant), self.after_first_run[slug])

    def test_one_school_can_be_seeded_alone(self):
        out = self.run_seed(only="lagoon-view")
        self.assertIn("Lagoon View Academy", out)
        self.assertNotIn("Sunrise Academy", out)

    def test_every_state_is_reached_at_a_school_with_one_branch(self):
        tenant = self.tenants["sunrise-academy"]
        self.assert_the_states(tenant)
        self.assertEqual(tenant.branches.count(), 1)
        self.assertFalse(
            CalendarEvent.all_objects.filter(tenant=tenant, branch__isnull=False).exists(),
        )

    def test_every_state_is_reached_at_a_school_with_two_branches(self):
        tenant = self.tenants["lagoon-view"]
        self.assert_the_states(tenant)
        second = tenant.branches.order_by("-is_main", "code")[1]
        self.assertTrue(
            CalendarEvent.all_objects.filter(
                tenant=tenant, branch=second, closes_school=True,
            ).exists(),
        )
        # The second branch has registers of its own.
        self.assertTrue(
            ClassRegister.objects.filter(tenant=tenant, branch=second).exists(),
        )

    def assert_the_states(self, tenant):
        marks = StudentMark.objects.filter(tenant=tenant)
        registers = ClassRegister.objects.filter(tenant=tenant)

        self.assertTrue(marks.filter(notice_state=NoticeState.SENT).exists())
        self.assertTrue(marks.filter(notice_state=NoticeState.NO_EMAIL).exists())
        excused = marks.filter(mark=Mark.EXCUSED).first()
        self.assertEqual(excused.reason_code.name, "Sick")
        self.assertTrue(excused.note)
        self.assertEqual(marks.filter(mark=Mark.LATE).first().arrived_at, dt.time(8, 40))

        behalf = registers.filter(on_behalf_of__isnull=False)
        self.assertTrue(behalf.exists())
        self.assertTrue(all(r.submitted_by_id != r.on_behalf_of.user_id for r in behalf))

        same_day = MarkChange.objects.filter(
            tenant=tenant, via=ChangeVia.SAME_DAY, from_mark=Mark.ABSENT, to_mark=Mark.LATE,
        )
        self.assertEqual(same_day.count(), 1)
        self.assertEqual(same_day.first().mark.register.date, TODAY)

        statuses = sorted(
            CorrectionRequest.objects.filter(tenant=tenant).values_list("status", flat=True),
        )
        self.assertEqual(statuses, sorted([
            CorrectionStatus.PENDING, CorrectionStatus.APPROVED, CorrectionStatus.REJECTED,
        ]))

        self.assertEqual(
            list(ReasonCode.objects.filter(
                tenant=tenant, retired_at__isnull=False,
            ).values_list("name", flat=True)),
            ["Travel"],
        )
        self.assertTrue(
            CalendarEvent.all_objects.filter(
                tenant=tenant, closes_school=True, branch__isnull=True,
            ).exists(),
        )

        today_registers = registers.filter(date=TODAY)
        self.assertTrue(today_registers.filter(submitted_at__isnull=False).exists())
        reminded = RegisterReminder.objects.filter(tenant=tenant, date=TODAY)
        self.assertEqual(reminded.count(), 1)

        locked = [
            entry for register in registers
            for entry in roster_as_at(tenant, register.school_class, register.date)
            if entry.suspended
        ]
        self.assertTrue(locked)

    def test_the_hub_shows_one_reminded_and_one_unreminded_class(self):
        for slug, tenant in self.tenants.items():
            with self.subTest(school=slug), school_clock(MONDAY):
                admin = User.objects.get(tenant=tenant, email=f"admin@{slug}.example.com")
                response = TenantAPIClient(admin).get(reverse("attendance-today"))
                self.assertEqual(response.status_code, 200, response.data)
                hub = response.data["data"]
                flags = {row["reminded"] for row in hub["not_taken"]}
                self.assertEqual(flags, {True, False})
                self.assertEqual(hub["kpis"]["corrections_waiting"], 1)
                self.assertEqual(hub["kpis"]["absent"], 1)
                self.assertEqual(hub["kpis"]["late"], 1)

    def test_a_pupil_below_the_line_sits_on_the_chronic_list(self):
        for slug, tenant in self.tenants.items():
            with self.subTest(school=slug), school_clock(MONDAY):
                admin = User.objects.get(tenant=tenant, email=f"admin@{slug}.example.com")
                response = TenantAPIClient(admin).get(reverse("attendance-reports"))
                chronic = response.data["data"]["chronic"]
                self.assertTrue(chronic)
                self.assertTrue(all(row["rate"] < 90 for row in chronic))
