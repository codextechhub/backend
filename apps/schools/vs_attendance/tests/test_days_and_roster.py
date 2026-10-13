"""When a register is due, and who is on it.

Both are read as at the register's date, never as the school stands today.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_calendar.models import CalendarEvent, CalendarEventAudience
from schools.vs_students.constants import StudentStatus
from schools.vs_students.models import ClassEnrolment, StudentStatusLog

from django.db import connection
from django.test.utils import CaptureQueriesContext

from ..services.days import (
    CODE_CLOSED_BY_EVENT,
    CODE_NOT_TEACHING_DAY,
    CODE_OUTSIDE_TERM,
    DayCalendar,
    school_day,
)
from schools.vs_students.services.roster import roster_as_at, rosters_as_at, status_on, suspended_student_ids
from .base import (
    FOUNDERS_DAY,
    LAST_FRIDAY,
    LEKKI_OPEN_DAY,
    TODAY,
    AttendanceFixture,
)


class SchoolDayTests(AttendanceFixture):
    def day(self, school_class, branch, on_date):
        return school_day(self.tenant, school_class, branch, on_date)

    def test_an_ordinary_teaching_day_is_open_and_names_its_term(self):
        day = self.day(self.jss1a, self.lekki, LAST_FRIDAY)
        self.assertTrue(day.is_open)
        self.assertEqual(day.term, self.first_term)
        self.assertIsNone(day.payload())

    def test_a_weekend_is_not_a_teaching_day(self):
        day = self.day(self.jss1a, self.lekki, dt.date(2025, 10, 11))
        self.assertFalse(day.is_open)
        self.assertEqual(day.code, CODE_NOT_TEACHING_DAY)
        self.assertEqual(day.reason, "Saturday is not a teaching day.")

    def test_a_date_outside_every_term_is_closed(self):
        day = self.day(self.jss1a, self.lekki, dt.date(2025, 12, 15))
        self.assertFalse(day.is_open)
        self.assertEqual(day.code, CODE_OUTSIDE_TERM)

    def test_a_school_wide_closure_closes_every_branch_and_names_itself(self):
        for school_class, branch in ((self.jss1a, self.lekki), (self.sss2, self.ikeja)):
            day = self.day(school_class, branch, FOUNDERS_DAY)
            self.assertFalse(day.is_open)
            self.assertEqual(day.code, CODE_CLOSED_BY_EVENT)
            self.assertEqual(day.reason, "Founders' Day")

    def test_a_branch_closure_closes_only_that_branch(self):
        self.assertFalse(self.day(self.jss1a, self.lekki, LEKKI_OPEN_DAY).is_open)
        self.assertTrue(self.day(self.sss2, self.ikeja, LEKKI_OPEN_DAY).is_open)

    def test_a_closure_narrowed_to_a_level_closes_only_that_level(self):
        """Lekki's primary Speech Day closes Primary 4 A and not JSS1 A."""
        speech_day = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=self.lekki,
            name="Speech Day", event_type="SCHOOL_EVENT",
            start_date=LAST_FRIDAY, end_date=LAST_FRIDAY, closes_school=True,
        )
        CalendarEventAudience.all_objects.create(
            tenant=self.tenant, event=speech_day, level=self.pry4,
        )
        self.assertFalse(self.day(self.pry4a, self.lekki, LAST_FRIDAY).is_open)
        self.assertTrue(self.day(self.jss1a, self.lekki, LAST_FRIDAY).is_open)

    def test_an_event_that_does_not_close_the_school_closes_nothing(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=None, name="PTA evening",
            event_type="PTA", start_date=LAST_FRIDAY, end_date=LAST_FRIDAY,
            closes_school=False,
        )
        self.assertTrue(self.day(self.jss1a, self.lekki, LAST_FRIDAY).is_open)


