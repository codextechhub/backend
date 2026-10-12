"""Changing a register after it has been submitted.

Until the day closes the person who submitted it can still change a row, every
change is kept, and nobody else can without the key to take any class. Once the
day has closed only a correction changes a mark. The day closes on the school's
own setting, ``attendance.edit_window_days``, whose default is the same day.
"""
from __future__ import annotations

import datetime as dt

from django.db.models import ProtectedError

from vs_audit.models import AuditEvent

from ..models import MarkChange, StudentMark
from ..constants import ChangeVia
from .base import (
    LAST_FRIDAY,
    NOW,
    TODAY,
    AttendanceFixture,
    school_clock,
    set_setting,
)

TUESDAY = NOW + dt.timedelta(days=1)
WEDNESDAY = NOW + dt.timedelta(days=2)


class _Submitted(AttendanceFixture):
    """JSS1 A, submitted by Mr Eze with Chiamaka absent."""

    def setUp(self):
        super().setUp()
        for pupil, kind in ((self.tunde, "P"), (self.kelechi, "P"), (self.chiamaka, "A")):
            self.assertEqual(self.mark(self.eze, self.jss1a, pupil, kind).status_code, 200)
        self.assertEqual(self.submit(self.eze, self.jss1a).status_code, 200)


class SameDayChangeTests(_Submitted):
    def test_the_submitter_can_still_change_a_row_and_the_change_is_kept(self):
        response = self.mark(self.eze, self.jss1a, self.chiamaka, "L", arrived_at="09:10")
        self.assertEqual(response.status_code, 200, response.data)

        change = MarkChange.objects.get()
        self.assertEqual((change.from_mark, change.to_mark), ("A", "L"))
        self.assertEqual(change.via, ChangeVia.SAME_DAY)
        self.assertEqual(change.changed_by, self.eze)
        self.assertEqual(StudentMark.objects.get(student=self.chiamaka).mark, "L")

    def test_the_history_is_read_with_the_register(self):
        self.mark(self.eze, self.jss1a, self.chiamaka, "L", arrived_at="09:10")
        [entry] = self.data(self.read(self.eze, self.jss1a))["history"]
        self.assertEqual(entry["student_name"], "Chiamaka Nwosu")
        self.assertEqual((entry["from_mark"], entry["to_mark"]), ("A", "L"))
        self.assertEqual(entry["changed_by_name"], "Chidi Eze")
        self.assertEqual(entry["via"], "SAME_DAY")

    def test_every_change_is_audited(self):
        self.mark(self.eze, self.jss1a, self.chiamaka, "P")
        event = AuditEvent.objects.get(
            tenant=self.tenant, entity_type="ClassRegister",
            summary__contains="changed from",
        )
        self.assertIn("Chiamaka Nwosu changed from Absent to Present", event.summary)
        self.assertEqual(event.actor_user_id, self.eze.pk)
        self.assertEqual(event.module_key, "ATTENDANCE")

    def test_saving_a_row_unchanged_keeps_no_history(self):
        self.mark(self.eze, self.jss1a, self.chiamaka, "A")
        self.assertEqual(MarkChange.objects.count(), 0)

    def test_an_administrator_can_change_a_register_somebody_else_submitted(self):
        self.assertEqual(self.mark(self.admin, self.jss1a, self.chiamaka, "P").status_code, 200)
        self.assertEqual(MarkChange.objects.get().changed_by, self.admin)

    def test_the_class_teacher_cannot_change_a_register_an_administrator_submitted(self):
        zainab = self.make_student("Zainab", "Aliyu", self.jss1b, "BFS/777")
        self.assertEqual(self.mark(self.admin, self.jss1b, zainab, "P").status_code, 200)
        self.assertEqual(self.submit(self.admin, self.jss1b).status_code, 200)

        response = self.mark(self.bello, self.jss1b, zainab, "A")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.code(response), "ONLY_SUBMITTER_MAY_CHANGE")
        self.assertEqual(StudentMark.objects.get(student=zainab).mark, "P")

    def test_a_child_placed_after_submission_is_filled_in_as_a_recorded_change(self):
        newcomer = self.make_student("Ugo", "Dike", self.jss1a, "BFS/778")
        self.assertEqual(self.fill(self.eze, self.jss1a).status_code, 200)
        self.assertEqual(StudentMark.objects.get(student=newcomer).mark, "P")
        change = MarkChange.objects.get()
        self.assertEqual((change.from_mark, change.to_mark), ("", "P"))

    def test_a_mark_change_is_history_and_is_never_edited_or_deleted(self):
        self.mark(self.eze, self.jss1a, self.chiamaka, "P")
        change = MarkChange.objects.get()
        change.note = "rewritten"
        with self.assertRaises(RuntimeError):
            change.save()
        with self.assertRaises(RuntimeError):
            change.delete()
        with self.assertRaises(ProtectedError):
            StudentMark.objects.get(student=self.chiamaka).delete()


