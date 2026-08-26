"""The edges of the year lens: across it, into a closed one, and before any.

Written after the lens shipped, from the question "what can still reach a year
it does not belong to?" - which had six answers.
"""
from __future__ import annotations

import datetime as dt

from django.urls import reverse

from schools.vs_academics.models import (
    AcademicSession, Level, SessionStatus, Subject, SubjectOffering,
)
from schools.vs_academics.tests.test_rollover import _Base


class YearBoundaryTests(_Base):
    """Writes that would reach across a year boundary, or into a closed one.

    Each of these was possible. A year is only an honest record if nothing
    outside it can point into it, and the FK columns alone cannot say that -
    a level's `next_level` and an offering's `level` are both plain
    self/foreign keys with no year in the constraint.
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

    # 1 ─────────────────────────────────────────────────────────────────────
    def test_a_subject_cannot_be_offered_at_another_years_level(self):
        response = self.post(
            self.admin, "academics-subject-list",
            {"name": "Latin", "code": "LAT", "level_ids": [self.old_level.pk]},
        )
        self.assertIn(response.status_code, (400, 404, 409, 422), response.data)
        self.assertFalse(
            SubjectOffering.all_objects.filter(level=self.old_level).exists(),
        )

    # 2 ─────────────────────────────────────────────────────────────────────
    def test_a_level_cannot_promote_into_another_years_level(self):
        url = reverse("academics-level-detail", kwargs={"pk": self.jss1.pk})
        response = self.client_for(self.admin).patch(
            f"{url}?tenant={self.tenant.slug}",
            {"next_level": self.old_level.pk}, format="json",
        )
        self.assertIn(response.status_code, (400, 404, 409, 422), response.data)
        self.jss1.refresh_from_db()
        self.assertNotEqual(self.jss1.next_level_id, self.old_level.pk)

    # 3 ─────────────────────────────────────────────────────────────────────
    def test_rolling_forward_into_an_archived_year_is_refused(self):
        # EMPTY archived year, so the only thing that could refuse it is the
        # archived rule itself.
        empty_past = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2097/2098",
            start_date=dt.date(2097, 9, 1), end_date=dt.date(2098, 7, 31),
            status=SessionStatus.ARCHIVED,
        )
        url = reverse(
            "academics-session-roll-forward", kwargs={"pk": empty_past.pk},
        )
        response = self.client_for(self.admin).post(
            f"{url}?tenant={self.tenant.slug}",
            {"from": self.this_year.pk}, format="json",
        )
        self.assertIn(response.status_code, (409, 422), response.data)

    # 4 ─────────────────────────────────────────────────────────────────────
    def test_rolling_into_a_year_that_has_subjects_but_no_levels_is_refused(self):
        Subject.all_objects.create(
            tenant=self.tenant, session=self.next_year, name="Latin", code="LAT",
        )
        url = reverse(
            "academics-session-roll-forward", kwargs={"pk": self.next_year.pk},
        )
        response = self.client_for(self.admin).post(
            f"{url}?tenant={self.tenant.slug}",
            {"from": self.this_year.pk}, format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            Subject.all_objects.filter(
                session=self.next_year, name="Latin",
            ).count(), 1,
        )


class NoYearYetTests(_Base):
    """A school that has not created its first academic year.

    Every onboarding school is in this state, and the module is meant to be
    open to them - it is where they build the structure in the first place.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # The fixture builds three years; this school has none.
        AcademicSession.all_objects.filter(tenant=cls.tenant).delete()

    def test_the_departments_screen_still_opens(self):
        response = self.get(self.admin, "academics-department-list")
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_overview_still_opens(self):
        response = self.get(self.admin, "academics-overview")
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_programmes_screen_still_opens(self):
        response = self.get(self.admin, "academics-program-list")
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_department_can_still_be_created(self):
        response = self.post(
            self.admin, "academics-department-list", {"name": "Sciences"},
        )
        self.assertEqual(response.status_code, 201, response.data)


class ClosedYearRowsTests(_Base):
    """A row in a closed year cannot be edited, deleted, or re-offered.

    The archived-year guard reads the LENS, and a detail view does not use the
    lens - it resolves a row by primary key. So creating into a closed year was
    refused while renaming and deleting inside one went straight through, and
    the rule only looked enforced. A row carries its own year.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.past = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2098/2099",
            start_date=dt.date(2098, 9, 1), end_date=dt.date(2099, 7, 31),
            status=SessionStatus.ARCHIVED,
        )
        cls.old_level = Level.all_objects.create(
            tenant=cls.tenant, session=cls.past, program=cls.prog,
            name="JSS9", code="JSS9", order_index=9,
        )
        cls.old_subject = Subject.all_objects.create(
            tenant=cls.tenant, session=cls.past, name="Latin", code="LAT",
        )

    def test_a_level_in_an_archived_year_cannot_be_deleted(self):
        url = reverse("academics-level-detail", kwargs={"pk": self.old_level.pk})
        r = self.client_for(self.admin).delete(f"{url}?tenant={self.tenant.slug}")
        self.assertIn(r.status_code, (403, 409), r.data)
        self.assertTrue(Level.all_objects.filter(pk=self.old_level.pk).exists())

    def test_a_subject_in_an_archived_year_cannot_be_deleted(self):
        url = reverse("academics-subject-detail", kwargs={"pk": self.old_subject.pk})
        r = self.client_for(self.admin).delete(f"{url}?tenant={self.tenant.slug}")
        self.assertIn(r.status_code, (403, 409), r.data)
        self.assertTrue(Subject.all_objects.filter(pk=self.old_subject.pk).exists())

    def test_a_level_in_an_archived_year_cannot_be_edited(self):
        url = reverse("academics-level-detail", kwargs={"pk": self.old_level.pk})
        r = self.client_for(self.admin).patch(
            f"{url}?tenant={self.tenant.slug}", {"name": "Rewritten"}, format="json",
        )
        self.assertIn(r.status_code, (403, 409), r.data)
        self.old_level.refresh_from_db()
        self.assertEqual(self.old_level.name, "JSS9")

    def test_a_class_in_an_archived_year_cannot_be_archived_again(self):
        """The lifecycle routes resolve by pk too, so they need the same guard."""
        from schools.vs_academics.models import SchoolClass

        old_class = SchoolClass.all_objects.create(
            tenant=self.tenant, session=self.past, level=self.old_level,
            name="JSS9 A", code="JSS9-A", arm="A",
        )
        url = reverse("academics-class-archive", kwargs={"pk": old_class.pk})
        r = self.client_for(self.admin).post(f"{url}?tenant={self.tenant.slug}")
        self.assertIn(r.status_code, (403, 409), r.data)
        old_class.refresh_from_db()
        self.assertTrue(old_class.is_active)

    def test_offerings_cannot_be_rewritten_in_an_archived_year(self):
        url = reverse(
            "academics-subject-offerings", kwargs={"pk": self.old_subject.pk},
        )
        r = self.client_for(self.admin).put(
            f"{url}?tenant={self.tenant.slug}",
            {"level_ids": [self.old_level.pk]}, format="json",
        )
        self.assertIn(r.status_code, (403, 409), r.data)
