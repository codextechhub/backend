"""A teacher's next lesson on their own Today read (``today/mine/``).

Mr Eze teaches JSS1 A English in Period 1 (08:00) on Monday, Tuesday and
Thursday. The fixture clock reads Monday 13 October 2025 at 13:00 in Lagos, so
Monday's lesson is over and his next is Tuesday's.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_calendar.models import CalendarEvent, PublishState

from .base import TODAY, school_clock
from .lesson_base import LessonFixture, publish

TUESDAY = TODAY + dt.timedelta(days=1)
THURSDAY = TODAY + dt.timedelta(days=3)


class NextLessonTests(LessonFixture):
    def next_lesson(self, user=None, **params):
        response = self.today_mine(user or self.eze, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)["next_lesson"]

    def test_a_lesson_already_over_today_gives_way_to_the_next_school_day(self):
        lesson = self.next_lesson()
        self.assertEqual(lesson["date"], str(TUESDAY))
        self.assertFalse(lesson["is_today"])
        self.assertEqual(
            (lesson["period_label"], lesson["start_time"], lesson["subject"], lesson["class_name"]),
            ("Period 1", "08:00", "English Language", "JSS1 A"),
        )

    def test_a_lesson_still_to_come_today_is_the_next_one(self):
        # 06:00 UTC is 07:00 in Lagos, before Period 1.
        with school_clock(dt.datetime(2025, 10, 13, 6, 0, tzinfo=dt.timezone.utc)):
            lesson = self.next_lesson()
        self.assertEqual(lesson["date"], str(TODAY))
        self.assertTrue(lesson["is_today"])

    def test_a_closed_day_is_skipped(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=None, name="Mid-term break",
            event_type="HOLIDAY", start_date=TUESDAY, end_date=TUESDAY, closes_school=True,
        )
        self.assertEqual(self.next_lesson()["date"], str(THURSDAY))

    def test_a_draft_timetable_has_no_lessons_to_announce(self):
        publish(self.tenant, self.jss1a, status=PublishState.DRAFT)
        self.assertIsNone(self.next_lesson())

    def test_it_does_not_depend_on_lesson_marking(self):
        self.switch_lessons_off()
        self.assertEqual(self.next_lesson()["date"], str(TUESDAY))

    def test_a_day_looked_back_at_has_no_next_lesson(self):
        self.assertIsNone(self.next_lesson(date=str(TODAY - dt.timedelta(days=3))))

    def test_somebody_who_teaches_nothing_has_none(self):
        self.assertIsNone(self.next_lesson(self.admin))
