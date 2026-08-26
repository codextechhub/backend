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
    Department,
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
            tenant=cls.tenant, name="Mathematics",
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

    def taught_in_next(self):
        """Subjects with an offering in the new year - subjects have no year."""
        return Subject.all_objects.filter(
            tenant=self.tenant, offerings__level__session=self.next_year,
        ).distinct()

    def test_the_structure_arrives_in_the_new_year(self):
        written = self.roll()
        self.assertEqual(written, {"levels": 2, "classes": 1, "subjects": 1})
        self.assertEqual(
            sorted(self.in_next(Level).values_list("name", flat=True)),
            ["JSS1", "JSS2"],
        )
        self.assertEqual(self.in_next(SchoolClass).count(), 1)
        self.assertEqual(self.taught_in_next().count(), 1)

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

    def test_an_offering_follows_the_SAME_subject_to_the_new_level(self):
        """The subject row is not copied - only where it is taught.

        Copying it would give Mathematics a second id, and the only thing
        tying the two together would be the name.
        """
        self.roll()
        offering = SubjectOffering.all_objects.get(
            level__session=self.next_year,
        )
        self.assertEqual(offering.subject_id, self.maths.pk)
        self.assertEqual(offering.level.session_id, self.next_year.pk)
        self.assertTrue(offering.is_core)
        self.assertEqual(Subject.all_objects.filter(name="Mathematics").count(), 1)

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
        # Catalogue, like every subject. What ties it to next year is the
        # offering, not the row.
        astronomy = Subject.all_objects.create(
            tenant=cls.tenant, name="Astronomy", code="AST",
        )
        SubjectOffering.all_objects.create(
            tenant=cls.tenant, subject=astronomy, level=cls.jss9,
        )

    def taught(self, view, params=None):
        """The subjects with somewhere to be taught in the year being read.

        A subject is catalogue and always listed; what answers to the year is
        its offerings, so that is what this reads.
        """
        response = self.get(self.admin, view, params)
        self.assertEqual(response.status_code, 200, response.data)
        return sorted(
            row["name"] for row in response.data["data"] if row["offerings"]
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
        # Both subjects are on file; only Mathematics is taught this year.
        self.assertEqual(
            self.taught("academics-subject-list"), ["Mathematics"],
        )

    def test_naming_a_year_moves_every_list_to_it(self):
        year = {"session": self.next_year.pk}
        self.assertEqual(self.names("academics-program-list", year), ["JSS9"])
        self.assertEqual(self.names("academics-class-list", year), ["JSS9 A"])
        self.assertEqual(
            self.taught("academics-subject-list", year), ["Astronomy"],
        )

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

    def test_a_new_subject_is_taught_in_the_year_being_looked_at(self):
        """The subject is catalogue; the OFFERING is what lands in a year.

        Otherwise a school drafting next year's curriculum would quietly add
        to the year its pupils are sitting in right now.
        """
        url = reverse("academics-subject-list")
        response = self.client_for(self.admin).post(
            f"{url}?tenant={self.tenant.slug}&session={self.next_year.pk}",
            {"name": "Geology", "code": "GEO", "level_ids": [self.jss9.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        geology = Subject.all_objects.get(name="Geology")
        self.assertEqual(
            list(
                SubjectOffering.all_objects.filter(subject=geology)
                .values_list("level__session__name", flat=True)
            ),
            ["2100/2101"],
        )


class ArchivedYearIsReadOnlyTests(_Base):
    """Last year's record stops being editable, which is why it is a record.

    Without this the year column buys nothing: a school could still open
    2025/2026 and add the class it forgot, and the register would say a class
    ran in a year it did not.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_this_year()
        cls.past = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2098/2099",
            start_date=dt.date(2098, 9, 1), end_date=dt.date(2099, 7, 31),
            status=SessionStatus.ARCHIVED,
        )
        cls.old_level = Level.all_objects.create(
            tenant=cls.tenant, session=cls.past, program=cls.prog,
            name="JSS1", code="JSS1", order_index=1,
        )

    def in_past(self, name, body, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(self.admin).post(
            f"{url}?tenant={self.tenant.slug}&session={self.past.pk}",
            body, format="json",
        )

    def test_a_subject_cannot_be_added_to_an_archived_year(self):
        response = self.in_past("academics-subject-list", {"name": "Latin"})
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "SESSION_ARCHIVED_READ_ONLY",
        )
        self.assertFalse(Subject.all_objects.filter(name="Latin").exists())

    def test_a_level_cannot_be_added_to_an_archived_year(self):
        response = self.in_past(
            "academics-level-list",
            {"name": "JSS4", "code": "JSS4", "order_index": 4},
            pk=self.prog.pk,
        )
        self.assertEqual(response.status_code, 409, response.data)

    def test_a_class_cannot_be_added_to_an_archived_years_level(self):
        """The lens says this year; the LEVEL says 2098/2099, and it wins.

        A class takes its year from its level, so the guard on the lens never
        sees this one - it is the path that would otherwise let an archived
        year be edited from a screen that looks perfectly current.
        """
        response = self.post(
            self.admin, "academics-class-list",
            {"name": "JSS1 Z", "code": "JSS1-Z", "level": self.old_level.pk,
             "arm": "Z"},
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(SchoolClass.all_objects.filter(name="JSS1 Z").exists())

    def test_generating_arms_into_an_archived_year_is_refused(self):
        url = reverse("academics-class-arms")
        response = self.client_for(self.admin).post(
            f"{url}?tenant={self.tenant.slug}",
            {"level": self.old_level.pk, "arms": ["A", "B"]}, format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            SchoolClass.all_objects.filter(session=self.past).count(), 0,
        )

    def test_reading_an_archived_year_still_works(self):
        """Read-only means read-only, not closed."""
        response = self.get(
            self.admin, "academics-program-list", {"session": self.past.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        levels = [lvl for row in response.data["data"] for lvl in row["levels"]]
        self.assertEqual([lvl["name"] for lvl in levels], ["JSS1"])


class OverviewNamesTheYearBeingReadTests(_Base):
    """The heading over a count has to be the year the count is about."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_this_year()

    def overview(self, params=None):
        return self.get(self.admin, "academics-overview", params).data["data"]

    def test_by_default_the_viewed_year_is_the_live_one(self):
        data = self.overview()
        self.assertEqual(data["viewed_session"]["id"], self.this_year.pk)
        self.assertEqual(data["active_session"]["id"], self.this_year.pk)

    def test_looking_back_names_the_year_looked_at_not_the_live_one(self):
        data = self.overview({"session": self.next_year.pk})
        self.assertEqual(data["viewed_session"]["name"], "2100/2101")
        self.assertEqual(data["viewed_session"]["status"], SessionStatus.DRAFT)
        # The live year is still reported, because the screen says which one
        # the school is actually running.
        self.assertEqual(data["active_session"]["name"], "2099/2100")

    def test_the_counts_follow_the_year_that_is_named(self):
        """Otherwise the numbers and the heading disagree, which is the bug."""
        live = self.overview()["counts"]
        drafted = self.overview({"session": self.next_year.pk})["counts"]
        self.assertEqual((live["levels"], live["classes"]), (2, 1))
        self.assertEqual((drafted["levels"], drafted["classes"]), (0, 0))


class ScrappingAProgrammeTests(_Base):
    """Dropping Commercial, without editing the year it ran in.

    A programme has no year of its own, so "we stopped running it" cannot be a
    delete and must not be an archive: both reach backwards. It is expressed by
    the programme having no levels in the new year, and the screens read that.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_this_year()
        cls.commerce_dept = Department.all_objects.create(
            tenant=cls.tenant, name="Commercial", code="COM",
        )
        cls.commercial = Program.all_objects.create(
            tenant=cls.tenant, name="Commercial", code="COMP",
            department=cls.commerce_dept,
        )
        Level.all_objects.create(
            tenant=cls.tenant, session=cls.this_year, program=cls.commercial,
            name="SSS1 Commercial", code="SSS1COM", order_index=1,
        )

    def programmes(self, session):
        response = self.get(
            self.admin, "academics-program-list", {"session": session.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        return {
            row["name"]: [lvl["name"] for lvl in row["levels"]]
            for row in response.data["data"]
        }

    def departments(self, session):
        """Each department's name against what it ran in `session`."""
        response = self.get(
            self.admin, "academics-department-list", {"session": session.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        return {
            row["name"]: row["program_count"] + row["subject_count"]
            for row in response.data["data"]
        }

    def test_a_programme_left_out_of_the_new_year_keeps_the_year_it_ran(self):
        """The whole point: 2100/2101 drops it, 2099/2100 still has it."""
        roll_forward(self.tenant, source=self.this_year, target=self.next_year)
        # The school then withdraws next year's Commercial levels.
        Level.all_objects.filter(
            session=self.next_year, program=self.commercial,
        ).delete()

        self.assertEqual(
            self.programmes(self.this_year)["Commercial"], ["SSS1 Commercial"],
        )
        self.assertEqual(self.programmes(self.next_year)["Commercial"], [])

    def test_a_programme_with_none_this_year_shows_none_not_every_year(self):
        """Narrowed to nothing is an answer, not a missing prefetch.

        The serializer fell back to every year's levels when the year's list
        came back empty, so a programme the school had stopped running showed
        last year's levels under this year's heading - the one case the
        narrowing exists to express was the one case that undid it. The same
        `or` sat in front of a subject's offerings.
        """
        # Both programmes are listed, both with nothing in them: the list is
        # the school's programmes, and the LEVELS are what belong to a year.
        self.assertEqual(
            self.programmes(self.next_year),
            {"Commercial": [], "Junior Secondary": []},
        )
        Level.all_objects.create(
            tenant=self.tenant, session=self.next_year, program=self.prog,
            name="JSS1", code="JSS1", order_index=1,
        )
        shown = self.programmes(self.next_year)
        self.assertEqual(shown["Junior Secondary"], ["JSS1"])
        # Commercial is listed with nothing in it, NOT with last year's level.
        self.assertEqual(shown["Commercial"], [])

    def test_a_departments_counts_say_it_ran_nothing_in_the_new_year(self):
        roll_forward(self.tenant, source=self.this_year, target=self.next_year)
        Level.all_objects.filter(
            session=self.next_year, program=self.commercial,
        ).delete()
        SubjectOffering.all_objects.filter(
            level__session=self.next_year, subject__department=self.commerce_dept,
        ).delete()

        self.assertTrue(self.departments(self.this_year)["Commercial"])
        self.assertEqual(self.departments(self.next_year)["Commercial"], 0)

    def test_a_departments_subject_count_is_one_years_not_every_years(self):
        """The count that read "Subjects 9" for three subjects.

        Subjects belong to a year, so counting them across all of them adds
        the same three subjects up once per year the school has run. Rolling
        forward, which duplicates them by design, made the number grow every
        September.
        """
        maths_dept = Department.all_objects.create(
            tenant=self.tenant, name="Sciences", code="SCI",
        )
        Subject.all_objects.filter(pk=self.maths.pk).update(department=maths_dept)
        roll_forward(self.tenant, source=self.this_year, target=self.next_year)

        for year in (self.this_year, self.next_year):
            counts = {
                row["name"]: row["subject_count"]
                for row in self.get(
                    self.admin, "academics-department-list",
                    {"session": year.pk},
                ).data["data"]
            }
            self.assertEqual(counts["Sciences"], 1, f"wrong in {year.name}")

    def test_it_is_never_deleted_only_left_out_of_the_new_year(self):
        """There is no delete to refuse any more, which is the stronger answer.

        The refusal used to have to explain that last year still held levels.
        Now the route is gone: a programme is part of what a school ran, and
        the way to stop running it is to leave it out of the new year.
        """
        url = reverse(
            "academics-program-detail", kwargs={"pk": self.commercial.pk},
        )
        response = self.client_for(self.admin).delete(
            f"{url}?tenant={self.tenant.slug}&session={self.next_year.pk}",
        )
        self.assertEqual(response.status_code, 405, response.data)
        self.assertTrue(
            Program.all_objects.filter(pk=self.commercial.pk).exists(),
        )
