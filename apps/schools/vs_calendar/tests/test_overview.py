"""The hub, and the two figures FRD v3.0.1 forbids and the design shows."""
from __future__ import annotations

import datetime as dt

from ..models import CalendarEvent, EventType, TimetableSlot
from .base import _Base, _SingleBranchBase


class OverviewCountTests(_Base):
    def setUp(self):
        self.eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")

    def test_the_counts_include_the_timetable_figures_the_hub_shows(self):
        """FR-007 forbids these; the text predates the timetable half existing."""
        TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.jss1a,
            day_of_week=1, period=self.p1, subject=self.maths, teacher=self.eze,
            room=self.room_a1,
        )
        response = self.get(
            self.admin, "calendar-overview", {"on": "2025-10-15"},
        )
        counts = response.data["data"]["counts"]
        self.assertEqual(counts["classes_timetabled"], 1)
        self.assertEqual(counts["rooms"], 3)
        self.assertEqual(counts["terms"], 2)

    def test_there_is_no_complete_timetable_count_and_no_exam_count(self):
        """The two prohibitions of FR-007 that survive, and why."""
        response = self.get(self.admin, "calendar-overview")
        counts = response.data["data"]["counts"]
        for key in ("classes_complete", "complete", "exams", "scheduled_exams"):
            self.assertNotIn(key, counts)

    def test_the_counts_follow_the_branch_lens(self):
        response = self.get(self.ikeja_admin, "calendar-overview")
        # Only Ikeja's one room, not Lekki's two.
        self.assertEqual(response.data["data"]["counts"]["rooms"], 1)

    def test_next_up_is_empty_rather_than_absent(self):
        response = self.get(
            self.admin, "calendar-overview", {"on": "2026-07-16"},
        )
        self.assertEqual(response.data["data"]["next_up"], [])

    def test_next_up_carries_the_id_so_a_client_can_link_to_it(self):
        event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Independence Day",
            event_type=EventType.HOLIDAY,
            start_date=dt.date(2025, 10, 1), end_date=dt.date(2025, 10, 1),
        )
        response = self.get(
            self.admin, "calendar-overview", {"on": "2025-09-20"},
        )
        first = response.data["data"]["next_up"][0]
        self.assertEqual(first["id"], event.pk)
        self.assertEqual(first["days_away"], 11)


class OverviewAlertTests(_Base):
    def setUp(self):
        self.eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")

    def _codes(self, user=None, params=None):
        response = self.get(
            user or self.admin, "calendar-overview",
            {"on": "2025-10-15", **(params or {})},
        )
        return {a["code"] for a in response.data["data"]["alerts"]}

    def test_a_well_formed_year_raises_nothing_about_the_calendar(self):
        # A different day each, so the fixture itself does not create the
        # clash the assertion is checking for the absence of.
        lekki_rooms = (self.room_a1, self.room_a2)
        for index, cls in enumerate(
            (self.jss1a, self.jss1b, self.sss2, self.pry4a),
        ):
            TimetableSlot.all_objects.create(
                tenant=self.tenant, session=self.year, school_class=cls,
                day_of_week=index + 1, period=self.p1, subject=self.maths,
                room=(
                    self.room_c1 if cls.branch_id == self.ikeja.pk
                    else lekki_rooms[index % 2]
                ),
            )
        self.assertEqual(self._codes(), set())

    def test_a_year_with_no_terms_says_so(self):
        from schools.vs_academics.models import AcademicTerm

        AcademicTerm.all_objects.filter(session=self.year).delete()
        self.assertIn("SESSION_HAS_NO_TERMS", self._codes())

    def test_an_event_in_the_gap_between_terms_is_flagged(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Christmas break",
            event_type=EventType.HOLIDAY,
            start_date=dt.date(2025, 12, 19), end_date=dt.date(2026, 1, 2),
        )
        self.assertIn("EVENT_OUTSIDE_ANY_TERM", self._codes())

    def test_a_malformed_term_written_behind_the_api_is_still_reported(self):
        """M13 refuses this at write time; rows arrive by import too."""
        from schools.vs_academics.models import AcademicTerm

        AcademicTerm.all_objects.filter(pk=self.second_term.pk).update(
            end_date=dt.date(2026, 9, 30),
        )
        self.assertIn("TERM_OUTSIDE_SESSION", self._codes())

    def test_overlapping_terms_written_behind_the_api_are_reported(self):
        from schools.vs_academics.models import AcademicTerm

        AcademicTerm.all_objects.filter(pk=self.second_term.pk).update(
            start_date=dt.date(2025, 12, 1),
        )
        self.assertIn("TERM_DATES_OVERLAP", self._codes())

    def test_an_unresolved_clash_is_surfaced_on_the_hub(self):
        for cls in (self.jss1a, self.jss1b):
            TimetableSlot.all_objects.create(
                tenant=self.tenant, session=self.year, school_class=cls,
                day_of_week=1, period=self.p1, subject=self.maths,
                teacher=self.eze, room=self.room_a1,
            )
        codes = self._codes()
        self.assertIn("TIMETABLE_HAS_CLASHES", codes)

    def test_a_class_with_no_timetable_is_surfaced(self):
        self.assertIn("CLASS_HAS_NO_TIMETABLE", self._codes())

    def test_an_event_whose_term_was_archived_raises_no_alert(self):
        """An archived term is still a term, and its events fall inside it."""
        from schools.vs_academics.models import AcademicTerm
        from django.utils import timezone

        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="PTA Meeting",
            event_type=EventType.PTA,
            start_date=dt.date(2025, 11, 8), end_date=dt.date(2025, 11, 8),
        )
        AcademicTerm.all_objects.filter(pk=self.first_term.pk).update(
            archived_at=timezone.now(),
        )
        self.assertNotIn("EVENT_OUTSIDE_ANY_TERM", self._codes())


