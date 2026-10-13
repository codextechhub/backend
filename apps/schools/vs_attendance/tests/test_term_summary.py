"""The system-side term summary: one class's counts, the same as the tab and the report.

The first term is shortened to begin on Monday 6 October and today is Monday 13
October, so a class has six weekdays to account for: the 6th, the 7th (Lekki
Open Day, Lekki only), the 8th, the 9th (Founders' Day, everyone), the 10th and
today, the 13th. Registers are written straight to the tables.

JSS1 A (Lekki):

* the 6th: Chiamaka late, Tunde present, Kelechi present;
* the 8th: Chiamaka present, Tunde absent, Kelechi excused;
* the 9th: a register is on file for the closed day, Tunde absent, and counts
  for nothing;
* the 10th: no register;
* the 13th: Chiamaka and Tunde present, Kelechi has no mark;
* Amina is suspended from the 6th; Zara is placed on the 8th; Obinna is placed
  on the 20th, after today.

Wale leaves JSS1 A for JSS1 B on the 10th, so each class counts only his own
stretch in it.
"""
from __future__ import annotations

import datetime as dt

from django.db import connection
from django.test.utils import CaptureQueriesContext

from schools.vs_academics.models import AcademicTerm
from schools.vs_calendar.models import CalendarEvent
from schools.vs_students.models import ClassEnrolment

from ..exceptions import ClassHasNoBranch
from ..models import ClassRegister, StudentMark
from ..services.report import build_report
from ..services.student_tab import student_tab
from ..services.term_summary import (
    ClassTermSummary,
    StudentTermCounts,
    class_term_summary,
)
from .base import AttendanceFixture

D = dt.date
TAB_KEYS = ("opened", "present", "absent", "late", "excused", "not_taken")


def at(day, hour=9):
    return dt.datetime(2025, 10, day, hour, 0, tzinfo=dt.timezone.utc)


