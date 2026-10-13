"""The two shapes of school every gradebook test runs against.

Brightfield has two branches (Lekki and Ikeja) and Sunrise has one, and half the
rules here only exist in one of them: a single-branch test proves nothing about
a multi-branch school and the reverse is just as true. Sunrise is also the other
school in every isolation test.

The school clock is fixed at Monday 13 October 2025, 13:00 in Lagos, so "today"
is the same day for every test and the current term is always First Term.

**What Brightfield holds**

* Programmes: Junior Secondary (``JSS``) with level JSS1, and Senior Secondary
  (``SSS``) with level SSS1.
* Classes: JSS1 A at Lekki (class teacher Mr Eze), JSS1 B at Lekki (Ms Bello),
  JSS1 C at Ikeja (no class teacher), SSS1 A at Lekki, and JSS1 Shared with no
  branch at all. Eze and Bello work at Lekki.
* Departments: Sciences, with no head until a test names one (``self.sciences``;
  ``set_head`` does it).
* Subjects at JSS1: Mathematics (core, Sciences) and French (not core, no
  department). At SSS1: Mathematics only.
* Teaching: Mr Eze leads Mathematics in JSS1 A and Ms Bello assists.
* Students: Chiamaka, Tunde, Kelechi on JSS1 A's roll on the 13th, Amina
  suspended on JSS1 A, Obinna placed in JSS1 A only from 3 November, and Sade
  and Ife on JSS1 C. Two terms in 2025/2026.
* Users, each with a role carrying exactly the keys named: ``admin``
  (school-wide, every setup key and the hub, sheet and reminder keys),
  ``lekki_admin`` and ``ikeja_admin`` (pinned to one branch with the same keys,
  so a refusal can only be the branch rule), ``viewer`` (``setup.view`` only),
  ``updater`` (``setup.update`` only), ``eze`` and ``bello`` (teachers, no setup
  key) and ``nobody`` (no key). For the sheets: ``okeke`` (a teacher, available
  to head Sciences), ``adewale`` (leads Mathematics in SSS1 A), ``musa`` (a
  teacher with no part in any sheet), ``hub_reader`` (``hub.view`` only),
  ``enterer`` (``sheet.enter_any``) and ``registrar`` (the admin keys and the key
  that unlocks staff accounts).
* SSS1 A (Lekki) has two students, Zainab and Femi, and Adewale leads its
  Mathematics. Sunrise's Basic 3 has Mathematics (core) led by Bola Ade and two
  students.

Sunrise: one branch, one admin, one teacher, one class with no branch of its
own (it is the only branch's), one programme ``BAS``.

**Builders** make setup rows directly, without the services, so a test of the
services starts from a known state: :meth:`make_structure`, :meth:`make_scale`,
:meth:`make_class_term`. :data:`STANDARD_COMPONENTS` is two continuous
assessments of 20 and an examination of 60. :data:`JUNIOR_BANDS` and
:data:`SENIOR_BANDS` are scales; the senior one is the WAEC-style A1 to F9.
Later parts add their own fixtures by subclassing :class:`GradebookFixture`.
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
    Department,
    Level,
    Program,
    SchoolClass,
    Subject,
    SubjectOffering,
)
from schools.vs_gradebook.models import (
    AssessmentComponent,
    AssessmentStructure,
    ClassTerm,
    GradeBand,
    GradingScale,
)
from schools.vs_staff.constants import TeachingPart
from schools.vs_staff.models import StaffProfile, TeachingAssignment
from schools.vs_students.constants import Gender, StudentStatus
from schools.vs_students.models import ClassEnrolment, Student, StudentStatusLog
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
    PERM_CLASS_SUBMIT,
    PERM_HUB_VIEW,
    PERM_MODERATION_APPROVE,
    PERM_SEE_LOCKED_ACCOUNTS,
    PERM_SETUP_UPDATE,
    PERM_SETUP_VIEW,
    PERM_SHEET_ENTER,
    PERM_SHEET_ENTER_ANY,
    PERM_SHEET_REMIND,
    PERM_SHEET_VIEW,
    SetupStatus,
    StoredStage,
)

ADMIN_KEYS = (
    PERM_SETUP_VIEW, PERM_SETUP_UPDATE, PERM_HUB_VIEW, PERM_SHEET_VIEW, PERM_SHEET_REMIND,
)
TEACHER_KEYS = (PERM_SHEET_VIEW, PERM_SHEET_ENTER, PERM_MODERATION_APPROVE, PERM_CLASS_SUBMIT)

#: Monday 13 October 2025, 13:00 in Lagos.
NOW = dt.datetime(2025, 10, 13, 12, 0, tzinfo=dt.timezone.utc)
TODAY = dt.date(2025, 10, 13)

#: ``(name, short name, maximum, is the examination)``.
STANDARD_COMPONENTS = (
    ("1st CA", "CA1", 20, False),
    ("2nd CA", "CA2", 20, False),
    ("Examination", "EXAM", 60, True),
)

#: ``(lowest mark, grade, remark, is a fail)``.
JUNIOR_BANDS = (
    (70, "A", "Excellent", False),
    (60, "B", "Very good", False),
    (50, "C", "Good", False),
    (40, "D", "Fair", False),
    (0, "F", "Fail", True),
)
SENIOR_BANDS = (
    (75, "A1", "Excellent", False),
    (70, "B2", "Very good", False),
    (65, "B3", "Good", False),
    (60, "C4", "Credit", False),
    (55, "C5", "Credit", False),
    (50, "C6", "Credit", False),
    (45, "D7", "Pass", False),
    (40, "E8", "Pass", False),
    (0, "F9", "Fail", True),
)


def school_clock(instant=NOW):
    """Pin the school's clock, the only one a term's "today" is read from."""
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


class GradebookFixture(TestCase):
    """Brightfield: two branches, programmes, classes, teachers and a stranger; and Sunrise."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch", is_main=False)

        cls.admin_role = make_key_role(cls.tenant, "School Admin", "school_admin", ADMIN_KEYS)
        cls.teacher_role = make_key_role(cls.tenant, "Teacher", "teacher", TEACHER_KEYS)
        cls.viewer_role = make_key_role(cls.tenant, "Viewer", "viewer", (PERM_SETUP_VIEW,))
        cls.updater_role = make_key_role(cls.tenant, "Updater", "updater", (PERM_SETUP_UPDATE,))
        cls.nobody_role = make_role(cls.tenant, name="Nobody", key="nobody")
        cls.hub_role = make_key_role(cls.tenant, "Hub Reader", "hub_reader", (PERM_HUB_VIEW,))
        cls.enterer_role = make_key_role(
            cls.tenant, "Enterer", "enterer", (PERM_SHEET_VIEW, PERM_SHEET_ENTER_ANY),
        )
        cls.registrar_role = make_key_role(
            cls.tenant, "Registrar", "registrar", ADMIN_KEYS + (PERM_SEE_LOCKED_ACCOUNTS,),
        )

        cls.admin = cls.make_user("ngozi@brightfield.test", "Ngozi", "Adeyemi", cls.admin_role)
        cls.lekki_admin = cls.make_user(
            "head@lekki.test", "Femi", "Lawal", cls.admin_role, branch=cls.lekki,
        )
        cls.ikeja_admin = cls.make_user(
            "head@ikeja.test", "Ada", "Obi", cls.admin_role, branch=cls.ikeja,
        )
        cls.viewer = cls.make_user("viewer@brightfield.test", "Vera", "Viewer", cls.viewer_role)
        cls.updater = cls.make_user("updater@brightfield.test", "Uche", "Updater", cls.updater_role)
        cls.nobody = cls.make_user("nobody@brightfield.test", "Nobody", "Here", cls.nobody_role)

        cls.hub_reader = cls.make_user("hub@brightfield.test", "Hub", "Reader", cls.hub_role)
        cls.enterer = cls.make_user("enterer@brightfield.test", "Enid", "Enterer", cls.enterer_role)
        cls.registrar = cls.make_user("registrar@brightfield.test", "Rita", "Registrar", cls.registrar_role)

        cls.eze = cls.make_teacher("eze@brightfield.test", "Chidi", "Eze", cls.lekki)
        cls.bello = cls.make_teacher("bello@brightfield.test", "Sade", "Bello", cls.lekki)
        cls.okeke = cls.make_teacher("okeke@brightfield.test", "Ify", "Okeke", cls.lekki)
        cls.adewale = cls.make_teacher("adewale@brightfield.test", "Kunle", "Adewale", cls.lekki)
        cls.musa = cls.make_teacher("musa@brightfield.test", "Garba", "Musa", cls.lekki)

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

        cls.junior = Program.all_objects.create(
            tenant=cls.tenant, name="Junior Secondary", code="JSS", order_index=1,
        )
        cls.senior = Program.all_objects.create(
            tenant=cls.tenant, name="Senior Secondary", code="SSS", order_index=2,
        )
        cls.jss1 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.year, program=cls.junior,
            name="JSS1", code="JSS1", order_index=1,
        )
        cls.sss1 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.year, program=cls.senior,
            name="SSS1", code="SSS1", order_index=1,
        )

        cls.sciences = Department.all_objects.create(
            tenant=cls.tenant, name="Sciences", code="SCI",
        )
        cls.maths = cls.make_subject("Mathematics", "MTH", True, cls.sciences, cls.jss1, cls.sss1)
        cls.french = cls.make_subject("French", "FRN", False, None, cls.jss1)

        cls.jss1a = cls.make_class(cls.jss1, "JSS1 A", "JSS1A", cls.lekki, cls.eze_staff)
        cls.jss1b = cls.make_class(cls.jss1, "JSS1 B", "JSS1B", cls.lekki, cls.bello_staff)
        cls.jss1c = cls.make_class(cls.jss1, "JSS1 C", "JSS1C", cls.ikeja, None)
        cls.sss1a = cls.make_class(cls.sss1, "SSS1 A", "SSS1A", cls.lekki, None)
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
        cls.sade = cls.make_student("Sade", "Ojo", cls.jss1c, "BFS/006", branch=cls.ikeja)
        cls.ife = cls.make_student("Ife", "Adeola", cls.jss1c, "BFS/007", branch=cls.ikeja)

        TeachingAssignment.all_objects.create(
            tenant=cls.tenant, staff=cls.eze_staff, school_class=cls.jss1a,
            subject=cls.maths, session=cls.year, part=TeachingPart.LEAD,
        )
        TeachingAssignment.all_objects.create(
            tenant=cls.tenant, staff=cls.bello_staff, school_class=cls.jss1a,
            subject=cls.maths, session=cls.year, part=TeachingPart.ASSISTANT,
        )
        cls.zainab = cls.make_student("Zainab", "Aliyu", cls.sss1a, "BFS/008")
        cls.femi = cls.make_student("Femi", "Balogun", cls.sss1a, "BFS/009")
        TeachingAssignment.all_objects.create(
            tenant=cls.tenant, staff=cls.adewale_staff, school_class=cls.sss1a,
            subject=cls.maths, session=cls.year, part=TeachingPart.LEAD,
        )

        # Sunrise: one branch, and the other school in every isolation test.
        cls.solo_school = make_school(slug="sunrise", name="Sunrise Academy")
        cls.solo = cls.solo_school.tenant
        cls.solo_branch = make_branch(cls.solo_school, name="Main Branch", is_main=True)
        solo_admin_role = make_key_role(cls.solo, "School Admin", "school_admin", ADMIN_KEYS)
        solo_teacher_role = make_key_role(cls.solo, "Teacher", "teacher", TEACHER_KEYS)
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
        cls.solo_program = Program.all_objects.create(tenant=cls.solo, name="Basic", code="BAS")
        cls.solo_level = Level.all_objects.create(
            tenant=cls.solo, session=cls.solo_year, program=cls.solo_program,
            name="Basic 3", code="BAS3", order_index=1,
        )
        cls.solo_class = SchoolClass.all_objects.create(
            tenant=cls.solo, session=cls.solo_year, level=cls.solo_level,
            name="Basic 3", code="BAS3A", branch=None, class_teacher=cls.solo_staff,
        )
        cls.solo_maths = Subject.all_objects.create(
            tenant=cls.solo, name="Mathematics", code="MTH", is_core=True,
        )
        SubjectOffering.all_objects.create(
            tenant=cls.solo, subject=cls.solo_maths, level=cls.solo_level,
        )
        TeachingAssignment.all_objects.create(
            tenant=cls.solo, staff=cls.solo_staff, school_class=cls.solo_class,
            subject=cls.solo_maths, session=cls.solo_year, part=TeachingPart.LEAD,
        )
        cls.solo_pupil = cls.make_student(
            "Ngozi", "Eke", cls.solo_class, "SUN/001", tenant=cls.solo, branch=cls.solo_branch,
        )
        cls.solo_pupil_two = cls.make_student(
            "Tobi", "Ojo", cls.solo_class, "SUN/002", tenant=cls.solo, branch=cls.solo_branch,
        )

    # ── builders ──────────────────────────────────────────────────────────

    @classmethod
    def make_user(cls, email, first, last, role, *, branch=None, tenant=None):
        """A staff account holding *role*, school-wide unless pinned to *branch*."""
        tenant = tenant or cls.tenant
        user = make_school_admin(
            branch, email=email, first_name=first, last_name=last,
            **({"tenant": tenant} if branch is None else {}),
        )
        make_assignment(tenant, user, role, branch=branch)
        return user

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
    def make_subject(cls, name, code, is_core, department, *levels):
        subject = Subject.all_objects.create(
            tenant=cls.tenant, name=name, code=code, is_core=is_core, department=department,
        )
        for level in levels:
            SubjectOffering.all_objects.create(
                tenant=cls.tenant, subject=subject, level=level,
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

    @classmethod
    def make_structure(
        cls, program, *, branch=None, status=SetupStatus.IN_USE, name="Standard",
        components=STANDARD_COMPONENTS, tenant=None, copied_from=None,
    ):
        """An assessment structure with its components, written straight to the tables."""
        tenant = tenant or cls.tenant
        structure = AssessmentStructure.all_objects.create(
            tenant=tenant, program=program, branch=branch, name=name, status=status,
            copied_from=copied_from,
        )
        for order, (cname, short, maximum, is_exam) in enumerate(components, start=1):
            AssessmentComponent.all_objects.create(
                tenant=tenant, structure=structure, name=cname, short_name=short,
                max_score=maximum, is_exam=is_exam, order=order,
            )
        return structure

    @classmethod
    def make_scale(
        cls, program, *, branch=None, status=SetupStatus.IN_USE, name="Standard",
        bands=JUNIOR_BANDS, tenant=None,
    ):
        """A grading scale with its bands, written straight to the tables."""
        tenant = tenant or cls.tenant
        scale = GradingScale.all_objects.create(
            tenant=tenant, program=program, branch=branch, name=name, status=status,
        )
        for order, (low, grade, remark, is_fail) in enumerate(bands, start=1):
            GradeBand.all_objects.create(
                tenant=tenant, scale=scale, min_score=low, grade=grade, remark=remark,
                is_fail=is_fail, order=order,
            )
        return scale

    @classmethod
    def make_class_term(cls, school_class, term=None, *, stage=None, structure=None, branch=None):
        """A class's term row, on the class's branch unless told otherwise."""
        return ClassTerm.all_objects.create(
            tenant=school_class.tenant, school_class=school_class,
            term=term or cls.first_term, branch=branch or school_class.branch or cls.lekki,
            structure=structure, stage=stage,
        )

    @classmethod
    def publish(cls, school_class, structure, term=None):
        """A class-term published with *structure*."""
        return cls.make_class_term(
            school_class, term, stage=StoredStage.PUBLISHED, structure=structure,
        )

    @classmethod
    def set_head(cls, department, staff):
        Department.all_objects.filter(pk=department.pk).update(head=staff)
        department.refresh_from_db()

    # ── the clock ─────────────────────────────────────────────────────────

    def setUp(self):
        clock = school_clock()
        clock.start()
        self.addCleanup(clock.stop)

    # ── HTTP ──────────────────────────────────────────────────────────────

    def get(self, user, name, query=None, **kwargs):
        return TenantAPIClient(user).get(reverse(name, kwargs=kwargs), query)

    def send(self, verb, user, name, body=None, **kwargs):
        client = TenantAPIClient(user)
        return getattr(client, verb)(reverse(name, kwargs=kwargs), body or {}, format="json")

    def post(self, user, name, body=None, **kwargs):
        return self.send("post", user, name, body, **kwargs)

    def patch(self, user, name, body=None, **kwargs):
        return self.send("patch", user, name, body, **kwargs)

    def put(self, user, name, body=None, **kwargs):
        return self.send("put", user, name, body, **kwargs)

    def delete(self, user, name, **kwargs):
        return TenantAPIClient(user).delete(reverse(name, kwargs=kwargs))

    @staticmethod
    def data(response):
        return response.data["data"]

    @staticmethod
    def code(response):
        return response.data["error"]["code"]

    @staticmethod
    def message(response):
        return response.data["message"]

    @staticmethod
    def components_body(rows=STANDARD_COMPONENTS):
        """A request's component rows from ``(name, short, max, is_exam)`` tuples."""
        return [
            {"name": name, "short_name": short, "max_score": maximum, "is_exam": is_exam}
            for name, short, maximum, is_exam in rows
        ]

    @staticmethod
    def bands_body(rows=JUNIOR_BANDS):
        """A request's band rows from ``(lowest, grade, remark, is_fail)`` tuples."""
        return [
            {"min_score": low, "grade": grade, "remark": remark, "is_fail": is_fail}
            for low, grade, remark, is_fail in rows
        ]
