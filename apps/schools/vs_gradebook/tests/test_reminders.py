"""Reminding a sheet's main teacher."""
from __future__ import annotations

import datetime as dt
from unittest import mock

from schools.vs_gradebook.constants import SheetStatus
from schools.vs_gradebook.models import ClassTerm, ScoreSheet, SheetReminder
from schools.vs_staff.models import StaffProfile

from .base import school_clock
from .sheet_base import SheetFixture

SEND = "schools.vs_gradebook.services.reminders.send_notification"
TOMORROW = dt.datetime(2025, 10, 14, 12, 0, tzinfo=dt.timezone.utc)


class ReminderTests(SheetFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_administrator_reminds_the_main_teacher(self):
        response = self.remind(self.admin, self.jss1a, self.maths)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.data(response)["reminded_today"])
        self.assertEqual(self.data(response)["main_teacher"]["name"], "Chidi Eze")

        call = self.send.call_args.kwargs
        self.assertEqual(call["event_key"], "gradebook.sheet_reminder")
        self.assertEqual(call["recipients"], [self.eze])
        self.assertEqual(call["tenant"], self.tenant)
        self.assertEqual(call["branch"], self.lekki)
        self.assertEqual(call["context"]["subject_name"], "Mathematics")
        self.assertEqual(call["context"]["class_name"], "JSS1 A")
        self.assertEqual(call["context"]["term_name"], "First Term")
        reminder = SheetReminder.objects.get()
        self.assertEqual((reminder.sent_by, reminder.on_date), (self.admin, dt.date(2025, 10, 13)))

    def test_a_sheet_nobody_started_gets_its_row_so_the_reminder_has_somewhere_to_live(self):
        self.remind(self.admin, self.jss1a, self.maths)
        sheet = ScoreSheet.objects.get()
        self.assertEqual(sheet.status, SheetStatus.NOT_STARTED)
        self.assertIsNone(ClassTerm.objects.get().structure)

    def test_a_sheet_is_reminded_once_a_day(self):
        self.remind(self.admin, self.jss1a, self.maths)
        again = self.remind(self.lekki_admin, self.jss1a, self.maths)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.code(again), "ALREADY_REMINDED")
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(SheetReminder.objects.count(), 1)

    def test_another_day_and_another_sheet_are_separate(self):
        self.remind(self.admin, self.jss1a, self.maths)
        with school_clock(TOMORROW):
            self.assertEqual(self.remind(self.admin, self.jss1a, self.maths).status_code, 200)
        self.assertEqual(SheetReminder.objects.count(), 2)

    def test_a_sheet_that_is_in_has_nothing_to_remind(self):
        self.make_sheet(self.jss1a, self.maths, status=SheetStatus.SUBMITTED)
        response = self.remind(self.admin, self.jss1a, self.maths)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "SHEET_LOCKED")
        self.assertEqual(self.send.call_count, 0)

    def test_a_returned_sheet_can_be_reminded(self):
        self.make_sheet(self.jss1a, self.maths, status=SheetStatus.RETURNED)
        self.assertEqual(self.remind(self.admin, self.jss1a, self.maths).status_code, 200)

    def test_a_subject_with_no_main_teacher_has_nobody_to_remind(self):
        response = self.remind(self.admin, self.jss1b, self.maths)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "NO_MAIN_TEACHER")
        self.assertEqual(SheetReminder.objects.count(), 0)

    def test_a_teacher_who_cannot_sign_in_is_unreachable_and_the_refusal_says_so(self):
        type(self.eze).objects.filter(pk=self.eze.pk).update(status="LOCKED")
        response = self.remind(self.admin, self.jss1a, self.maths)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "TEACHER_UNREACHABLE")
        self.assertIn("cannot sign in", response.data["message"])
        self.assertEqual(SheetReminder.objects.count(), 0)
        self.assertEqual(self.send.call_count, 0)

    def test_a_teacher_who_has_left_is_unreachable_and_the_refusal_says_so(self):
        StaffProfile.all_objects.filter(pk=self.eze_staff.pk).update(employment_status="RESIGNED")
        response = self.remind(self.admin, self.jss1a, self.maths)
        self.assertEqual(self.code(response), "TEACHER_UNREACHABLE")
        self.assertIn("has left the school", response.data["message"])

    def test_the_key_is_required(self):
        for user in (self.eze, self.nobody, self.hub_reader, self.enterer):
            with self.subTest(user=user.email):
                self.assertEqual(self.remind(user, self.jss1a, self.maths).status_code, 403)

    def test_another_schools_sheet_and_one_outside_the_branch_are_a_404(self):
        self.assertEqual(self.remind(self.solo_admin, self.jss1a, self.maths).status_code, 404)
        self.assertEqual(self.remind(self.lekki_admin, self.jss1c, self.maths).status_code, 404)
        self.assertEqual(self.remind(self.admin, self.sss1a, self.french).status_code, 404)

    def test_a_branch_administrator_reminds_within_their_branch(self):
        self.assertEqual(self.remind(self.lekki_admin, self.jss1a, self.maths).status_code, 200)

    def test_a_reminder_is_audited(self):
        from vs_audit.models import AuditEvent

        self.remind(self.admin, self.jss1a, self.maths)
        event = AuditEvent.objects.get(module_key="GRADEBOOK", entity_type="ScoreSheet")
        self.assertIn("reminded Chidi Eze about Mathematics, JSS1 A, First Term", event.summary)

    def test_a_single_branch_school_names_no_branch_in_the_notice(self):
        response = self.call(
            "post", self.solo_admin, "gradebook-sheet-remind", term=self.solo_term,
            class_id=self.solo_class.pk, subject_id=self.solo_maths.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.send.call_args.kwargs["context"]["branch_name"], "")