class _Summary(AttendanceFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        AcademicTerm.all_objects.filter(pk=cls.first_term.pk).update(start_date=D(2025, 10, 6))
        cls.first_term.refresh_from_db()
        cls.zara = cls.make_student(
            "Zara", "Okon", cls.jss1a, "BFS/301", effective=D(2025, 10, 8),
        )
        cls.wale = cls.make_student("Wale", "Ojo", cls.jss1a, "BFS/302")
        ClassEnrolment.all_objects.filter(student=cls.wale).update(is_active=False)
        ClassEnrolment.all_objects.create(
            tenant=cls.tenant, student=cls.wale, school_class=cls.jss1b,
            session=cls.year, is_active=True, effective_date=D(2025, 10, 10),
        )

        def register(school_class, day):
            return ClassRegister.objects.create(
                tenant=cls.tenant, school_class=school_class, date=D(2025, 10, day),
                branch=cls.lekki, session=cls.year, term=cls.first_term,
                submitted_at=at(day), submitted_by=cls.eze,
            )

        def mark(reg, student, kind, **extra):
            return StudentMark.objects.create(
                tenant=cls.tenant, register=reg, student=student, mark=kind,
                marked_by=cls.eze, **extra,
            )

        monday, wednesday = register(cls.jss1a, 6), register(cls.jss1a, 8)
        thursday, today = register(cls.jss1a, 9), register(cls.jss1a, 13)
        mark(monday, cls.chiamaka, "L", arrived_at=dt.time(8, 40))
        mark(monday, cls.tunde, "P")
        mark(monday, cls.kelechi, "P")
        mark(monday, cls.wale, "A")
        mark(wednesday, cls.chiamaka, "P")
        mark(wednesday, cls.tunde, "A")
        mark(wednesday, cls.kelechi, "E")
        mark(wednesday, cls.zara, "P")
        mark(thursday, cls.tunde, "A")
        mark(today, cls.chiamaka, "P")
        mark(today, cls.tunde, "P")
        mark(today, cls.zara, "P")
        friday_b = register(cls.jss1b, 10)
        mark(friday_b, cls.wale, "P")

    def summary(self, school_class=None, **extra):
        return class_term_summary(
            self.tenant, school_class or self.jss1a, self.first_term, **extra,
        )

    def tab_counts(self, student):
        counts = student_tab(self.tenant, self.admin, student.pk, self.first_term.pk)["counts"]
        return {key: counts[key] for key in TAB_KEYS}

    @staticmethod
    def counts_dict(counts):
        return {key: getattr(counts, key) for key in TAB_KEYS}


class MatchesTheTabTests(_Summary):
    def test_each_pupil_who_stayed_has_the_tabs_counts(self):
        summary = self.summary()

        for student in (self.chiamaka, self.tunde, self.kelechi, self.amina):
            with self.subTest(student=student.first_name):
                self.assertEqual(
                    self.counts_dict(summary.students[student.pk]),
                    self.tab_counts(student),
                )

    def test_the_counts_have_the_tabs_meanings(self):
        summary = self.summary()

        self.assertEqual(summary.students[self.chiamaka.pk], StudentTermCounts(
            opened=4, present=3, absent=0, late=1, excused=0, not_taken=1,
        ))
        self.assertEqual(summary.students[self.tunde.pk], StudentTermCounts(
            opened=4, present=2, absent=1, late=0, excused=0, not_taken=1,
        ))
        self.assertEqual(summary.students[self.kelechi.pk], StudentTermCounts(
            opened=4, present=1, absent=0, late=0, excused=1, not_taken=2,
        ))

    def test_a_suspended_pupil_is_counted_nowhere(self):
        self.assertEqual(
            self.summary().students[self.amina.pk],
            StudentTermCounts(opened=0, present=0, absent=0, late=0, excused=0, not_taken=0),
        )

    def test_the_result_is_a_frozen_summary(self):
        summary = self.summary()

        self.assertIsInstance(summary, ClassTermSummary)
        with self.assertRaises(AttributeError):
            summary.days_not_taken = 0


class ClosedDaysTests(_Summary):
    def test_days_closed_by_a_calendar_entry_are_excluded(self):
        counts = self.summary().students[self.tunde.pk]

        # The 7th (Lekki Open Day) and the 9th (Founders' Day, with a register
        # and an Absent mark on file) are open to nobody in JSS1 A.
        self.assertEqual((counts.opened, counts.absent), (4, 1))

    def test_a_new_closure_takes_its_day_out_of_every_count(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=None, name="Staff training",
            event_type="SCHOOL_EVENT", start_date=D(2025, 10, 10),
            end_date=D(2025, 10, 10), closes_school=True,
        )

        summary = self.summary()

        self.assertEqual(summary.days_not_taken, 0)
        self.assertEqual(summary.students[self.chiamaka.pk].not_taken, 0)
        self.assertEqual(summary.students[self.chiamaka.pk].opened, 3)
        self.assertEqual(
            self.counts_dict(summary.students[self.chiamaka.pk]),
            self.tab_counts(self.chiamaka),
        )

    def test_a_closure_for_another_branch_changes_nothing(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=self.ikeja, name="Ikeja trip",
            event_type="SCHOOL_EVENT", start_date=D(2025, 10, 10),
            end_date=D(2025, 10, 10), closes_school=True,
        )

        self.assertEqual(self.summary().days_not_taken, 1)


class PlacementTests(_Summary):
    def test_a_pupil_placed_mid_term_counts_only_from_their_placement(self):
        counts = self.summary().students[self.zara.pk]

        # The 8th, the 10th (no register) and the 13th; never the 6th.
        self.assertEqual(
            counts,
            StudentTermCounts(opened=3, present=2, absent=0, late=0, excused=0, not_taken=1),
        )
        self.assertEqual(self.counts_dict(counts), self.tab_counts(self.zara))

    def test_a_pupil_placed_after_the_last_day_is_not_on_the_roll(self):
        self.assertNotIn(self.obinna.pk, self.summary().students)

    def test_a_pupil_who_moved_class_counts_for_each_class_over_their_own_stretch(self):
        in_a = self.summary(self.jss1a).students[self.wale.pk]
        in_b = self.summary(self.jss1b).students[self.wale.pk]

        self.assertEqual(
            in_a, StudentTermCounts(opened=2, present=0, absent=1, late=0, excused=0, not_taken=1),
        )
        self.assertEqual(
            in_b, StudentTermCounts(opened=2, present=1, absent=0, late=0, excused=0, not_taken=1),
        )
        tab = self.tab_counts(self.wale)
        for key in TAB_KEYS:
            self.assertEqual(getattr(in_a, key) + getattr(in_b, key), tab[key], key)


class DaysNotTakenTests(_Summary):
    def report_not_taken(self, school_class):
        body = build_report(self.tenant, self.admin, term_id=self.first_term.pk)
        return {row["class_id"]: row["counts"]["not_taken"] for row in body["by_class"]}[
            school_class.pk
        ]

    def test_days_not_taken_match_the_reports(self):
        for school_class in (self.jss1a, self.jss1b):
            with self.subTest(school_class=school_class.name):
                self.assertEqual(
                    self.summary(school_class).days_not_taken,
                    self.report_not_taken(school_class),
                )

    def test_the_10th_is_the_one_day_jss1_a_took_no_register(self):
        self.assertEqual(self.summary(self.jss1a).days_not_taken, 1)

    def test_up_to_stops_the_count_at_that_day(self):
        self.assertEqual(self.summary(up_to=D(2025, 10, 9)).days_not_taken, 0)
        self.assertEqual(self.summary(up_to=D(2025, 10, 10)).days_not_taken, 1)

    def test_up_to_cannot_reach_past_the_classes_today(self):
        self.assertEqual(self.summary(up_to=D(2026, 1, 1)), self.summary())

    def test_a_class_with_no_branch_at_a_school_of_several_is_refused(self):
        with self.assertRaises(ClassHasNoBranch):
            self.summary(self.shared)


class QueryCostTests(AttendanceFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        AcademicTerm.all_objects.filter(pk=cls.first_term.pk).update(start_date=D(2025, 10, 6))
        cls.first_term.refresh_from_db()
        cls.small = cls.make_class(cls.jss1, "JSS1 S", "JSS1S", cls.lekki, None)
        cls.big = cls.make_class(cls.jss1, "JSS1 Z", "JSS1Z", cls.lekki, None)
        for school_class, size in ((cls.small, 2), (cls.big, 40)):
            register = ClassRegister.objects.create(
                tenant=cls.tenant, school_class=school_class, date=D(2025, 10, 8),
                branch=cls.lekki, session=cls.year, term=cls.first_term,
                submitted_at=at(8), submitted_by=cls.eze,
            )
            for number in range(size):
                pupil = cls.make_student(
                    "Pupil", f"N{number}", school_class, f"{school_class.code}/{number:03d}",
                )
                StudentMark.objects.create(
                    tenant=cls.tenant, register=register, student=pupil,
                    mark="A" if number % 5 == 0 else "P", marked_by=cls.eze,
                )

    def queries(self, school_class):
        with CaptureQueriesContext(connection) as captured:
            summary = class_term_summary(self.tenant, school_class, self.first_term)
        return len(captured), summary

    def test_the_query_count_does_not_grow_with_the_roll(self):
        # The first read of a process fills the settings caches.
        self.queries(self.small)
        small_count, small = self.queries(self.small)
        big_count, big = self.queries(self.big)

        self.assertEqual((len(small.students), len(big.students)), (2, 40))
        self.assertEqual(big_count, small_count)
        self.assertLessEqual(big_count, 20)