class TeachingDayTests(_Base):
    def test_the_term_carries_both_a_day_count_and_a_teaching_day_count(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Independence Day",
            event_type=EventType.HOLIDAY,
            start_date=dt.date(2025, 10, 1), end_date=dt.date(2025, 10, 1),
            closes_school=True,
        )
        response = self.get(
            self.admin, "calendar-overview", {"on": "2025-10-15"},
        )
        term = response.data["data"]["term"]
        self.assertEqual(term["name"], "First Term")
        # Teaching days exclude weekends and closures; plain days do not.
        self.assertLess(term["teaching_days_total"], term["days_total"])


class YearViewTests(_Base):
    def test_the_terms_carry_their_state(self):
        response = self.get(self.admin, "calendar-year", {"on": "2025-10-15"})
        states = {t["name"]: t["state"] for t in response.data["data"]["terms"]}
        self.assertEqual(states["First Term"], "ongoing")
        self.assertEqual(states["Second Term"], "pending")

    def test_an_archived_year_says_it_is_read_only(self):
        self.year.status = "ARCHIVED"
        self.year.save(update_fields=["status"])
        response = self.get(self.admin, "calendar-year")
        self.assertTrue(response.data["data"]["session"]["read_only"])


class CurrentViewTests(_Base):
    def test_it_answers_which_term_it_is(self):
        response = self.get(self.admin, "calendar-current", {"on": "2025-10-15"})
        self.assertEqual(response.data["data"]["term"]["name"], "First Term")

    def test_a_day_between_terms_has_no_term(self):
        response = self.get(self.admin, "calendar-current", {"on": "2025-12-25"})
        self.assertIsNone(response.data["data"]["term"])


class ArchivedYearTests(_Base):
    def test_an_archived_year_refuses_a_write(self):
        self.year.status = "ARCHIVED"
        self.year.save(update_fields=["status"])
        response = self.post(self.admin, "calendar-event-list", {
            "name": "Too late", "event_type": "HOLIDAY",
            "start_date": "2025-10-01", "end_date": "2025-10-01",
        })
        self.assertIn(response.status_code, (409, 422), response.data)

    def test_an_archived_years_calendar_is_still_readable_in_full(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Independence Day",
            event_type=EventType.HOLIDAY,
            start_date=dt.date(2025, 10, 1), end_date=dt.date(2025, 10, 1),
        )
        self.year.status = "ARCHIVED"
        self.year.save(update_fields=["status"])
        response = self.get(self.admin, "calendar-event-list")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["data"]), 1)


class OverviewSecurityTests(_Base):
    def test_the_overview_needs_the_calendar_view_key(self):
        from vs_rbac.tests.helpers import make_school_admin

        stranger = make_school_admin(
            None, email="nobody@brightfield.test", tenant=self.tenant,
        )
        response = self.get(stranger, "calendar-overview")
        self.assertEqual(response.status_code, 403, response.data)


class SingleBranchOverviewTests(_SingleBranchBase):
    def test_it_works_with_one_branch_and_nothing_else(self):
        response = self.get(self.admin, "calendar-overview")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["counts"]["rooms"], 0)

    def test_a_school_with_no_year_answers_200_with_nothing(self):
        """Not 404: a school that has not started its year is not broken."""
        from schools.vs_academics.models import AcademicSession, AcademicTerm

        AcademicTerm.all_objects.filter(session=self.year).delete()
        AcademicSession.all_objects.filter(tenant=self.tenant).delete()
        response = self.get(self.admin, "calendar-overview")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], {})