class EditWindowTests(AttendanceFixture):
    def submit_all_present(self, user, on_date):
        self.assertEqual(self.fill(user, self.jss1a, on_date).status_code, 200)
        self.assertEqual(self.submit(user, self.jss1a, on_date).status_code, 200)

    def test_once_the_day_has_closed_a_submitted_register_changes_only_by_correction(self):
        with school_clock(NOW - dt.timedelta(days=3)):
            self.submit_all_present(self.eze, LAST_FRIDAY)
        response = self.mark(self.eze, self.jss1a, self.tunde, "A", on_date=LAST_FRIDAY)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "DAY_CLOSED_USE_CORRECTION")
        self.assertEqual(response.data["message"], "This day is closed. Request a correction instead.")
        self.assertEqual(StudentMark.objects.get(student=self.tunde).mark, "P")
        self.assertEqual(MarkChange.objects.count(), 0)

    def test_the_administrator_is_held_to_the_same_window(self):
        with school_clock(NOW - dt.timedelta(days=3)):
            self.submit_all_present(self.eze, LAST_FRIDAY)
        response = self.mark(self.admin, self.jss1a, self.tunde, "A", on_date=LAST_FRIDAY)
        self.assertEqual(self.code(response), "DAY_CLOSED_USE_CORRECTION")
        self.assertEqual(self.fill(self.admin, self.jss1a, LAST_FRIDAY).status_code, 200)

    def test_by_default_the_next_day_is_too_late(self):
        self.submit_all_present(self.eze, TODAY)
        with school_clock(TUESDAY):
            response = self.mark(self.eze, self.jss1a, self.tunde, "A")
        self.assertEqual(self.code(response), "DAY_CLOSED_USE_CORRECTION")

    def test_a_window_of_one_day_allows_the_next_day_and_not_the_one_after(self):
        set_setting("attendance.edit_window_days", 1, self.tenant, self.admin)
        self.submit_all_present(self.eze, TODAY)
        with school_clock(TUESDAY):
            self.assertEqual(self.mark(self.eze, self.jss1a, self.tunde, "A").status_code, 200)
        with school_clock(WEDNESDAY):
            late = self.mark(self.eze, self.jss1a, self.kelechi, "A")
        self.assertEqual(self.code(late), "DAY_CLOSED_USE_CORRECTION")

    def test_the_window_is_the_schools_and_not_another_schools(self):
        set_setting("attendance.edit_window_days", 5, self.solo, self.solo_admin)
        self.submit_all_present(self.eze, TODAY)
        with school_clock(TUESDAY):
            late = self.mark(self.eze, self.jss1a, self.tunde, "A")
        self.assertEqual(self.code(late), "DAY_CLOSED_USE_CORRECTION")

    def test_the_read_says_whether_the_window_is_open(self):
        self.submit_all_present(self.eze, TODAY)
        self.assertTrue(self.data(self.read(self.eze, self.jss1a))["edit_window_open"])
        with school_clock(TUESDAY):
            self.assertFalse(self.data(self.read(self.eze, self.jss1a))["edit_window_open"])

    def test_a_register_nobody_submitted_has_no_window(self):
        self.assertIsNone(self.data(self.read(self.eze, self.jss1a))["edit_window_open"])
