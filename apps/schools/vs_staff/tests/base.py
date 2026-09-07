"""The fixture every test in this module builds on.

**Two shapes of school, always.** A single-branch test proves nothing about a
multi-branch one, and the branch rules are where this module is most likely to
be wrong: Brightfield has two branches so the dimension is live, and Sunrise has
one so the recede rule is exercised. A third school exists purely to be another
tenant, because a cross-tenant test with one tenant proves nothing either.

**Four callers, and each exists for a different refusal.** The school admin
holds everything. The Lekki head is pinned to one branch, and every isolation
test runs as her. The teacher holds the teacher defaults and nothing more, which
is what the self-service rules are checked against. And the stranger belongs to
another school entirely, which is the only way to prove a 404 is a 404 rather
than a 403 wearing a different number.
"""
from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from schools.vs_academics.models import (
    AcademicSession,
    Level,
    Program,
    SchoolClass,
    Subject,
    SubjectOffering,
)
from schools.vs_staff.constants import EmploymentStatus
from schools.vs_staff.models import StaffProfile
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
from vs_user.tokens import CodeXRefreshToken

STAFF_KEYS = (
    "school.teachers.view",
    "school.teachers.create",
    "school.teachers.update",
    "school.teachers.manage",
    "school.teachers.assign",
)
#: Qualifications, certificates and documents. Split out of the register keys
#: so the two can be sold at different depths; granted here alongside them, the
#: way the migration carried every existing register grant across.
RECORD_KEYS = ("school.staff_records.view", "school.staff_records.update")
LEAVE_KEYS = ("school.leave.apply", "school.leave.view", "school.leave.manage")
ACCOUNT_KEYS = (
    "school.administrators.create",
    "school.administrators.update",
    "school.administrators.suspend",
    "school.administrators.reactivate",
)
#: Keys other modules own that this one uses, and that a real school_admin
#: holds. ``constants.py`` names all four for the same reason: this module reads
#: classes and subjects to write a teaching duty, assigns roles it does not own,
#: and renders per-user overrides it does not manage.
#:
#: ``academics.classes.view`` is here so the class-teacher designation can be
#: read back from the screen that shows it. Without it a test of that round trip
#: proves only that this fixture grants no academics key.
OTHER_KEYS = (
    "school.roles.assign",
    "school.user_overrides.view",
    "academics.classes.view",
)
ALL_KEYS = STAFF_KEYS + RECORD_KEYS + LEAVE_KEYS + ACCOUNT_KEYS + OTHER_KEYS

#: What a teacher actually holds, per the seeder's own defaults. Kept in step
#: with ``seed_school_permissions`` on purpose: a test that granted a teacher
#: more than a teacher has would prove nothing about the real refusals.
TEACHER_KEYS = ("school.teachers.view", "school.leave.apply")


