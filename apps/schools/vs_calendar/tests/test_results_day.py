"""The date a term's results are released, read from the calendar.

Brightfield has two branches. A results day with no branch is the whole
school's; one that names a branch is that branch's own and wins there alone.
"""
from __future__ import annotations

import datetime as dt

from ..models import CalendarEvent, EventType
from ..services.calendar import results_day
from .base import _Base


class ResultsDayTests(_Base):
    def put(self, day, *, branch=None, event_type=EventType.RESULTS_DAY, session=None):
        return CalendarEvent.all_objects.create(
            tenant=self.tenant, session=session or self.year,
            name="Release of results", event_type=event_type, branch=branch,
            start_date=day, end_date=day,
        )

    def test_a_term_with_none_has_none(self):
        self.assertIsNone(results_day(self.tenant, self.first_term))
        self.assertIsNone(results_day(self.tenant, self.first_term, self.lekki))

    def test_other_kinds_of_event_are_not_a_results_day(self):
        self.put(dt.date(2025, 12, 10), event_type=EventType.SCHOOL_EVENT)

        self.assertIsNone(results_day(self.tenant, self.first_term))

    def test_a_school_wide_results_day_applies_to_every_branch(self):
        self.put(dt.date(2025, 12, 12))

        self.assertEqual(
            results_day(self.tenant, self.first_term), dt.date(2025, 12, 12),
        )
        for branch in (self.lekki, self.ikeja):
            self.assertEqual(
                results_day(self.tenant, self.first_term, branch),
                dt.date(2025, 12, 12),
            )

    def test_the_earliest_inside_the_term_is_the_one(self):
        self.put(dt.date(2025, 12, 12))
        self.put(dt.date(2025, 12, 5))

        self.assertEqual(
            results_day(self.tenant, self.first_term), dt.date(2025, 12, 5),
        )

    def test_a_branchs_own_date_wins_for_that_branch_only(self):
        self.put(dt.date(2025, 12, 5))
        self.put(dt.date(2025, 12, 11), branch=self.lekki)

        self.assertEqual(
            results_day(self.tenant, self.first_term, self.lekki),
            dt.date(2025, 12, 11),
        )
        self.assertEqual(
            results_day(self.tenant, self.first_term, self.ikeja),
            dt.date(2025, 12, 5),
        )
        self.assertEqual(
            results_day(self.tenant, self.first_term), dt.date(2025, 12, 5),
        )

    def test_a_branch_date_alone_is_not_the_schools(self):
        self.put(dt.date(2025, 12, 10), branch=self.lekki)

        self.assertIsNone(results_day(self.tenant, self.first_term))
        self.assertIsNone(results_day(self.tenant, self.first_term, self.ikeja))
        self.assertEqual(
            results_day(self.tenant, self.first_term, self.lekki),
            dt.date(2025, 12, 10),
        )

    def test_a_date_outside_the_term_is_ignored(self):
        self.put(dt.date(2025, 12, 20))
        self.put(dt.date(2026, 1, 20))

        self.assertIsNone(results_day(self.tenant, self.first_term))
        self.assertEqual(
            results_day(self.tenant, self.second_term), dt.date(2026, 1, 20),
        )

    def test_another_schools_calendar_is_never_read(self):
        CalendarEvent.all_objects.create(
            tenant=self.other.tenant, session=self.other_year,
            name="Release of results", event_type=EventType.RESULTS_DAY,
            start_date=dt.date(2025, 12, 10), end_date=dt.date(2025, 12, 10),
        )

        self.assertIsNone(results_day(self.tenant, self.first_term))

    def test_a_results_day_does_not_close_the_school_by_default(self):
        from ..services.calendar_rules import DEFAULT_CLOSES_SCHOOL

        self.assertFalse(DEFAULT_CLOSES_SCHOOL[EventType.RESULTS_DAY.value])
