"""The two shapes of school the elective and department-head tests run against.

Brightfield has two branches (Lekki and Ikeja) and Sunrise has one, which is also
the other school in every isolation test. The school clock is fixed at Monday 13
October 2025, so the current term is always First Term.

Brightfield's JSS1 offers Mathematics and Civic Education as core (Civic only
through its offering, since the subject itself is an elective), and Yoruba and
French as electives (French only through its offering, since the subject itself
is core). That covers both directions in which an offering overrides its
subject. Chemistry is offered at JSS1 as an elective but archived, and JSS2
offers only Mathematics, so a class there has no elective at all.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_academics.constants import (
    PERM_CLASSES_ASSIGN,
    PERM_ELECTIVES_UPDATE,
    PERM_ELECTIVES_VIEW,
    PERM_STRUCTURE_CREATE,
    PERM_STRUCTURE_UPDATE,
    PERM_STRUCTURE_VIEW,
)
from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    Department,
    Level,
    Program,
    SchoolClass,
    Subject,
    SubjectOffering,
)
from schools.vs_staff.models import StaffProfile
from schools.vs_students.constants import Gender, StudentStatus
from schools.vs_students.models import ClassEnrolment, Student, StudentStatusLog
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

NOW = dt.datetime(2025, 10, 13, 12, 0, tzinfo=dt.timezone.utc)
TODAY = dt.date(2025, 10, 13)

ADMIN_KEYS = (
    PERM_ELECTIVES_VIEW, PERM_ELECTIVES_UPDATE, PERM_CLASSES_ASSIGN,
    PERM_STRUCTURE_VIEW, PERM_STRUCTURE_UPDATE, PERM_STRUCTURE_CREATE,
)
TEACHER_KEYS = (PERM_ELECTIVES_VIEW, PERM_ELECTIVES_UPDATE, PERM_STRUCTURE_VIEW)


def school_clock(instant=NOW):
    """Pin the school's clock, the only one a term's "today" is read from."""
    return mock.patch("vs_config.clock.timezone", SimpleNamespace(now=lambda: instant))


def key_role(tenant, name, key, keys):
    role = make_role(tenant, name=name, key=key)
    for permission_key in keys:
        make_role_permission(
            role, make_permission(permission_key, scope=PermissionScope.TENANT),
        )
    return role


class ElectivesFixture(TestCase):
    """Brightfield: two branches, a class teacher per Lekki class, and a stranger."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch", is_main=False)

        cls.admin_role = key_role(cls.tenant, "School Admin", "school_admin", ADMIN_KEYS)
        cls.teacher_role = key_role(cls.tenant, "Teacher", "teacher", TEACHER_KEYS)
        cls.viewer_role = key_role(cls.tenant, "Viewer", "viewer", (PERM_ELECTIVES_VIEW,))
        cls.nobody_role = make_role(cls.tenant, name="Nobody", key="nobody")

        cls.admin = make_school_admin(
            None, email="ngozi@brightfield.test", tenant=cls.tenant,
            first_name="Ngozi", last_name="Adeyemi",
        )
        make_assignment(cls.tenant, cls.admin, cls.admin_role, branch=None)
        cls.lekki_admin = make_school_admin(
            cls.lekki, email="head@lekki.test", first_name="Femi", last_name="Lawal",
        )
        make_assignment(cls.tenant, cls.lekki_admin, cls.admin_role, branch=cls.lekki)
        cls.ikeja_admin = make_school_admin(
            cls.ikeja, email="head@ikeja.test", first_name="Ada", last_name="Obi",
        )
        make_assignment(cls.tenant, cls.ikeja_admin, cls.admin_role, branch=cls.ikeja)
        cls.viewer = make_school_admin(
            None, email="viewer@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.tenant, cls.viewer, cls.viewer_role, branch=None)
        cls.nobody = make_school_admin(
            None, email="nobody@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.tenant, cls.nobody, cls.nobody_role, branch=None)

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
        cls.second_term = AcademicTerm.all_objects.create(
            tenant=cls.tenant, session=cls.year, name="Second Term", order_index=2,
            start_date=dt.date(2026, 1, 12), end_date=dt.date(2026, 4, 3),
        )
        cls.old_year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2024/2025",
            start_date=dt.date(2024, 9, 9), end_date=dt.date(2025, 7, 18),
            status="ARCHIVED",
        )
        cls.old_term = AcademicTerm.all_objects.create(
            tenant=cls.tenant, session=cls.old_year, name="Old First Term",
            order_index=1, start_date=dt.date(2024, 9, 9), end_date=dt.date(2024, 12, 13),
        )

        program = Program.all_objects.create(
            tenant=cls.tenant, name="Junior Secondary", code="JSS",
        )
        cls.jss1 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.year, program=program,
            name="JSS1", code="JSS1", order_index=1,
        )
        cls.jss2 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.year, program=program,
            name="JSS2", code="JSS2", order_index=2,
        )

        cls.maths = cls.make_subject("Mathematics", "MTH", True, cls.jss1, cls.jss2)
        cls.civic = cls.make_subject("Civic Education", "CVC", False, cls.jss1, offering_core=True)
        cls.yoruba = cls.make_subject("Yoruba", "YOR", False, cls.jss1)
        cls.french = cls.make_subject("French", "FRN", True, cls.jss1, offering_core=False)
        cls.chemistry = cls.make_subject("Chemistry", "CHM", False, cls.jss1)
        Subject.all_objects.filter(pk=cls.chemistry.pk).update(is_active=False)

        cls.jss1a = cls.make_class(cls.jss1, "JSS1 A", "JSS1A", cls.lekki, cls.eze_staff)
        cls.jss1b = cls.make_class(cls.jss1, "JSS1 B", "JSS1B", cls.lekki, cls.bello_staff)
        cls.jss1i = cls.make_class(cls.jss1, "JSS1 Ikeja", "JSS1I", cls.ikeja, None)
        cls.jss2a = cls.make_class(cls.jss2, "JSS2 A", "JSS2A", cls.lekki, cls.eze_staff)
        cls.shared = cls.make_class(cls.jss1, "JSS1 Shared", "JSS1SH", None, None)

        cls.chiamaka = cls.make_student("Chiamaka", "Nwosu", cls.jss1a, "BFS/001")
        cls.tunde = cls.make_student("Tunde", "Bello", cls.jss1a, "BFS/002")
        cls.kelechi = cls.make_student("Kelechi", "Eze", cls.jss1a, "BFS/003")
        cls.amina = cls.make_student("Amina", "Yusuf", cls.jss1a, "BFS/004")
        StudentStatusLog.all_objects.create(
            tenant=cls.tenant, student=cls.amina,
            from_status=StudentStatus.ACTIVE, to_status=StudentStatus.SUSPENDED,
            effective_date=dt.date(2025, 10, 6),
        )
        cls.obinna = cls.make_student(
            "Obinna", "Okafor", cls.jss1a, "BFS/005", effective=dt.date(2025, 11, 3),
        )
        cls.sade = cls.make_student("Sade", "Ojo", cls.jss1b, "BFS/006")
        cls.jss2_pupil = cls.make_student("Ife", "Adeola", cls.jss2a, "BFS/007")

        cls.sciences = Department.all_objects.create(
            tenant=cls.tenant, name="Sciences", code="SCI",
        )

        # Sunrise: one branch, and the other school in every isolation test.
        cls.solo_school = make_school(slug="sunrise", name="Sunrise Academy")
        cls.solo = cls.solo_school.tenant
        cls.solo_branch = make_branch(cls.solo_school, name="Main Branch", is_main=True)
        solo_admin_role = key_role(cls.solo, "School Admin", "school_admin", ADMIN_KEYS)
        solo_teacher_role = key_role(cls.solo, "Teacher", "teacher", TEACHER_KEYS)
        cls.solo_admin = make_school_admin(
            None, email="ikenna@sunrise.test", tenant=cls.solo,
            first_name="Ikenna", last_name="Okoye",
        )
        make_assignment(cls.solo, cls.solo_admin, solo_admin_role, branch=None)
        cls.solo_teacher = make_school_admin(
            None, email="teacher@sunrise.test", tenant=cls.solo,
            first_name="Bola", last_name="Ade",
        )
        make_assignment(cls.solo, cls.solo_teacher, solo_teacher_role, branch=None)
        cls.solo_staff = StaffProfile.all_objects.create(
            tenant=cls.solo, user=cls.solo_teacher, branch=cls.solo_branch,
            employment_status="ACTIVE",
        )
        cls.solo_year = AcademicSession.all_objects.create(
            tenant=cls.solo, name="2025/2026",
            start_date=dt.date(2025, 9, 8), end_date=dt.date(2026, 7, 17),
            status="ACTIVE",
        )
        cls.solo_term = AcademicTerm.all_objects.create(
            tenant=cls.solo, session=cls.solo_year, name="First Term", order_index=1,
            start_date=dt.date(2025, 9, 8), end_date=dt.date(2025, 12, 12),
        )
        solo_level = Level.all_objects.create(
            tenant=cls.solo, session=cls.solo_year,
            program=Program.all_objects.create(tenant=cls.solo, name="Basic", code="BAS"),
            name="Basic 3", code="BAS3", order_index=1,
        )
        cls.solo_subject = Subject.all_objects.create(
            tenant=cls.solo, name="Arabic", code="ARB", is_core=False,
        )
        SubjectOffering.all_objects.create(
            tenant=cls.solo, subject=cls.solo_subject, level=solo_level,
        )
        cls.solo_class = SchoolClass.all_objects.create(
            tenant=cls.solo, session=cls.solo_year, level=solo_level,
            name="Basic 3", code="BAS3A", branch=None, class_teacher=cls.solo_staff,
        )
        cls.solo_pupil = cls.make_student(
            "Ebube", "Okoro", cls.solo_class, "SUN/001",
            tenant=cls.solo, branch=cls.solo_branch,
        )
        cls.solo_dept = Department.all_objects.create(
            tenant=cls.solo, name="Languages", code="LNG",
        )

    # ── builders ──────────────────────────────────────────────────────────

    @classmethod
    def make_teacher(cls, email, first, last, branch):
        user = make_school_admin(branch, email=email, first_name=first, last_name=last)
        make_assignment(cls.tenant, user, cls.teacher_role, branch=branch)
        staff = StaffProfile.all_objects.create(
            tenant=cls.tenant, user=user, branch=branch, employment_status="ACTIVE",
        )
        setattr(cls, f"{last.lower()}_staff", staff)
        return user

    @classmethod
    def make_subject(cls, name, code, is_core, *levels, offering_core=None):
        subject = Subject.all_objects.create(
            tenant=cls.tenant, name=name, code=code, is_core=is_core,
        )
        for level in levels:
            SubjectOffering.all_objects.create(
                tenant=cls.tenant, subject=subject, level=level, is_core=offering_core,
            )
        return subject

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
        pupil = Student.all_objects.create(
            tenant=tenant, branch=branch or cls.lekki, first_name=first, last_name=last,
            student_number=number, date_of_birth=dt.date(2013, 4, 18),
            gender=Gender.FEMALE, status=StudentStatus.ACTIVE,
            enrolment_date=dt.date(2025, 9, 8),
        )
        ClassEnrolment.all_objects.create(
            tenant=tenant, student=pupil, school_class=school_class,
            session=school_class.session, is_active=True, effective_date=effective,
        )
        return pupil

    # ── the clock and HTTP ────────────────────────────────────────────────

    def setUp(self):
        clock = school_clock()
        clock.start()
        self.addCleanup(clock.stop)

    def url(self, school_class, term=None):
        base = reverse("academics-class-electives", kwargs={"pk": school_class.pk})
        return f"{base}?term={term.pk}" if term is not None else base

    def read(self, user, school_class, term=None):
        return TenantAPIClient(user).get(self.url(school_class, term))

    def save(self, user, school_class, changes, term=None):
        return TenantAPIClient(user).put(
            self.url(school_class, term), {"changes": changes}, format="json",
        )

    @staticmethod
    def change(student, subject, takes=True):
        return {"student": student.pk, "subject": subject.pk, "takes": takes}

    @staticmethod
    def data(response):
        return response.data["data"]
