"""Reminding a class teacher that a register has not been taken."""
from __future__ import annotations

import datetime as dt
from unittest import mock

from ..constants import EVENT_REGISTER_REMINDER
from ..models import RegisterReminder
from .base import FOUNDERS_DAY, LAST_FRIDAY, TODAY, AttendanceFixture

SEND = "schools.vs_attendance.services.reminders.send_notification"


class ReminderTests(AttendanceFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_administrator_reminds_the_class_teacher(self):
        response = self.remind(self.admin, self.jss1a)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.data(response)["class_teacher_name"], "Chidi Eze")
        self.assertTrue(self.data(response)["reminded"])

        call = self.send.call_args.kwargs
        self.assertEqual(call["event_key"], EVENT_REGISTER_REMINDER)
        self.assertEqual(call["recipients"], [self.eze])
        self.assertEqual(call["tenant"], self.tenant)
        self.assertEqual(call["branch"], self.lekki)
        self.assertEqual(call["context"]["class_name"], "JSS1 A")
        self.assertEqual(call["context"]["date"], "13 Oct 2025")
        reminder = RegisterReminder.objects.get(school_class=self.jss1a)
        self.assertEqual(reminder.sent_by, self.admin)

    def test_a_class_is_reminded_once_a_day(self):
        self.remind(self.admin, self.jss1a)
        again = self.remind(self.lekki_admin, self.jss1a)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.code(again), "ALREADY_REMINDED")
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(RegisterReminder.objects.count(), 1)

    def test_another_class_the_same_day_and_the_same_class_another_day_are_separate(self):
        self.remind(self.admin, self.jss1a)
        self.assertEqual(self.remind(self.admin, self.jss1b).status_code, 201)
        self.assertEqual(self.remind(self.admin, self.jss1a, LAST_FRIDAY).status_code, 201)

    def test_a_class_with_no_class_teacher_has_nobody_to_remind(self):
        response = self.remind(self.admin, self.pry4a)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "NO_CLASS_TEACHER")
        self.assertEqual(
            response.data["message"],
            "This class has no class teacher with a login to remind.",
        )
        self.assertEqual(RegisterReminder.objects.count(), 0)
        self.assertEqual(self.send.call_count, 0)

    def test_a_class_teacher_who_has_left_is_nobody_to_remind(self):
        from schools.vs_staff.models import StaffProfile

        StaffProfile.all_objects.filter(pk=self.eze_staff.pk).update(
            employment_status="RESIGNED",
        )
        self.assertEqual(self.code(self.remind(self.admin, self.jss1a)), "NO_CLASS_TEACHER")

    def test_a_class_teacher_who_cannot_sign_in_is_nobody_to_remind(self):
        type(self.eze).objects.filter(pk=self.eze.pk).update(status="SUSPENDED")
        self.assertEqual(self.code(self.remind(self.admin, self.jss1a)), "NO_CLASS_TEACHER")

    def test_a_register_already_submitted_needs_no_reminder(self):
        self.fill(self.eze, self.jss1a)
        self.submit(self.eze, self.jss1a)
        response = self.remind(self.admin, self.jss1a)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "ALREADY_SUBMITTED")

    def test_a_closed_day_and_a_future_day_are_not_reminded(self):
        self.assertEqual(
            self.code(self.remind(self.admin, self.jss1a, FOUNDERS_DAY)),
            "DAY_CLOSED_NO_REGISTER",
        )
        self.assertEqual(
            self.code(self.remind(self.admin, self.jss1a, TODAY + dt.timedelta(days=1))),
            "DATE_IN_FUTURE",
        )

    def test_a_teacher_with_only_the_mark_key_cannot_send_a_reminder(self):
        self.assertEqual(self.remind(self.eze, self.jss1a).status_code, 403)

    def test_a_caller_with_no_attendance_key_cannot_send_a_reminder(self):
        self.assertEqual(self.remind(self.nobody, self.jss1a).status_code, 403)

    def test_another_schools_class_is_a_404(self):
        self.assertEqual(self.remind(self.admin, self.solo_class).status_code, 404)

    def test_a_branch_administrator_reminds_only_their_own_branchs_classes(self):
        self.assertEqual(self.remind(self.ikeja_admin, self.jss1a).status_code, 404)
        self.assertEqual(self.remind(self.lekki_admin, self.jss1a).status_code, 201)