class RosterTests(AttendanceFixture):
    def names(self, on_date, school_class=None):
        entries = roster_as_at(self.tenant, school_class or self.jss1a, on_date)
        return [(e.student.first_name, e.suspended) for e in entries]

    def test_the_roster_is_in_name_order_with_the_suspended_child_locked(self):
        # Bello, Eze, Nwosu, Yusuf.
        self.assertEqual(self.names(TODAY), [
            ("Tunde", False), ("Kelechi", False), ("Chiamaka", False), ("Amina", True),
        ])

    def test_a_child_placed_after_the_date_is_not_on_it_yet(self):
        self.assertNotIn("Obinna", [n for n, _ in self.names(TODAY)])
        self.assertIn("Obinna", [n for n, _ in self.names(dt.date(2025, 10, 20))])

    def test_a_child_is_not_on_the_register_before_their_placement_took_effect(self):
        self.assertEqual(self.names(dt.date(2025, 9, 1)), [])

    def test_a_class_with_nobody_placed_has_an_empty_roster(self):
        self.assertEqual(roster_as_at(self.tenant, self.pry4a, TODAY), [])

    def test_a_move_agreed_now_but_effective_later_leaves_the_child_in_the_old_class(self):
        """Tunde moves to JSS1 B from the 20th; until then he is in JSS1 A."""
        ClassEnrolment.all_objects.filter(student=self.tunde).update(is_active=False)
        ClassEnrolment.all_objects.create(
            tenant=self.tenant, student=self.tunde, school_class=self.jss1b,
            session=self.year, is_active=True, effective_date=dt.date(2025, 10, 20),
        )
        self.assertIn("Tunde", [n for n, _ in self.names(TODAY)])
        self.assertNotIn("Tunde", [n for n, _ in self.names(TODAY, self.jss1b)])
        self.assertNotIn("Tunde", [n for n, _ in self.names(dt.date(2025, 10, 20))])
        self.assertIn("Tunde", [n for n, _ in self.names(dt.date(2025, 10, 20), self.jss1b)])

    def test_a_child_who_left_the_roll_is_on_earlier_registers_and_not_later_ones(self):
        StudentStatusLog.all_objects.create(
            tenant=self.tenant, student=self.chiamaka,
            from_status=StudentStatus.ACTIVE, to_status=StudentStatus.WITHDRAWN,
            reason="Family relocated.", effective_date=dt.date(2025, 10, 10),
        )
        self.assertIn("Chiamaka", [n for n, _ in self.names(dt.date(2025, 10, 9))])
        self.assertNotIn("Chiamaka", [n for n, _ in self.names(TODAY)])

    def test_a_suspension_with_a_return_date_ends_on_that_day_without_the_sweep(self):
        StudentStatusLog.all_objects.filter(student=self.amina).delete()
        self.make_suspension(self.amina, dt.date(2025, 10, 6), dt.date(2025, 10, 9))
        ids = [self.amina.pk]
        self.assertEqual(suspended_student_ids(self.tenant, ids, dt.date(2025, 10, 8)), {self.amina.pk})
        self.assertEqual(suspended_student_ids(self.tenant, ids, dt.date(2025, 10, 9)), set())

    def test_a_suspension_is_read_as_at_the_date_not_as_today(self):
        self.assertEqual(
            suspended_student_ids(self.tenant, [self.amina.pk], dt.date(2025, 10, 3)), set(),
        )
        self.assertEqual(
            suspended_student_ids(self.tenant, [self.amina.pk], TODAY), {self.amina.pk},
        )

    def test_another_schools_children_are_never_on_the_roster(self):
        self.assertEqual(roster_as_at(self.solo, self.jss1a, TODAY), [])


