"""The two shapes of school every test in this module runs against.

Brightfield has two branches (Lekki and Ikeja) and Sunrise has one, and half the
rules here only exist in one of them: a single-branch test proves nothing about
a multi-branch school and the reverse is just as true. Sunrise is also the
other tenant in every isolation test.

The school clock is fixed at Monday 13 October 2025, 13:00 in Lagos, so "today"
is the same day for every test and a register for last Friday is a register for
a day that has closed.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    Level,
    Program,
    SchoolClass,
)
from schools.vs_calendar.models import CalendarEvent
from schools.vs_staff.models import StaffProfile
from schools.vs_students.constants import Gender, Relationship, StudentStatus
from schools.vs_students.models import (
    ClassEnrolment,
    Guardian,
    Student,
    StudentGuardian,
    StudentStatusLog,
)
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

from ..constants import (
    PERM_CORRECTION_APPROVE,
    PERM_CORRECTION_CREATE,
    PERM_REGISTER_MARK,
    PERM_REGISTER_MARK_ANY,
    PERM_REGISTER_REMIND,
    PERM_REGISTER_VIEW,
)

ADMIN_KEYS = (
    PERM_REGISTER_VIEW, PERM_REGISTER_MARK_ANY, PERM_REGISTER_REMIND,
    PERM_CORRECTION_CREATE, PERM_CORRECTION_APPROVE,
)
TEACHER_KEYS = (PERM_REGISTER_VIEW, PERM_REGISTER_MARK, PERM_CORRECTION_CREATE)

#: Monday 13 October 2025, 13:00 in Lagos.
NOW = dt.datetime(2025, 10, 13, 12, 0, tzinfo=dt.timezone.utc)
TODAY = dt.date(2025, 10, 13)
YESTERDAY = dt.date(2025, 10, 12)
LAST_FRIDAY = dt.date(2025, 10, 10)
FOUNDERS_DAY = dt.date(2025, 10, 9)
LEKKI_OPEN_DAY = dt.date(2025, 10, 7)


def school_clock(instant=NOW):
    """Pin the school's clock, the only one a register's "today" is read from."""
    return mock.patch("vs_config.clock.timezone", SimpleNamespace(now=lambda: instant))


def make_key_role(tenant, name, key, keys):
    role = make_role(tenant, name=name, key=key)
    for permission_key in keys:
        make_role_permission(
            role, make_permission(permission_key, scope=PermissionScope.TENANT),
        )
    return role


def set_setting(key, value, tenant, actor):
    set_value(
        definition=ConfigurationDefinition.objects.get(key=key),
        value=value, actor=actor, tenant=tenant,
    )


class AttendanceFixture(TestCase):
    """Brightfield: two branches, a class teacher per Lekki class, and a stranger."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch", is_main=False)

        cls.admin_role = make_key_role(cls.tenant, "School Admin", "school_admin", ADMIN_KEYS)
        cls.teacher_role = make_key_role(cls.tenant, "Teacher", "teacher", TEACHER_KEYS)
        cls.nobody_role = make_role(cls.tenant, name="Nobody", key="nobody")

        # Whole-school registrar.
        cls.admin = make_school_admin(
            None, email="ngozi@brightfield.test", tenant=cls.tenant,
            first_name="Ngozi", last_name="Adeyemi",
        )
        make_assignment(cls.tenant, cls.admin, cls.admin_role, branch=None)
        # Branch admins, each pinned to their own branch.
        cls.lekki_admin = make_school_admin(
            cls.lekki, email="head@lekki.test", first_name="Femi", last_name="Lawal",
        )
        make_assignment(cls.tenant, cls.lekki_admin, cls.admin_role, branch=cls.lekki)
        cls.ikeja_admin = make_school_admin(
            cls.ikeja, email="head@ikeja.test", first_name="Ada", last_name="Obi",
        )
        make_assignment(cls.tenant, cls.ikeja_admin, cls.admin_role, branch=cls.ikeja)
        # Holds no attendance key at all.
        cls.nobody = make_school_admin(
            None, email="nobody@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.tenant, cls.nobody, cls.nobody_role, branch=None)

        # Two teachers who each hold `mark`: Mr Eze leads JSS1 A, Ms Bello JSS1 B.
        cls.eze = cls.make_teacher("eze@brightfield.test", "Chidi", "Eze", cls.lekki)
        cls.bello = cls.make_teacher("bello@brightfield.test", "Sade", "Bello", cls.lekki)

        cls.year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2025/2026",
            start_date=dt.date(2025, 9, 8), end_date=dt.date(2026, 7, 17),
            status="ACTIVE",
        )
        cls.first_term = AcademicTerm.all_objects.create(
            tenant=cls.tenant, session=cls.year, name="First Term", order_index=1,
            start_date=dt.date(2025, 9, 8), end_date=dt.date(2025, 12, 12),
        )
        program = Program.all_objects.create(
            tenant=cls.tenant, name="Junior Secondary", code="JSS",
        )
        cls.jss1 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.year, program=program,
            name="JSS1", code="JSS1", order_index=1,
        )
        cls.pry4 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.year, program=program,
            name="Primary 4", code="PRY4", order_index=2,
        )
        cls.jss1a = cls.make_class(cls.jss1, "JSS1 A", "JSS1A", cls.lekki, cls.eze_staff)
        cls.jss1b = cls.make_class(cls.jss1, "JSS1 B", "JSS1B", cls.lekki, cls.bello_staff)
        cls.sss2 = cls.make_class(cls.jss1, "SSS2 Science", "SSS2S", cls.ikeja, None)
        cls.pry4a = cls.make_class(cls.pry4, "Primary 4 A", "PRY4A", cls.lekki, None)
        cls.shared = cls.make_class(cls.jss1, "JSS1 Shared", "JSS1SH", None, None)

        # JSS1 A: five pupils on the roll on the 13th, one placed later, and a
        # sixth suspended.
        cls.chiamaka = cls.make_student("Chiamaka", "Nwosu", cls.jss1a, "BFS/001")
        cls.tunde = cls.make_student("Tunde", "Bello", cls.jss1a, "BFS/002")
        cls.kelechi = cls.make_student("Kelechi", "Eze", cls.jss1a, "BFS/003")
        cls.amina = cls.make_student("Amina", "Yusuf", cls.jss1a, "BFS/004")
        cls.make_suspension(cls.amina, dt.date(2025, 10, 6))
        cls.obinna = cls.make_student(
            "Obinna", "Okafor", cls.jss1a, "BFS/005", effective=dt.date(2025, 10, 20),
        )

        # Her mother has an address, Tunde's has none, Kelechi's has an account.
        cls.nwosu = cls.make_guardian(
            cls.chiamaka, "Mrs. Ngozi Nwosu", email="ngozi.nwosu@example.ng",
        )
        cls.make_guardian(cls.tunde, "Mrs. Aduke Bello", email="")
        cls.eze_parent_user = make_school_admin(
            None, email="mrs.eze@example.ng", tenant=cls.tenant,
            first_name="Ifeoma", last_name="Eze",
        )
        cls.make_guardian(
            cls.kelechi, "Mrs. Ifeoma Eze", email="mrs.eze@example.ng",
            user=cls.eze_parent_user,
        )

        # A school-wide closure and a Lekki-only one.
        cls.founders_day = CalendarEvent.all_objects.create(
            tenant=cls.tenant, session=cls.year, branch=None, name="Founders' Day",
            event_type="HOLIDAY", start_date=FOUNDERS_DAY, end_date=FOUNDERS_DAY,
            closes_school=True,
        )
        cls.lekki_open_day = CalendarEvent.all_objects.create(
            tenant=cls.tenant, session=cls.year, branch=cls.lekki, name="Lekki Open Day",
            event_type="SCHOOL_EVENT", start_date=LEKKI_OPEN_DAY, end_date=LEKKI_OPEN_DAY,
            closes_school=True,
        )

        # Sunrise: one branch, and the other tenant in every isolation test.
        cls.solo_school = make_school(slug="sunrise", name="Sunrise Academy")
        cls.solo = cls.solo_school.tenant
        cls.solo_branch = make_branch(cls.solo_school, name="Main Branch", is_main=True)
        solo_admin_role = make_key_role(cls.solo, "School Admin", "school_admin", ADMIN_KEYS)
        cls.solo_admin = make_school_admin(
            None, email="ikenna@sunrise.test", tenant=cls.solo,
            first_name="Ikenna", last_name="Okoye",
        )
        make_assignment(cls.solo, cls.solo_admin, solo_admin_role, branch=None)
        solo_teacher_role = make_key_role(cls.solo, "Teacher", "teacher", TEACHER_KEYS)
        cls.solo_teacher = make_school_admin(
            None, email="teacher@sunrise.test", tenant=cls.solo,
            first_name="Bola", last_name="Ade",
        )
        make_assignment(cls.solo, cls.solo_teacher, solo_teacher_role, branch=None)
        cls.solo_teacher_staff = StaffProfile.all_objects.create(
            tenant=cls.solo, user=cls.solo_teacher, branch=cls.solo_branch,
            employment_status="ACTIVE",
        )
        solo_year = AcademicSession.all_objects.create(
            tenant=cls.solo, name="2025/2026",
            start_date=dt.date(2025, 9, 8), end_date=dt.date(2026, 7, 17),
            status="ACTIVE",
        )
        AcademicTerm.all_objects.create(
            tenant=cls.solo, session=solo_year, name="First Term", order_index=1,
            start_date=dt.date(2025, 9, 8), end_date=dt.date(2025, 12, 12),
        )
        solo_level = Level.all_objects.create(
            tenant=cls.solo, session=solo_year,
            program=Program.all_objects.create(tenant=cls.solo, name="Basic", code="BAS"),
            name="Basic 3", code="BAS3", order_index=1,
        )
        # No branch on the class: at a one-branch school it is that branch's.
        cls.solo_class = SchoolClass.all_objects.create(
            tenant=cls.solo, session=solo_year, level=solo_level,
            name="Basic 3", code="BAS3A", branch=None,
            class_teacher=cls.solo_teacher_staff,
        )
        cls.solo_pupil = cls.make_student(
            "Ebube", "Okoro", cls.solo_class, "SUN/001",
            tenant=cls.solo, branch=cls.solo_branch,
        )

    # ── builders ──────────────────────────────────────────────────────────

    @classmethod
    def make_teacher(cls, email, first, last, branch):
        user = make_school_admin(
            branch, email=email, first_name=first, last_name=last,
        )
        make_assignment(cls.tenant, user, cls.teacher_role, branch=branch)
        staff = StaffProfile.all_objects.create(
            tenant=cls.tenant, user=user, branch=branch, employment_status="ACTIVE",
        )
        setattr(cls, f"{last.lower()}_staff", staff)
        return user

    @classmethod
    def make_class(cls, level, name, code, branch, teacher):
        return SchoolClass.all_objects.create(
            tenant=cls.tenant, session=cls.year, level=level, name=name,
            code=code, branch=branch, class_teacher=teacher,
        )

    @classmethod
    def make_student(
        cls, first, last, school_class, number, *, effective=dt.date(2025, 9, 8),
        tenant=None, branch=None,
    ):
        tenant = tenant or cls.tenant
        branch = branch or cls.lekki
        pupil = Student.all_objects.create(
            tenant=tenant, branch=branch, first_name=first, last_name=last,
            student_number=number, date_of_birth=dt.date(2013, 4, 18),
            gender=Gender.FEMALE, status=StudentStatus.ACTIVE,
            enrolment_date=dt.date(2025, 9, 8),
        )
        ClassEnrolment.all_objects.create(
            tenant=tenant, student=pupil, school_class=school_class,
            session=school_class.session, is_active=True, effective_date=effective,
        )
        return pupil

    @classmethod
    def make_guardian(cls, student, name, *, email, user=None):
        guardian = Guardian.all_objects.create(
            tenant=student.tenant, full_name=name, email=email,
            phone="08035550101", user=user,
        )
        StudentGuardian.all_objects.create(
            tenant=student.tenant, student=student, guardian=guardian,
            relationship=Relationship.MOTHER, is_primary=True,
        )
        return guardian

    @classmethod
    def make_suspension(cls, student, effective, return_date=None):
        StudentStatusLog.all_objects.create(
            tenant=student.tenant, student=student,
            from_status=StudentStatus.ACTIVE, to_status=StudentStatus.SUSPENDED,
            effective_date=effective, return_date=return_date,
        )

    # ── the clock ─────────────────────────────────────────────────────────

    def setUp(self):
        clock = school_clock()
        clock.start()
        self.addCleanup(clock.stop)

    # ── HTTP ──────────────────────────────────────────────────────────────

    def url(self, name, school_class, on_date, **extra):
        return reverse(name, kwargs={
            "class_id": school_class.pk, "on_date": str(on_date), **extra,
        })

    def read(self, user, school_class, on_date=TODAY):
        return TenantAPIClient(user).get(self.url("attendance-register", school_class, on_date))

    def mark(self, user, school_class, student, mark, on_date=TODAY, **body):
        # Notices go out once the request's transaction commits, which a test
        # inside its own transaction never reaches unless it is told to.
        with self.captureOnCommitCallbacks(execute=True):
            return TenantAPIClient(user).put(
                self.url("attendance-mark", school_class, on_date, student_id=student.pk),
                {"mark": mark, **body}, format="json",
            )

    def fill(self, user, school_class, on_date=TODAY):
        with self.captureOnCommitCallbacks(execute=True):
            return TenantAPIClient(user).post(
                self.url("attendance-fill-present", school_class, on_date), {}, format="json",
            )

    def submit(self, user, school_class, on_date=TODAY):
        with self.captureOnCommitCallbacks(execute=True):
            return TenantAPIClient(user).post(
                self.url("attendance-submit", school_class, on_date), {}, format="json",
            )

    def remind(self, user, school_class, on_date=TODAY):
        return TenantAPIClient(user).post(
            self.url("attendance-remind", school_class, on_date), {}, format="json",
        )

    def correct(self, user, school_class, student, to_mark, on_date=LAST_FRIDAY, **body):
        """Ask for a correction to *student*'s mark on a closed day."""
        return TenantAPIClient(user).post(
            reverse("attendance-corrections"),
            {
                "register_class_id": school_class.pk, "date": str(on_date),
                "student_id": student.pk, "to_mark": to_mark, **body,
            },
            format="json",
        )

    def decide(self, user, correction_id, verb, **body):
        """Approve or reject (*verb*) a correction. Notices go out on commit."""
        with self.captureOnCommitCallbacks(execute=True):
            return TenantAPIClient(user).post(
                reverse(f"attendance-correction-{verb}", kwargs={"correction_id": correction_id}),
                body, format="json",
            )

    def corrections(self, user, **params):
        return TenantAPIClient(user).get(reverse("attendance-corrections"), params)

    def today(self, user, **params):
        return TenantAPIClient(user).get(reverse("attendance-today"), params)

    def today_mine(self, user, **params):
        return TenantAPIClient(user).get(reverse("attendance-today-mine"), params)

    def student_tab(self, user, student, **params):
        return TenantAPIClient(user).get(
            reverse("attendance-student", kwargs={"student_id": student.pk}), params,
        )

    def take(self, user, school_class, on_date, **marks):
        """Mark the class's whole roster on *on_date* and submit it.

        *marks* maps a pupil's first name (lower case) to a mark; anyone not
        named is Present and a Late mark arrives at 08:30. Returns the submit
        response.
        """
        from schools.vs_students.services.roster import roster_as_at

        for entry in roster_as_at(school_class.tenant, school_class, on_date):
            if entry.suspended:
                continue
            kind = marks.get(entry.student.first_name.lower(), "P")
            body = {"arrived_at": "08:30"} if kind == "L" else {}
            if kind == "E":
                body["reason_code"] = self.reason_id()
            response = self.mark(user, school_class, entry.student, kind, on_date=on_date, **body)
            assert response.status_code == 200, response.data
        return self.submit(user, school_class, on_date)

    def reason_id(self, name="Sick", tenant=None):
        from ..models import ReasonCode
        from ..services.reasons import ensure_default_reason_codes

        tenant = tenant or self.tenant
        ensure_default_reason_codes(tenant)
        return ReasonCode.objects.get(tenant=tenant, name=name).pk

    @staticmethod
    def data(response):
        return response.data["data"]

    @staticmethod
    def code(response):
        return response.data["error"]["code"]

    def rows(self, response):
        return {row["student_id"]: row for row in self.data(response)["rows"]}

    def take_whole_register(self, user, school_class=None, on_date=TODAY):
        """Mark every unlocked pupil of JSS1 A Present, as the class teacher would."""
        school_class = school_class or self.jss1a
        response = self.fill(user, school_class, on_date)
        self.assertEqual(response.status_code, 200, response.data)
        return response
