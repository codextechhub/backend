"""Starting a year from the one before it, and reading one year at a time.

Two halves of the same decision. Levels, classes and subjects belong to a year,
which makes last year's register an honest record instead of something this
year's edits overwrite - but it also means a school that creates 2100/2101
would face every level, class and subject again from scratch. So a year is
seeded from another year, and every read says which year it is answering about.

The rules worth pinning are the ones a careless copy would get wrong: promotion
links that still point at LAST year's levels, archived rows quietly revived, and
a second copy doubling a year that was already started.
"""
from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

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
from schools.vs_academics.models import (
    AcademicSession,
    Level,
    Program,
    SchoolClass,
    SessionStatus,
    Subject,
    SubjectOffering,
)
from schools.vs_academics.services.rollover import (
    NothingToCopy,
    TargetYearNotEmpty,
    roll_forward,
)

KEYS = (
    "academics.structure.view",
    "academics.structure.create",
    "academics.structure.update",
    "academics.structure.manage",
    "academics.classes.view",
    "academics.classes.create",
    "academics.classes.update",
    "academics.classes.manage",
    "academics.subject.view",
    "academics.subject.create",
    "academics.subject.update",
    "academics.subject.manage",
)


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Campus", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Campus", is_main=False)

        cls.role = make_role(cls.school, name="School Admin", key="school_admin")
        for key in KEYS:
            make_role_permission(
                cls.role, make_permission(key, scope=PermissionScope.TENANT),
            )
        cls.admin = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, cls.role, branch=None)

        cls.other = make_school(slug="sunrise", name="Sunrise Academy")
        make_branch(cls.other, name="Main", is_main=True)
        cls.other_year = AcademicSession.all_objects.create(
            tenant=cls.other.tenant, name="2099/2100",
            start_date=dt.date(2099, 9, 1), end_date=dt.date(2100, 7, 31),
            status=SessionStatus.ACTIVE,
        )

        # This year, live, and next year, drafted but empty - the state a school
        # is in on the day it wants to roll forward.
        cls.this_year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2099/2100",
            start_date=dt.date(2099, 9, 1), end_date=dt.date(2100, 7, 31),
            status=SessionStatus.ACTIVE,
        )
        cls.next_year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2100/2101",
            start_date=dt.date(2100, 9, 1), end_date=dt.date(2101, 7, 31),
            status=SessionStatus.DRAFT,
        )

        cls.prog = Program.all_objects.create(
            tenant=cls.tenant, name="Junior Secondary", code="JSS",
        )

    @classmethod
    def build_this_year(cls):
        """One programme, two levels that promote, a class, and a subject."""
        cls.jss1 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.this_year, program=cls.prog,
            name="JSS1", code="JSS1", order_index=1,
        )
        cls.jss2 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.this_year, program=cls.prog,
            name="JSS2", code="JSS2", order_index=2,
        )
        cls.jss1.next_level = cls.jss2
        cls.jss1.save(update_fields=["next_level", "updated_at"])

        cls.jss1a = SchoolClass.all_objects.create(
            tenant=cls.tenant, session=cls.this_year, level=cls.jss1,
            branch=cls.lekki, name="JSS1 A", code="JSS1-A", arm="A", capacity=35,
        )
        cls.maths = Subject.all_objects.create(
            tenant=cls.tenant, session=cls.this_year, name="Mathematics",
            code="MTH", is_core=True,
        )
        SubjectOffering.all_objects.create(
            tenant=cls.tenant, subject=cls.maths, level=cls.jss1, is_core=True,
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

    def post(self, user, name, body, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).post(
            f"{url}?tenant={self.tenant.slug}", body, format="json",
        )


class RollForwardTests(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_this_year()

    def roll(self):
        return roll_forward(
            self.tenant, source=self.this_year, target=self.next_year,
        )

    def in_next(self, model):
        return model.all_objects.filter(tenant=self.tenant, session=self.next_year)

    def test_the_structure_arrives_in_the_new_year(self):
        written = self.roll()
        self.assertEqual(written, {"levels": 2, "classes": 1, "subjects": 1})
        self.assertEqual(
            sorted(self.in_next(Level).values_list("name", flat=True)),
            ["JSS1", "JSS2"],
        )
        self.assertEqual(self.in_next(SchoolClass).count(), 1)
        self.assertEqual(self.in_next(Subject).count(), 1)

    def test_last_years_rows_are_left_exactly_as_they_were(self):
        """The point of the whole change: history is not edited by a rollover."""
        self.roll()
        self.assertEqual(
            Level.all_objects.filter(
                tenant=self.tenant, session=self.this_year,
            ).count(), 2,
        )
        self.jss1.refresh_from_db()
        self.assertEqual(self.jss1.next_level_id, self.jss2.pk)

    def test_promotion_points_at_the_new_years_level_not_last_years(self):
        """The copy a careless implementation gets wrong.

        Copying `next_level` verbatim leaves the new JSS1 promoting into LAST
        year's JSS2, so promoting a pupil moves them backwards a year.
        """
        self.roll()
        new_jss1 = self.in_next(Level).get(name="JSS1")
        new_jss2 = self.in_next(Level).get(name="JSS2")
        self.assertEqual(new_jss1.next_level_id, new_jss2.pk)
        self.assertNotEqual(new_jss1.next_level_id, self.jss2.pk)

    def test_a_class_keeps_its_branch_arm_and_capacity(self):
        self.roll()
        copied = self.in_next(SchoolClass).get()
        self.assertEqual(copied.branch_id, self.lekki.id)
        self.assertEqual(copied.arm, "A")
        self.assertEqual(copied.capacity, 35)
        self.assertEqual(copied.level.session_id, self.next_year.pk)

    def test_an_offering_follows_the_new_subject_and_the_new_level(self):
        self.roll()
        offering = SubjectOffering.all_objects.get(
            subject__session=self.next_year,
        )
        self.assertEqual(offering.subject.name, "Mathematics")
        self.assertEqual(offering.level.session_id, self.next_year.pk)
        self.assertTrue(offering.is_core)

    def test_an_archived_level_and_its_class_stay_behind(self):
        """Withdrawing a level was a decision; a copy must not undo it."""
        old = Level.all_objects.create(
            tenant=self.tenant, session=self.this_year, program=self.prog,
            name="JSS4", code="JSS4", order_index=4, is_active=False,
        )
        SchoolClass.all_objects.create(
            tenant=self.tenant, session=self.this_year, level=old,
            name="JSS4 A", code="JSS4-A", is_active=False,
        )
        written = self.roll()
        self.assertEqual(written["levels"], 2)
        self.assertFalse(self.in_next(Level).filter(name="JSS4").exists())
        self.assertEqual(self.in_next(SchoolClass).count(), 1)

    def test_a_second_copy_is_refused_rather_than_doubling_the_year(self):
        self.roll()
        with self.assertRaises(TargetYearNotEmpty) as caught:
            self.roll()
        self.assertIn("2100/2101", str(caught.exception))
        self.assertEqual(self.in_next(Level).count(), 2)

    def test_copying_a_year_into_itself_is_refused(self):
        with self.assertRaises(NothingToCopy):
            roll_forward(
                self.tenant, source=self.this_year, target=self.this_year,
            )

    def test_an_empty_source_year_says_there_is_nothing_to_copy(self):
        empty = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2098/2099",
            start_date=dt.date(2098, 9, 1), end_date=dt.date(2099, 7, 31),
            status=SessionStatus.ARCHIVED,
        )
        with self.assertRaises(NothingToCopy):
            roll_forward(self.tenant, source=empty, target=self.next_year)


class RollForwardEndpointTests(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_this_year()

    def roll(self, user=None, body=None, pk=None):
        return self.post(
            user or self.admin, "academics-session-roll-forward",
            body if body is not None else {"from": self.this_year.pk},
            pk=pk or self.next_year.pk,
        )

    def test_the_copy_runs_and_reports_what_it_wrote(self):
        response = self.roll()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["data"], {"levels": 2, "classes": 1, "subjects": 1},
        )

    def test_a_caller_without_the_key_is_refused(self):
        stranger = make_school_admin(
            None, email="nobody@brightfield.test", tenant=self.tenant,
        )
        self.assertEqual(self.roll(user=stranger).status_code, 403)
        self.assertFalse(
            Level.all_objects.filter(session=self.next_year).exists(),
        )

    def test_the_year_to_copy_from_must_be_named(self):
        response = self.roll(body={})
        self.assertEqual(response.status_code, 400, response.data)

    def test_another_schools_year_cannot_be_copied_from(self):
        response = self.roll(body={"from": self.other_year.pk})
        self.assertEqual(response.status_code, 404, response.data)

    def test_another_schools_year_cannot_be_copied_into(self):
        response = self.roll(pk=self.other_year.pk)
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_started_year_is_refused_with_a_409(self):
        self.assertEqual(self.roll().status_code, 200)
        response = self.roll()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "TARGET_YEAR_NOT_EMPTY")


