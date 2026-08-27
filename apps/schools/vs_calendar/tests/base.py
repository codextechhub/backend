"""The two shapes of school every test in this module runs against.

Brightfield has two branches and Sunrise has one, and half the rules here only
exist in one of them: a single-branch test proves nothing about a multi-branch
school and the reverse is just as true.
"""
from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    Level,
    Program,
    SchoolClass,
    Subject,
)
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

from ..models import Period, PeriodType, Room, RoomType

CALENDAR_KEYS = (
    "academics.calendar.view",
    "academics.calendar.create",
    "academics.calendar.update",
    "academics.calendar.manage",
)
TIMETABLE_KEYS = (
    "academics.timetable.view",
    "academics.timetable.create",
    "academics.timetable.update",
    "academics.timetable.manage",
    "academics.timetable.publish",
)
ALL_KEYS = CALENDAR_KEYS + TIMETABLE_KEYS


class _Base(TestCase):
    """Brightfield: two branches, one active year, three terms."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch", is_main=False)

        cls.role = make_role(cls.school, name="School Admin", key="school_admin")
        cls.perms = {}
        for key in ALL_KEYS:
            cls.perms[key] = make_permission(key, scope=PermissionScope.TENANT)
            make_role_permission(cls.role, cls.perms[key])

        cls.admin = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, cls.role, branch=None)

        # A branch admin, pinned to Ikeja. Every redaction test uses them.
        cls.ikeja_admin = make_school_admin(
            None, email="head@ikeja.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.ikeja_admin, cls.role, branch=cls.ikeja)

        cls.year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2025/2026",
            start_date=dt.date(2025, 9, 8), end_date=dt.date(2026, 7, 17),
            status="ACTIVE",
        )
        cls.first_term = AcademicTerm.all_objects.create(
            tenant=cls.tenant, session=cls.year, name="First Term",
            order_index=1,
            start_date=dt.date(2025, 9, 8), end_date=dt.date(2025, 12, 12),
        )
        cls.second_term = AcademicTerm.all_objects.create(
            tenant=cls.tenant, session=cls.year, name="Second Term",
            order_index=2,
            start_date=dt.date(2026, 1, 6), end_date=dt.date(2026, 4, 2),
        )

        cls.program = Program.all_objects.create(
            tenant=cls.tenant, name="Junior Secondary", code="JSS",
        )
        cls.jss1 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.year, program=cls.program,
            name="JSS1", code="JSS1", order_index=1,
        )
        cls.primary4 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.year, program=cls.program,
            name="Primary 4", code="PRY4", order_index=2,
        )

        # Two Lekki classes, one Ikeja class, one school-wide.
        cls.jss1a = SchoolClass.all_objects.create(
            tenant=cls.tenant, session=cls.year, level=cls.jss1,
            name="JSS1 A", code="JSS1A", branch=cls.lekki,
        )
        cls.jss1b = SchoolClass.all_objects.create(
            tenant=cls.tenant, session=cls.year, level=cls.jss1,
            name="JSS1 B", code="JSS1B", branch=cls.lekki,
        )
        cls.sss2 = SchoolClass.all_objects.create(
            tenant=cls.tenant, session=cls.year, level=cls.jss1,
            name="SSS2 Science", code="SSS2S", branch=cls.ikeja,
        )
        cls.pry4a = SchoolClass.all_objects.create(
            tenant=cls.tenant, session=cls.year, level=cls.primary4,
            name="Primary 4 A", code="PRY4A", branch=None,
        )

        # A subject is catalogue, not a thing that happens each year, so it
        # carries no session.
        cls.maths = Subject.all_objects.create(
            tenant=cls.tenant, name="Mathematics", code="MTH",
        )
        cls.physics = Subject.all_objects.create(
            tenant=cls.tenant, name="Physics", code="PHY",
        )

        # Rooms: two at Lekki, one at Ikeja.
        cls.room_a1 = Room.all_objects.create(
            tenant=cls.tenant, branch=cls.lekki, name="Block A Room 1",
            code="A-1", room_type=RoomType.CLASSROOM, capacity=40,
        )
        cls.room_a2 = Room.all_objects.create(
            tenant=cls.tenant, branch=cls.lekki, name="Block A Room 2",
            code="A-2", room_type=RoomType.CLASSROOM, capacity=40,
        )
        cls.room_c1 = Room.all_objects.create(
            tenant=cls.tenant, branch=cls.ikeja, name="Block C Room 1",
            code="C-1", room_type=RoomType.CLASSROOM, capacity=35,
        )

        # A school-wide everyday bell schedule: two lessons and a break.
        cls.p1 = Period.all_objects.create(
            tenant=cls.tenant, session=cls.year, order_index=1, label="Period 1",
            period_type=PeriodType.LESSON,
            start_time=dt.time(8, 0), end_time=dt.time(8, 45),
        )
        cls.p2 = Period.all_objects.create(
            tenant=cls.tenant, session=cls.year, order_index=2, label="Period 2",
            period_type=PeriodType.LESSON,
            start_time=dt.time(8, 45), end_time=dt.time(9, 30),
        )
        cls.brk = Period.all_objects.create(
            tenant=cls.tenant, session=cls.year, order_index=3, label="Break",
            period_type=PeriodType.BREAK,
            start_time=dt.time(9, 30), end_time=dt.time(10, 0),
        )

        # Another school entirely, for the isolation tests.
        cls.other = make_school(slug="sunrise", name="Sunrise Academy")
        cls.other_branch = make_branch(cls.other, name="Main Branch", is_main=True)
        cls.other_year = AcademicSession.all_objects.create(
            tenant=cls.other.tenant, name="2025/2026",
            start_date=dt.date(2025, 9, 15), end_date=dt.date(2026, 7, 24),
            status="ACTIVE",
        )

    # ── teachers ──────────────────────────────────────────────────────────

    @classmethod
    def make_teacher(cls, email, first, last, branch=None):
        """A user carrying the `teacher` role, which is what a teacher IS here.

        Not a persona column: `user_type` was dropped by vs_user migration 0009
        and this module identifies a teacher by role grant instead.
        """
        from vs_rbac.models import TenantRoleTemplate

        # One role per school, not one per teacher: the key is unique per
        # tenant, and this helper is called several times in a test.
        role = TenantRoleTemplate.objects.filter(
            tenant=cls.tenant, key="teacher",
        ).first() or make_role(cls.school, name="Teacher", key="teacher")
        user = make_school_admin(None, email=email, tenant=cls.tenant)
        user.first_name, user.last_name = first, last
        user.save(update_fields=["first_name", "last_name"])
        make_assignment(cls.school, user, role, branch=branch)
        return user

    # ── HTTP ──────────────────────────────────────────────────────────────

    def client_for(self, user):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
        )
        return client

    def get(self, user, name, params=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).get(
            url, {"tenant": self.tenant.slug, **(params or {})},
        )

    def post(self, user, name, body=None, params=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        query = "&".join(
            f"{k}={v}" for k, v in {"tenant": self.tenant.slug, **(params or {})}.items()
        )
        return self.client_for(user).post(
            f"{url}?{query}", body or {}, format="json",
        )

    def patch(self, user, name, body=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).patch(
            f"{url}?tenant={self.tenant.slug}", body or {}, format="json",
        )

    def delete(self, user, name, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).delete(f"{url}?tenant={self.tenant.slug}")

    def put(self, user, name, body=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).put(
            f"{url}?tenant={self.tenant.slug}", body or {}, format="json",
        )


class _SingleBranchBase(TestCase):
    """Sunrise: one branch, and the whole branch dimension should recede."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="sunrise", name="Sunrise Academy")
        cls.tenant = cls.school.tenant
        cls.branch = make_branch(cls.school, name="Main Branch", is_main=True)

        cls.role = make_role(cls.school, name="School Admin", key="school_admin")
        for key in ALL_KEYS:
            make_role_permission(
                cls.role, make_permission(key, scope=PermissionScope.TENANT),
            )
        cls.admin = make_school_admin(
            None, email="ikenna@sunrise.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, cls.role, branch=None)

        cls.year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2025/2026",
            start_date=dt.date(2025, 9, 15), end_date=dt.date(2026, 7, 24),
            status="ACTIVE",
        )
        AcademicTerm.all_objects.create(
            tenant=cls.tenant, session=cls.year, name="First Term",
            order_index=1,
            start_date=dt.date(2025, 9, 15), end_date=dt.date(2025, 12, 19),
        )

    def client_for(self, user):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
        )
        return client

    def get(self, user, name, params=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).get(
            url, {"tenant": self.tenant.slug, **(params or {})},
        )

    def post(self, user, name, body=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).post(
            f"{url}?tenant={self.tenant.slug}", body or {}, format="json",
        )
