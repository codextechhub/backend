"""The bell schedule, and the one rule a school has to be told rather than infer."""
from __future__ import annotations

import datetime as dt

from ..models import Period, PeriodType
from .base import _Base


class PeriodRuleTests(_Base):
    def test_the_end_time_must_be_after_the_start(self):
        response = self.post(self.admin, "calendar-period-list", {
            "label": "Backwards", "start_time": "10:00", "end_time": "09:00",
            "period_type": "LESSON",
        })
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "PERIOD_TIME_INVALID")

    def test_an_overlap_on_the_same_day_and_scope_is_refused(self):
        response = self.post(self.admin, "calendar-period-list", {
            "label": "Clash", "start_time": "08:30", "end_time": "09:00",
            "period_type": "LESSON",
        })
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "PERIOD_OVERLAP")
        # The message names what it collided with, because that is what the
        # person has to go and look at.
        self.assertIn("Period 1", response.data["message"])

    def test_the_same_times_at_another_branch_are_fine(self):
        """Lekki starting at 08:00 does not stop Ikeja starting at 08:00."""
        response = self.post(self.admin, "calendar-period-list", {
            "label": "Ikeja Period 1", "start_time": "08:00",
            "end_time": "08:45", "period_type": "LESSON",
            "branch": self.ikeja.pk,
        })
        self.assertEqual(response.status_code, 201, response.data)

    def test_order_is_computed_from_the_times_not_supplied(self):
        """The form has no order field, so the server assigns it."""
        response = self.post(self.admin, "calendar-period-list", {
            "label": "Assembly", "start_time": "07:30", "end_time": "07:55",
            "period_type": "ASSEMBLY", "order_index": 99,
        })
        self.assertEqual(response.status_code, 201, response.data)
        # Earliest in the day, so first - regardless of what was sent.
        self.assertEqual(response.data["data"]["order_index"], 1)
        self.assertEqual(
            Period.all_objects.get(label="Period 1").order_index, 2,
        )


class DayOverrideTests(_Base):
    """A weekday with its own periods REPLACES the everyday schedule."""

    def setUp(self):
        # Friday runs a short day: assembly and one lesson.
        Period.all_objects.create(
            tenant=self.tenant, session=self.year, day_of_week=5,
            order_index=1, label="Assembly", period_type=PeriodType.ASSEMBLY,
            start_time=dt.time(8, 0), end_time=dt.time(8, 30),
        )
        Period.all_objects.create(
            tenant=self.tenant, session=self.year, day_of_week=5,
            order_index=2, label="Period 1", period_type=PeriodType.LESSON,
            start_time=dt.time(8, 30), end_time=dt.time(9, 15),
        )

    def test_friday_runs_only_its_own_periods(self):
        response = self.get(self.admin, "calendar-period-list", {"day": 5})
        labels = [p["label"] for p in response.data["data"]["periods"]]
        # Two, not five: an override is wholesale, never additive.
        self.assertEqual(labels, ["Assembly", "Period 1"])

    def test_monday_still_runs_the_everyday_schedule(self):
        response = self.get(self.admin, "calendar-period-list", {"day": 1})
        labels = [p["label"] for p in response.data["data"]["periods"]]
        self.assertEqual(labels, ["Period 1", "Period 2", "Break"])
        self.assertFalse(response.data["data"]["has_own_schedule"])

    def test_the_note_says_so_in_the_words_the_screen_renders(self):
        response = self.get(self.admin, "calendar-period-list", {"day": 5})
        self.assertEqual(
            response.data["data"]["note"],
            "Friday uses its own schedule (2 periods). The everyday schedule "
            "does not apply.",
        )


class PeriodDeleteTests(_Base):
    def test_a_period_holding_lessons_cannot_be_deleted(self):
        from ..models import TimetableSlot

        TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.jss1a,
            day_of_week=1, period=self.p1, subject=self.maths,
        )
        response = self.delete(self.admin, "calendar-period-detail", pk=self.p1.pk)
        self.assertEqual(response.status_code, 409, response.data)

    def test_an_unused_period_can_be_deleted_and_the_day_renumbers(self):
        response = self.delete(self.admin, "calendar-period-detail", pk=self.p1.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Period.all_objects.get(pk=self.p2.pk).order_index, 1)


class PeriodSecurityTests(_Base):
    def test_another_tenants_period_answers_404(self):
        theirs = Period.all_objects.create(
            tenant=self.other.tenant, session=self.other_year, order_index=1,
            label="Theirs", period_type=PeriodType.LESSON,
            start_time=dt.time(8, 0), end_time=dt.time(9, 0),
        )
        response = self.get(self.admin, "calendar-period-detail", pk=theirs.pk)
        self.assertEqual(response.status_code, 404, response.data)

    def test_writing_a_period_needs_the_create_key(self):
        from vs_rbac.models import TenantRolePermission

        TenantRolePermission.objects.filter(
            role=self.role, permission__key="academics.timetable.create",
        ).update(granted=False)
        response = self.post(self.admin, "calendar-period-list", {
            "label": "P3", "start_time": "10:00", "end_time": "10:45",
            "period_type": "LESSON",
        })
        self.assertEqual(response.status_code, 403, response.data)