class SessionLensTests(_Base):
    """Every list answers about one year - the live one unless asked otherwise."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_this_year()
        # Next year has a level of its own that this year has never had.
        cls.jss9 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.next_year, program=cls.prog,
            name="JSS9", code="JSS9", order_index=9,
        )
        SchoolClass.all_objects.create(
            tenant=cls.tenant, session=cls.next_year, level=cls.jss9,
            branch=cls.lekki, name="JSS9 A", code="JSS9-A", arm="A",
        )
        Subject.all_objects.create(
            tenant=cls.tenant, session=cls.next_year, name="Astronomy",
            code="AST",
        )

    def names(self, view, params=None, key="name"):
        response = self.get(self.admin, view, params)
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        if view == "academics-program-list":
            rows = [lvl for row in rows for lvl in row["levels"]]
        return sorted(row[key] for row in rows)

    def test_the_live_year_is_what_a_screen_shows_by_default(self):
        self.assertEqual(self.names("academics-program-list"), ["JSS1", "JSS2"])
        self.assertEqual(self.names("academics-class-list"), ["JSS1 A"])
        self.assertEqual(self.names("academics-subject-list"), ["Mathematics"])

    def test_naming_a_year_moves_every_list_to_it(self):
        year = {"session": self.next_year.pk}
        self.assertEqual(self.names("academics-program-list", year), ["JSS9"])
        self.assertEqual(self.names("academics-class-list", year), ["JSS9 A"])
        self.assertEqual(self.names("academics-subject-list", year), ["Astronomy"])

    def test_another_schools_year_is_not_a_lens_this_school_can_use(self):
        response = self.get(
            self.admin, "academics-class-list", {"session": self.other_year.pk},
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_class_lands_in_its_LEVELS_year_not_the_lens(self):
        """A class cannot be in a different year from the level it sits in.

        So the level decides, and the lens does not get a vote: posting JSS9 B
        while looking at 2099/2100 still files it under 2100/2101, because that
        is where JSS9 is. The alternative is a class whose level belongs to
        another year, which nothing downstream could read sensibly.
        """
        response = self.post(
            self.admin, "academics-class-list",
            {"name": "JSS9 B", "code": "JSS9-B", "level": self.jss9.pk,
             "arm": "B", "branch": self.lekki.pk},
        )
        self.assertEqual(response.status_code, 201, response.data)
        made = SchoolClass.all_objects.get(name="JSS9 B")
        self.assertEqual(made.session_id, self.next_year.pk)

    def test_a_subject_lands_in_the_year_being_looked_at(self):
        """A subject hangs off no level, so the lens is the only answer.

        Otherwise a school drafting next year's curriculum would quietly add
        subjects to the year its pupils are sitting in right now.
        """
        response = self.post(
            self.admin, "academics-subject-list",
            {"name": "Further Maths", "code": "FMTH"},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Subject.all_objects.get(name="Further Maths").session_id,
            self.this_year.pk,
        )

        url = reverse("academics-subject-list")
        response = self.client_for(self.admin).post(
            f"{url}?tenant={self.tenant.slug}&session={self.next_year.pk}",
            {"name": "Geology", "code": "GEO"}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Subject.all_objects.get(name="Geology").session_id,
            self.next_year.pk,
        )
