"""The leave form's live day count: ``GET /staff/<id>/leave/preview/``.

Brightfield runs Lekki and Ikeja; Sunrise has one branch. At Brightfield the
week of 22 December 2025 has three closures: a Lekki-only retreat on Tuesday, an
Ikeja-only flood on Wednesday and a school-wide Christmas Day on Thursday, so the
same five weekdays count differently for Mr Eze at Lekki and Mr Sule at Ikeja.
Sunrise has the same dates and none of those closures.

The refusals come with the reads: a teacher previewing a colleague's leave, a
person from another school, and every malformed query.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_staff.constants import LeaveStatus
from schools.vs_staff.models import LeaveRequest
from schools.vs_staff.services import leave as leave_service

from .test_staff_settings import _LeaveSettingsFixture

WEEK = {"start_date": "2025-12-22", "end_date": "2025-12-26"}


class PreviewFixture(_LeaveSettingsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from schools.vs_calendar.models import CalendarEvent

        def closure(name, day, branch=None):
            return CalendarEvent.all_objects.create(
                tenant=cls.tenant, session=cls.year, branch=branch, name=name,
                event_type="HOLIDAY", start_date=day, end_date=day, closes_school=True,
            )

        closure("Lekki staff retreat", dt.date(2025, 12, 23), cls.lekki)
        closure("Ikeja flooding", dt.date(2025, 12, 24), cls.ikeja)
        closure("Christmas Day", dt.date(2025, 12, 25))

    def preview(self, person, user=None, **params):
        response = self.get(user or self.admin, "staff-leave-preview", params, pk=person.pk)
        return response

    def data(self, person, user=None, **params):
        response = self.preview(person, user, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()["data"]


class DayCountTests(PreviewFixture):
    def test_the_count_leaves_out_the_closures_that_reach_that_persons_branch(self):
        lekki = self.data(self.eze, **WEEK)
        ikeja = self.data(self.ikeja_teacher, **WEEK)

        # Lekki: Monday, Wednesday and Friday. Ikeja: Monday, Tuesday and Friday.
        self.assertEqual(lekki["days"], 3)
        self.assertEqual(ikeja["days"], 3)
        self.assertEqual(self.data(self.eze, start_date="2025-12-23", end_date="2025-12-23")["days"], 0)
        self.assertEqual(self.data(self.ikeja_teacher, start_date="2025-12-23", end_date="2025-12-23")["days"], 1)

    def test_a_school_with_one_branch_sees_only_its_own_closures(self):
        response = self.get(
            self.solo_admin, "staff-leave-preview", WEEK, pk=self.solo_staff.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["days"], 5)

    def test_weekends_are_left_out_and_the_return_is_the_next_working_day(self):
        data = self.data(self.eze, start_date="2025-12-26", end_date="2025-12-28")
        self.assertEqual(data["days"], 1)
        self.assertEqual(data["resumption_date"], "2025-12-29")

    def test_dates_with_no_working_day_answer_zero_rather_than_an_error(self):
        data = self.data(self.eze, start_date="2025-12-27", end_date="2025-12-28")
        self.assertEqual(data["days"], 0)
        self.assertEqual(data["resumption_date"], "2025-12-29")

    def test_it_counts_exactly_what_filing_would_record(self):
        preview = self.data(self.eze, **WEEK)["days"]
        filed = self.file(**WEEK).data["data"]["leave"]["days"]
        self.assertEqual(preview, filed)

    def test_a_school_that_does_not_exclude_closures_counts_them(self):
        self.save_rules(leave={"exclude_closures": False})
        self.assertEqual(self.data(self.eze, **WEEK)["days"], 5)

    def test_it_writes_nothing(self):
        before = LeaveRequest.all_objects.count()
        self.data(self.eze, leave_type="ANNUAL", **WEEK)
        self.assertEqual(LeaveRequest.all_objects.count(), before)


class BalanceTests(PreviewFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.approved = LeaveRequest.all_objects.create(
            tenant=cls.tenant, staff=cls.eze, leave_type="ANNUAL",
            start_date=dt.date(2026, 3, 2), end_date=dt.date(2026, 3, 4), days=3,
            status=LeaveStatus.APPROVED,
        )
        cls.pending = LeaveRequest.all_objects.create(
            tenant=cls.tenant, staff=cls.eze, leave_type="ANNUAL",
            start_date=dt.date(2026, 3, 9), end_date=dt.date(2026, 3, 10), days=2,
            status=LeaveStatus.PENDING,
        )
        # Rejected and cancelled leave are not absences and count for nothing.
        for status in (LeaveStatus.REJECTED, LeaveStatus.CANCELLED):
            LeaveRequest.all_objects.create(
                tenant=cls.tenant, staff=cls.eze, leave_type="ANNUAL",
                start_date=dt.date(2026, 3, 11), end_date=dt.date(2026, 3, 13), days=3,
                status=status,
            )
        # Another person's leave and another type are not this balance.
        LeaveRequest.all_objects.create(
            tenant=cls.tenant, staff=cls.ikeja_teacher, leave_type="ANNUAL",
            start_date=dt.date(2026, 3, 2), end_date=dt.date(2026, 3, 6), days=5,
            status=LeaveStatus.APPROVED,
        )
        LeaveRequest.all_objects.create(
            tenant=cls.tenant, staff=cls.eze, leave_type="SICK",
            start_date=dt.date(2026, 3, 2), end_date=dt.date(2026, 3, 6), days=5,
            status=LeaveStatus.APPROVED,
        )

    def setUp(self):
        super().setUp()
        self.save_rules(leave={"allowances": {"ANNUAL": 10}})

    def test_it_says_what_is_left_before_and_after_these_dates(self):
        data = self.data(
            self.eze, leave_type="ANNUAL",
            start_date="2026-03-16", end_date="2026-03-20",
        )
        self.assertEqual(data, {
            "days": 5, "resumption_date": "2026-03-23", "leave_type": "ANNUAL",
            "allowance": 10, "taken": 3, "pending": 2, "remaining": 5,
            "remaining_after": 0, "over_allowance_by": 0,
            "session": {"id": self.year.pk, "name": "2025/2026"},
        })

    def test_going_past_the_allowance_goes_negative_and_matches_filing(self):
        data = self.data(
            self.eze, leave_type="ANNUAL",
            start_date="2026-03-16", end_date="2026-03-24",
        )
        self.assertEqual((data["days"], data["remaining_after"], data["over_allowance_by"]), (7, -2, 2))
        self.assertEqual(
            data["over_allowance_by"],
            leave_service.over_allowance(
                self.eze, "ANNUAL", dt.date(2026, 3, 16), data["days"],
            ),
        )

    def test_a_request_being_corrected_is_not_counted_against_itself(self):
        data = self.data(
            self.eze, leave_type="ANNUAL", exclude=self.pending.pk,
            start_date="2026-03-09", end_date="2026-03-10",
        )
        self.assertEqual((data["pending"], data["remaining"], data["remaining_after"]), (0, 7, 5))

    def test_a_type_with_no_limit_has_no_allowance_or_remaining(self):
        data = self.data(
            self.eze, leave_type="SICK", start_date="2026-03-16", end_date="2026-03-17",
        )
        self.assertEqual(
            (data["allowance"], data["taken"], data["remaining"],
             data["remaining_after"], data["over_allowance_by"]),
            (None, 5, None, None, 0),
        )

    def test_without_a_type_there_is_no_balance(self):
        data = self.data(self.eze, start_date="2026-03-16", end_date="2026-03-17")
        self.assertEqual(
            (data["leave_type"], data["allowance"], data["taken"], data["pending"],
             data["remaining"], data["remaining_after"]),
            (None, None, None, None, None, None),
        )
        self.assertEqual(data["days"], 2)

    def test_dates_outside_every_session_have_no_balance(self):
        data = self.data(
            self.eze, leave_type="ANNUAL", start_date="2030-03-04", end_date="2030-03-05",
        )
        self.assertIsNone(data["session"])
        self.assertEqual(
            (data["allowance"], data["taken"], data["remaining"]), (None, None, None),
        )

    def test_the_session_is_the_one_the_leave_starts_in(self):
        data = self.data(
            self.eze, leave_type="ANNUAL", start_date="2025-03-03", end_date="2025-03-04",
        )
        self.assertEqual(data["session"]["name"], "2024/2025")
        self.assertEqual(data["taken"], 0)


class ExcludeTests(PreviewFixture):
    def test_exclude_must_be_this_persons_request(self):
        theirs = LeaveRequest.all_objects.create(
            tenant=self.tenant, staff=self.ikeja_teacher, leave_type="ANNUAL",
            start_date=dt.date(2026, 3, 2), end_date=dt.date(2026, 3, 4), days=3,
            status=LeaveStatus.PENDING,
        )
        response = self.preview(
            self.eze, leave_type="ANNUAL", exclude=theirs.pk,
            start_date="2026-03-02", end_date="2026-03-04",
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_another_schools_request_is_a_404(self):
        foreign = LeaveRequest.all_objects.create(
            tenant=self.solo.tenant, staff=self.solo_staff, leave_type="ANNUAL",
            start_date=dt.date(2026, 3, 2), end_date=dt.date(2026, 3, 4), days=3,
            status=LeaveStatus.PENDING,
        )
        response = self.preview(
            self.eze, exclude=foreign.pk, start_date="2026-03-02", end_date="2026-03-04",
        )
        self.assertEqual(response.status_code, 404, response.data)


class WhoMayPreviewTests(PreviewFixture):
    def test_a_teacher_may_preview_their_own_leave(self):
        data = self.data(self.eze, user=self.eze.user, **WEEK)
        self.assertEqual(data["days"], 3)

    def test_a_teacher_may_not_preview_a_colleagues_leave(self):
        response = self.preview(self.ikeja_teacher, user=self.eze.user, **WEEK)
        self.assertEqual(response.status_code, 403, response.data)

    def test_somebody_holding_no_leave_key_is_refused(self):
        response = self.preview(self.eze, user=self.nobody, **WEEK)
        self.assertEqual(response.status_code, 403, response.data)

    def test_an_administrator_pinned_to_one_branch_cannot_reach_the_other(self):
        response = self.preview(self.ikeja_teacher, user=self.lekki_head, **WEEK)
        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(self.data(self.eze, user=self.lekki_head, **WEEK)["days"], 3)

    def test_another_schools_person_is_a_404_not_a_403(self):
        response = self.preview(self.solo_staff, **WEEK)
        self.assertEqual(response.status_code, 404, response.data)

    def test_it_needs_a_sign_in(self):
        from django.urls import reverse
        from rest_framework.test import APIClient

        url = reverse("staff-leave-preview", kwargs={"pk": self.eze.pk})
        response = APIClient().get(url, {"tenant": self.tenant.slug, **WEEK})
        self.assertIn(response.status_code, (401, 403))


class ValidationTests(PreviewFixture):
    def refused(self, **params):
        response = self.preview(self.eze, **params)
        self.assertEqual(response.status_code, 400, response.data)
        return str(response.data)

    def test_both_dates_are_required(self):
        self.assertIn("start_date", self.refused(end_date="2025-12-26"))
        self.assertIn("end_date", self.refused(start_date="2025-12-22"))

    def test_a_date_must_be_a_date(self):
        self.assertIn("start_date", self.refused(start_date="soon", end_date="2025-12-26"))

    def test_an_end_before_the_start_is_refused_on_the_end_date(self):
        self.assertIn(
            "end_date", self.refused(start_date="2025-12-26", end_date="2025-12-22"),
        )

    def test_a_range_over_a_year_is_refused(self):
        self.assertIn(
            "end_date", self.refused(start_date="2025-01-01", end_date="2026-01-02"),
        )
        # 366 days, counting both ends, is the most a preview answers.
        data = self.data(self.eze, start_date="2025-01-01", end_date="2026-01-01")
        self.assertGreater(data["days"], 200)

    def test_an_unknown_leave_type_is_refused(self):
        self.assertIn("leave_type", self.refused(leave_type="HOLIDAY", **WEEK))

    def test_exclude_must_be_a_number(self):
        self.assertIn("exclude", self.refused(exclude="abc", **WEEK))


class QueryCostTests(PreviewFixture):
    def test_the_cost_does_not_grow_with_the_leave_on_record(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        params = {"leave_type": "ANNUAL", "start_date": "2026-03-16", "end_date": "2026-03-20"}
        self.data(self.eze, **params)
        with CaptureQueriesContext(connection) as few:
            self.data(self.eze, **params)
        for offset in range(8):
            LeaveRequest.all_objects.create(
                tenant=self.tenant, staff=self.eze, leave_type="ANNUAL",
                start_date=dt.date(2026, 4, 1) + dt.timedelta(days=offset * 7),
                end_date=dt.date(2026, 4, 2) + dt.timedelta(days=offset * 7),
                days=2, status=LeaveStatus.PENDING,
            )
        with CaptureQueriesContext(connection) as many:
            self.data(self.eze, **params)
        self.assertEqual(len(few), len(many))
