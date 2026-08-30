"""The fixture every test in this module builds on.

**Two shapes of school, always.** A single-branch test proves nothing about a
multi-branch one, and the branch rules are where this module is most likely to
be wrong: Brightfield has two branches so the dimension is live, and Sunrise
has one so the recede rule is exercised. A third school exists purely to be
another tenant, because a cross-tenant test with one tenant proves nothing
either.
"""
from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from schools.vs_academics.models import (
    AcademicSession,
    Level,
    Program,
    SchoolClass,
)
from schools.vs_students.constants import Gender, Relationship, StudentStatus
from schools.vs_students.models import Guardian, Student, StudentGuardian
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

STUDENT_KEYS = (
    "school.students.view",
    "school.students.create",
    "school.students.update",
    "school.students.manage",
    "school.students.view_sensitive",
    "school.students.import",
    "school.students.export",
)
CLASS_KEYS = ("academics.classes.assign", "academics.classes.view")
ALL_KEYS = STUDENT_KEYS + CLASS_KEYS


class StudentsFixture(TestCase):
    """A two-branch school, a one-branch school, and a stranger."""

    @classmethod
    def setUpTestData(cls):
        # The admission-number policy lives in vs_config, so its definitions
        # have to exist before any test can set one. Without this the write
        # silently stored nothing and every policy test passed by never
        # reaching the rule it was checking.
        call_command("seed_config_catalogue", verbosity=0)

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

        cls.admin = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, cls.role, branch=None)

        # Pinned to Lekki. Every branch-isolation test runs as this person.
        cls.lekki_head = make_school_admin(
            None, email="head@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.lekki_head, cls.role, branch=cls.lekki)

        # Holds nothing. Every 403 test runs as this person.
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
        cls.next_year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2026/2027",
            start_date=dt.date(2026, 9, 1), end_date=dt.date(2027, 7, 31),
            status="DRAFT",
        )
        # Programmes are not per-year - only levels, classes and subjects are.
        cls.program = Program.all_objects.create(
            tenant=cls.tenant, name="Junior Secondary", code="JSS",
        )
        cls.jss2 = Level.all_objects.create(
            tenant=cls.tenant, program=cls.program, session=cls.year,
            name="JSS2", code="JSS2", order_index=2,
        )
        cls.jss1 = Level.all_objects.create(
            tenant=cls.tenant, program=cls.program, session=cls.year,
            name="JSS1", code="JSS1", order_index=1, next_level=cls.jss2,
        )
        # A school-wide class and one bound to each branch, so containment can
        # be tested in all three directions.
        cls.shared_class = SchoolClass.all_objects.create(
            tenant=cls.tenant, level=cls.jss1, session=cls.year,
            name="JSS1 A", code="JSS1A", arm="A", capacity=30, branch=None,
        )
        cls.lekki_class = SchoolClass.all_objects.create(
            tenant=cls.tenant, level=cls.jss1, session=cls.year,
            name="JSS1 B", code="JSS1B", arm="B", capacity=2, branch=cls.lekki,
        )
        cls.ikeja_class = SchoolClass.all_objects.create(
            tenant=cls.tenant, level=cls.jss1, session=cls.year,
            name="JSS1 C", code="JSS1C", arm="C", capacity=30, branch=cls.ikeja,
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
        cls.solo_year = AcademicSession.all_objects.create(
            tenant=cls.solo.tenant, name="2025/2026",
            start_date=dt.date(2025, 9, 1), end_date=dt.date(2026, 7, 31),
            status="ACTIVE",
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

    def delete(self, user, name, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).delete(
            f"{url}?tenant={self._slug(user)}",
        )

    def put(self, user, name, body=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).put(
            f"{url}?tenant={self._slug(user)}", body or {}, format="json",
        )

    # ── data helpers ───────────────────────────────────────────────────────

    def student(self, *, branch=None, tenant=None, status=StudentStatus.ACTIVE,
                first="Chiamaka", last="Nwosu", number="", dob=None, **extra):
        return Student.all_objects.create(
            tenant=tenant or self.tenant, branch=branch or self.lekki,
            first_name=first, last_name=last, student_number=number,
            date_of_birth=dob or dt.date(2013, 4, 18),
            gender=Gender.FEMALE, status=status, **extra,
        )

    def guardian(self, *, tenant=None, name="Mr. Chukwudi Nwosu",
                 phone="08035550101", email="chukwudi@example.ng"):
        return Guardian.all_objects.create(
            tenant=tenant or self.tenant, full_name=name, phone=phone,
            email=email,
        )

    def link(self, student, guardian, *, primary=True,
             relationship=Relationship.FATHER):
        return StudentGuardian.all_objects.create(
            tenant=student.tenant, student=student, guardian=guardian,
            relationship=relationship, is_primary=primary,
        )

    def place(self, student, school_class=None, *, session=None):
        from schools.vs_students.models import ClassEnrolment

        return ClassEnrolment.all_objects.create(
            tenant=student.tenant, student=student,
            school_class=school_class or self.shared_class,
            session=session or self.year, is_active=True,
        )

    def enrolment_body(self, **overrides):
        body = {
            "first_name": "Zainab", "last_name": "Yusuf",
            "date_of_birth": "2013-11-30", "gender": Gender.FEMALE,
            "school_class": self.shared_class.pk,
            # Branch references are ids across the platform, never names:
            # find_branch_in_tenant refuses anything that is not a plausible
            # integer, deliberately, so the parameter cannot be used to
            # discover which branches exist elsewhere.
            "branch": str(self.lekki.pk),
            "guardians": [{
                "full_name": "Mrs. Amina Yusuf", "phone": "08115550177",
                "email": "amina.yusuf@example.ng",
                "relationship": Relationship.MOTHER, "is_primary": True,
            }],
        }
        body.update(overrides)
        return body