class StaffFixture(TestCase):
    """A two-branch school, a one-branch school, and a stranger."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja", is_main=False)

        cls.permissions = {
            key: make_permission(key, scope=PermissionScope.TENANT)
            for key in ALL_KEYS
        }

        cls.role = make_role(cls.school, name="School Admin", key="school_admin")
        for key in ALL_KEYS:
            make_role_permission(cls.role, cls.permissions[key])

        cls.teacher_role = make_role(cls.school, name="Teacher", key="teacher")
        for key in TEACHER_KEYS:
            make_role_permission(cls.teacher_role, cls.permissions[key])

        cls.admin = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, cls.role, branch=None)

        # Pinned to Lekki. Every branch-isolation test runs as this person.
        cls.lekki_head = make_school_admin(
            None, email="head@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.lekki_head, cls.role, branch=cls.lekki)

        # Holds nothing at all. Every 403 test runs as this person.
        cls.nobody_role = make_role(cls.school, name="Nobody", key="nobody")
        cls.nobody = make_school_admin(
            None, email="nobody@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.nobody, cls.nobody_role, branch=None)

        cls.year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2025/2026",
            start_date=dt.date(2025, 9, 1), end_date=dt.date(2026, 7, 31),
            status="ACTIVE",
        )
        cls.archived_year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2024/2025",
            start_date=dt.date(2024, 9, 1), end_date=dt.date(2025, 7, 31),
            status="ARCHIVED",
        )
        cls.program = Program.all_objects.create(
            tenant=cls.tenant, name="Junior Secondary", code="JSS",
        )
        cls.jss1 = Level.all_objects.create(
            tenant=cls.tenant, program=cls.program, session=cls.year,
            name="JSS1", code="JSS1", order_index=1,
        )
        cls.shared_class = SchoolClass.all_objects.create(
            tenant=cls.tenant, level=cls.jss1, session=cls.year,
            name="JSS1 A", code="JSS1A", arm="A", branch=None,
        )
        cls.lekki_class = SchoolClass.all_objects.create(
            tenant=cls.tenant, level=cls.jss1, session=cls.year,
            name="JSS1 B", code="JSS1B", arm="B", branch=cls.lekki,
        )
        cls.ikeja_class = SchoolClass.all_objects.create(
            tenant=cls.tenant, level=cls.jss1, session=cls.year,
            name="JSS1 C", code="JSS1C", arm="C", branch=cls.ikeja,
        )
        cls.maths = Subject.all_objects.create(
            tenant=cls.tenant, name="Mathematics", code="MTH",
        )
        cls.english = Subject.all_objects.create(
            tenant=cls.tenant, name="English Language", code="ENG",
        )
        for subject in (cls.maths, cls.english):
            SubjectOffering.all_objects.create(
                tenant=cls.tenant, subject=subject, level=cls.jss1,
            )

        # ── The people ────────────────────────────────────────────────────
        cls.eze = cls.make_staff(
            "eze@brightfield.test", "Chukwuemeka", "Eze", branch=cls.lekki,
            job_title="Lead Teacher", staff_number="BFS/STF/0012",
            role=cls.teacher_role,
        )
        cls.ikeja_teacher = cls.make_staff(
            "sule@brightfield.test", "Ibrahim", "Sule", branch=cls.ikeja,
            job_title="Teacher", role=cls.teacher_role,
        )
        # No posting: across the whole school, which is a first-class value and
        # the reason the narrowing here is inclusive.
        cls.registrar = cls.make_staff(
            "nwankwo@brightfield.test", "Adaeze", "Nwankwo", branch=None,
            job_title="Registrar",
        )

        # A second school with exactly ONE branch, so the recede rule is real.
        cls.solo = make_school(slug="sunrise", name="Sunrise Academy")
        cls.solo_branch = make_branch(cls.solo, name="Main", is_main=True)
        cls.solo_role = make_role(cls.solo, name="School Admin", key="school_admin")
        for key in ALL_KEYS:
            make_role_permission(cls.solo_role, cls.permissions[key])
        cls.solo_admin = make_school_admin(
            None, email="head@sunrise.test", tenant=cls.solo.tenant,
        )
        make_assignment(cls.solo, cls.solo_admin, cls.solo_role, branch=None)
        cls.solo_staff = cls.make_staff(
            "teacher@sunrise.test", "Bola", "Ade", branch=cls.solo_branch,
            tenant=cls.solo.tenant, school=cls.solo,
        )

    # ── builders ───────────────────────────────────────────────────────────

    @classmethod
    def make_staff(cls, email, first, last, *, branch=None, job_title="Teacher",
                   staff_number="", role=None, tenant=None, school=None,
                   status=EmploymentStatus.ACTIVE):
        tenant = tenant or cls.tenant
        user = make_school_admin(None, email=email, tenant=tenant)
        user.first_name = first
        user.last_name = last
        user.branch = branch
        user.save(update_fields=["first_name", "last_name", "branch"])
        if role is not None:
            make_assignment(school or cls.school, user, role, branch=branch)
        return StaffProfile.all_objects.create(
            tenant=tenant, user=user, branch=branch, job_title=job_title,
            staff_number=staff_number, employment_status=status,
            hire_date=dt.date(2021, 9, 6),
        )

    # ── HTTP helpers ───────────────────────────────────────────────────────

    def client_for(self, user):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
        )
        return client

    def _slug(self, user):
        return getattr(user.tenant, "slug", self.tenant.slug)

    def get(self, user, name, params=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).get(
            url, {"tenant": self._slug(user), **(params or {})},
        )

    def post(self, user, name, body=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).post(
            f"{url}?tenant={self._slug(user)}", body or {}, format="json",
        )

    def patch(self, user, name, body=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).patch(
            f"{url}?tenant={self._slug(user)}", body or {}, format="json",
        )

    def put(self, user, name, body=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).put(
            f"{url}?tenant={self._slug(user)}", body or {}, format="json",
        )

    def delete(self, user, name, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).delete(f"{url}?tenant={self._slug(user)}")

    # ── data helpers ───────────────────────────────────────────────────────

    def invite_body(self, **overrides):
        """The minimum a school types to add somebody, plus whatever a test varies."""
        body = {
            "first_name": "Funke",
            "last_name": "Adeyemi",
            "email": "funke@brightfield.test",
            "role": "teacher",
            "job_title": "Bursar",
            "staff_number": "BFS/STF/0003",
        }
        body.update(overrides)
        return body