class DayCalendarTests(AttendanceFixture):
    """One calendar loaded for a range answers exactly as asking a day at a time."""

    def test_it_agrees_with_school_day_for_every_class_and_date(self):
        classes = [
            (self.jss1a, self.lekki), (self.pry4a, self.lekki), (self.sss2, self.ikeja),
        ]
        speech_day = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=self.lekki, name="Speech Day",
            event_type="SCHOOL_EVENT", start_date=LAST_FRIDAY, end_date=LAST_FRIDAY,
            closes_school=True,
        )
        CalendarEventAudience.all_objects.create(
            tenant=self.tenant, event=speech_day, level=self.pry4,
        )
        start, end = dt.date(2025, 9, 1), dt.date(2025, 12, 31)
        calendar = DayCalendar(self.tenant, [self.year], start, end)
        day = start
        while day <= end:
            for school_class, branch in classes:
                with self.subTest(day=day, school_class=school_class.name):
                    mine = calendar.status(school_class, branch, day)
                    theirs = school_day(self.tenant, school_class, branch, day)
                    self.assertEqual((mine.is_open, mine.code, mine.reason), (theirs.is_open, theirs.code, theirs.reason))
            day += dt.timedelta(days=1)

    def test_it_asks_the_database_only_when_it_is_loaded(self):
        calendar = DayCalendar(self.tenant, [self.year], dt.date(2025, 9, 8), dt.date(2025, 12, 12))
        with CaptureQueriesContext(connection) as queries:
            for offset in range(60):
                calendar.status(
                    self.jss1a, self.lekki, dt.date(2025, 9, 8) + dt.timedelta(days=offset),
                )
        self.assertEqual(len(queries), 0)


class BulkRosterTests(AttendanceFixture):
    def test_it_reads_every_class_as_roster_as_at_reads_it_alone(self):
        ClassEnrolment.all_objects.filter(student=self.tunde).update(is_active=False)
        ClassEnrolment.all_objects.create(
            tenant=self.tenant, student=self.tunde, school_class=self.jss1b,
            session=self.year, is_active=True, effective_date=dt.date(2025, 10, 10),
        )
        StudentStatusLog.all_objects.create(
            tenant=self.tenant, student=self.chiamaka,
            from_status=StudentStatus.ACTIVE, to_status=StudentStatus.WITHDRAWN,
            reason="Family relocated.", effective_date=dt.date(2025, 10, 12),
        )
        classes = [self.jss1a, self.jss1b, self.pry4a, self.sss2, self.shared]
        for on_date in (dt.date(2025, 9, 1), dt.date(2025, 10, 9), TODAY, dt.date(2025, 10, 20)):
            bulk = rosters_as_at(self.tenant, classes, on_date)
            for school_class in classes:
                with self.subTest(school_class=school_class.name, on_date=on_date):
                    expected = [
                        (e.student.pk, e.suspended)
                        for e in roster_as_at(self.tenant, school_class, on_date)
                    ]
                    self.assertEqual(
                        [(e.student.pk, e.suspended) for e in bulk[school_class.pk]], expected,
                    )

    def test_the_queries_do_not_grow_with_the_classes(self):
        with CaptureQueriesContext(connection) as one:
            rosters_as_at(self.tenant, [self.jss1a], TODAY)
        with CaptureQueriesContext(connection) as five:
            rosters_as_at(
                self.tenant, [self.jss1a, self.jss1b, self.pry4a, self.sss2, self.shared], TODAY,
            )
        self.assertEqual(len(five), len(one))

    def test_no_classes_is_an_empty_answer_and_another_schools_children_are_never_in_it(self):
        self.assertEqual(rosters_as_at(self.tenant, [], TODAY), {})
        self.assertEqual(rosters_as_at(self.solo, [self.jss1a], TODAY), {self.jss1a.pk: []})


class StatusOnTests(AttendanceFixture):
    def test_one_childs_status_is_read_from_their_own_log_for_any_day(self):
        logs = list(StudentStatusLog.objects.filter(student=self.amina))
        self.assertIsNone(status_on(logs, dt.date(2025, 10, 3)))
        self.assertEqual(status_on(logs, dt.date(2025, 10, 6)), StudentStatus.SUSPENDED)
        StudentStatusLog.all_objects.filter(student=self.amina).delete()
        self.make_suspension(self.amina, dt.date(2025, 10, 6), dt.date(2025, 10, 9))
        logs = list(StudentStatusLog.objects.filter(student=self.amina))
        self.assertEqual(status_on(logs, dt.date(2025, 10, 8)), StudentStatus.SUSPENDED)
        self.assertEqual(status_on(logs, dt.date(2025, 10, 9)), StudentStatus.ACTIVE)
