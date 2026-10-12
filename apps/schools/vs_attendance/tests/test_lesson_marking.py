"""Marking a lesson: who may, what a mark holds, and how a lesson register is closed.

Monday 13 October 2025 at Brightfield. Mr Eze teaches JSS1 A English, Ms Bello
teaches JSS1 A Mathematics, and the registrar (Ngozi, whole school) holds
``mark_any``. JSS1 A has Tunde, Kelechi and Chiamaka on the roll and Amina
suspended.
"""
from __future__ import annotations

import datetime as dt

from django.core import mail
from django.db import IntegrityError, transaction
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_calendar.models import TimetableSlot
from schools.vs_students.models import ClassEnrolment
from vs_notifications.models import Notification
from vs_rbac.tests.helpers import make_assignment, make_school_admin

from ..constants import CFG_EDIT_WINDOW_DAYS, PERM_REGISTER_VIEW
from ..models import CorrectionRequest, LessonMark, LessonMarkChange, LessonRegister, StudentMark
from .base import FOUNDERS_DAY, TODAY, make_key_role, set_setting
from .lesson_base import LAST_MONDAY, LessonFixture, make_slot, publish


class WhoMayMarkTests(LessonFixture):
    def test_the_teacher_of_a_lesson_marks_it(self):
        response = self.mark_lesson(self.eze, self.a_english, self.tunde, "A", note="Not in class")
        self.assertEqual(response.status_code, 200, response.data)
        row = self.lesson_rows(response)[self.tunde.pk]
        self.assertEqual((row["mark"], row["note"]), ("A", "Not in class"))
        self.assertEqual(LessonRegister.objects.get().teacher, self.eze)

    def test_a_teacher_cannot_mark_a_lesson_they_do_not_teach(self):
        for response in (
            self.mark_lesson(self.eze, self.a_maths, self.tunde, "P"),
            self.fill_lesson(self.eze, self.a_maths),
            self.submit_lesson(self.eze, self.a_maths),
        ):
            self.assertEqual(response.status_code, 403, response.data)
            self.assertEqual(self.code(response), "NOT_YOUR_LESSON")
            self.assertIn("lessons you teach", response.data["message"])
        self.assertFalse(LessonRegister.objects.exists())
        self.assertFalse(LessonMark.objects.exists())

    def test_being_the_class_teacher_does_not_make_a_lesson_yours(self):
        # Mr Eze leads JSS1 A, but Ms Bello teaches its Mathematics.
        self.assertEqual(
            self.mark_lesson(self.eze, self.a_maths, self.tunde, "P").status_code, 403,
        )

    def test_a_holder_of_mark_any_marks_any_lesson_of_a_class_they_can_open(self):
        response = self.mark_lesson(self.admin, self.a_maths, self.tunde, "L", arrived_at="08:50")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.lesson_rows(response)[self.tunde.pk]["arrived_at"], "08:50")
        self.assertEqual(LessonRegister.objects.get().teacher, self.bello)

    def test_a_teacher_without_the_mark_key_cannot_mark_even_their_own_lesson(self):
        viewer = make_school_admin(
            None, email="viewer@brightfield.test", tenant=self.tenant,
        )
        role = make_key_role(self.tenant, "Viewer", "viewer", (PERM_REGISTER_VIEW,))
        make_assignment(self.tenant, viewer, role, branch=None)
        TimetableSlot.objects.filter(pk=self.a_english.pk).update(teacher=viewer)
        self.assertEqual(self.lesson(viewer, self.a_english).status_code, 200)
        self.assertEqual(self.mark_lesson(viewer, self.a_english, self.tunde, "P").status_code, 403)
        self.assertEqual(self.fill_lesson(viewer, self.a_english).status_code, 403)
        self.assertEqual(self.submit_lesson(viewer, self.a_english).status_code, 403)

    def test_a_caller_with_no_attendance_key_is_refused_everywhere(self):
        for response in (
            self.lesson(self.nobody, self.a_english),
            self.lessons(self.nobody),
            self.mark_lesson(self.nobody, self.a_english, self.tunde, "P"),
            self.fill_lesson(self.nobody, self.a_english),
            self.submit_lesson(self.nobody, self.a_english),
        ):
            self.assertEqual(response.status_code, 403, response.data)

    def test_a_lesson_handed_to_another_teacher_is_theirs_to_mark(self):
        TimetableSlot.objects.filter(pk=self.a_english.pk).update(teacher=self.bello)
        self.assertEqual(self.mark_lesson(self.eze, self.a_english, self.tunde, "P").status_code, 403)
        self.assertEqual(self.mark_lesson(self.bello, self.a_english, self.tunde, "P").status_code, 200)


class IsolationTests(LessonFixture):
    def test_another_schools_lesson_is_a_404_whatever_the_key(self):
        for response in (
            self.lesson(self.solo_admin, self.a_english),
            self.mark_lesson(self.solo_teacher, self.a_english, self.solo_pupil, "P"),
            self.fill_lesson(self.solo_admin, self.a_english),
            self.submit_lesson(self.solo_admin, self.a_english),
            self.lesson(self.admin, self.solo_slot),
        ):
            self.assertEqual(response.status_code, 404, response.data)

    def test_another_schools_student_is_a_404(self):
        response = self.mark_lesson(self.eze, self.a_english, self.solo_pupil, "P")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(LessonMark.objects.exists())

    def test_a_student_not_on_the_roster_that_day_is_a_404(self):
        # Obinna is placed from 20 October.
        response = self.mark_lesson(self.eze, self.a_english, self.obinna, "P")
        self.assertEqual(response.status_code, 404)

    def test_an_unknown_lesson_is_a_404(self):
        response = TenantAPIClient(self.admin).get(
            reverse("attendance-lesson", kwargs={"slot_id": 999999, "on_date": str(TODAY)}),
        )
        self.assertEqual(response.status_code, 404)

    def test_a_branch_bound_administrator_reaches_only_their_branchs_lessons(self):
        self.assertEqual(self.lesson(self.lekki_admin, self.sss2_english).status_code, 404)
        self.assertEqual(self.lesson(self.ikeja_admin, self.a_english).status_code, 404)
        self.assertEqual(self.lesson(self.lekki_admin, self.a_english).status_code, 200)
        self.assertEqual(self.lesson(self.ikeja_admin, self.sss2_english).status_code, 200)
        self.assertEqual(
            self.mark_lesson(self.lekki_admin, self.sss2_english, self.ngozi, "P").status_code, 404,
        )
        self.assertEqual(
            self.mark_lesson(self.ikeja_admin, self.sss2_english, self.ngozi, "P").status_code, 200,
        )

    def test_at_a_school_with_one_branch_the_class_shares_the_only_branch(self):
        response = self.mark_lesson(self.solo_teacher, self.solo_slot, self.solo_pupil, "A")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(self.data(response)["school_class"]["branch"])
        self.assertEqual(LessonRegister.objects.get().branch, self.solo_branch)

    def test_a_shared_class_at_a_school_with_several_branches_has_nowhere_to_register(self):
        slot = make_slot(
            self.tenant, self.shared, 1, self.period1, self.english, self.eze,
        )
        publish(self.tenant, self.shared)
        response = self.lesson(self.admin, slot)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "CLASS_HAS_NO_BRANCH")


class MarkRulesTests(LessonFixture):
    def test_a_lesson_register_has_no_excused(self):
        response = self.mark_lesson(self.eze, self.a_english, self.tunde, "E")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("mark", response.data["error"]["detail"])
        self.assertFalse(LessonMark.objects.exists())

    def test_a_late_mark_needs_the_time(self):
        response = self.mark_lesson(self.eze, self.a_english, self.tunde, "L")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "ARRIVAL_TIME_REQUIRED")
        ok = self.mark_lesson(self.eze, self.a_english, self.tunde, "L", arrived_at="08:07")
        self.assertEqual(self.lesson_rows(ok)[self.tunde.pk]["arrived_at"], "08:07")

    def test_a_mark_that_is_not_late_carries_no_arrival_time(self):
        self.mark_lesson(self.eze, self.a_english, self.tunde, "L", arrived_at="08:07")
        response = self.mark_lesson(self.eze, self.a_english, self.tunde, "P", arrived_at="08:07")
        self.assertIsNone(self.lesson_rows(response)[self.tunde.pk]["arrived_at"])

    def test_a_reason_is_not_something_a_lesson_mark_holds(self):
        response = self.mark_lesson(
            self.eze, self.a_english, self.tunde, "A", reason_code=1,
        )
        self.assertEqual(response.status_code, 200)
        row = self.lesson_rows(response)[self.tunde.pk]
        self.assertNotIn("reason_code", row)
        self.assertNotIn("reason_label", row)
        self.assertNotIn("notice_state", row)

    def test_a_note_not_sent_is_kept_and_a_blank_one_clears_it(self):
        self.mark_lesson(self.eze, self.a_english, self.tunde, "A", note="Sent to the nurse")
        kept = self.mark_lesson(self.eze, self.a_english, self.tunde, "L", arrived_at="08:20")
        self.assertEqual(self.lesson_rows(kept)[self.tunde.pk]["note"], "Sent to the nurse")
        cleared = self.mark_lesson(
            self.eze, self.a_english, self.tunde, "L", arrived_at="08:20", note="",
        )
        self.assertEqual(self.lesson_rows(cleared)[self.tunde.pk]["note"], "")

    def test_a_suspended_student_is_locked_and_counted_nowhere(self):
        response = self.mark_lesson(self.eze, self.a_english, self.amina, "P")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "STUDENT_SUSPENDED")
        body = self.data(self.lesson(self.eze, self.a_english))
        rows = {row["student_id"]: row for row in body["rows"]}
        self.assertTrue(rows[self.amina.pk]["locked"])
        self.assertIsNone(rows[self.amina.pk]["mark"])
        self.assertEqual(body["counts"]["total"], 3)
        self.assertEqual(body["counts"]["locked"], 1)

    def test_fill_present_marks_only_the_unmarked_unlocked_rows_and_submits_nothing(self):
        self.mark_lesson(self.eze, self.a_english, self.tunde, "A")
        response = self.fill_lesson(self.eze, self.a_english)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["filled"], 2)
        rows = self.lesson_rows(response)
        self.assertEqual(rows[self.tunde.pk]["mark"], "A")
        self.assertEqual(rows[self.kelechi.pk]["mark"], "P")
        self.assertEqual(rows[self.chiamaka.pk]["mark"], "P")
        self.assertIsNone(rows[self.amina.pk]["mark"])
        self.assertEqual(self.data(response)["status"], "READY")
        self.assertIsNone(LessonRegister.objects.get().submitted_at)
        self.assertEqual(self.data(self.fill_lesson(self.eze, self.a_english))["filled"], 0)

    def test_a_register_cannot_be_submitted_with_an_unmarked_row(self):
        self.mark_lesson(self.eze, self.a_english, self.tunde, "P")
        response = self.submit_lesson(self.eze, self.a_english)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "UNMARKED_ROWS")
        self.assertIn("unmarked", response.data["message"])
        self.assertIsNone(LessonRegister.objects.get().submitted_at)

    def test_submitting_records_who_and_when_and_cannot_be_done_twice(self):
        self.fill_lesson(self.eze, self.a_english)
        response = self.submit_lesson(self.eze, self.a_english)
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertEqual(body["status"], "SUBMITTED")
        self.assertEqual(body["taken_by"]["submitted_by"], "Chidi Eze")
        self.assertIsNotNone(body["taken_by"]["submitted_at"])
        again = self.submit_lesson(self.eze, self.a_english)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.code(again), "ALREADY_SUBMITTED")

    def test_a_lesson_with_nobody_on_the_roll_has_no_register_to_submit(self):
        # Nobody is on the roll yet: the pupils are placed in January.
        ClassEnrolment.all_objects.filter(school_class=self.jss1b).update(
            effective_date=dt.date(2026, 1, 5),
        )
        body = self.data(self.lesson(self.bello, self.b_maths))
        self.assertTrue(body["roster_empty"])
        response = self.submit_lesson(self.bello, self.b_maths)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "EMPTY_ROSTER")

    def test_the_status_follows_the_register(self):
        def status():
            return self.data(self.lesson(self.eze, self.a_english))["status"]

        self.assertEqual(status(), "NOT_STARTED")
        self.mark_lesson(self.eze, self.a_english, self.tunde, "P")
        self.assertEqual(status(), "IN_PROGRESS")
        self.fill_lesson(self.eze, self.a_english)
        self.assertEqual(status(), "READY")
        self.submit_lesson(self.eze, self.a_english)
        self.assertEqual(status(), "SUBMITTED")

    def test_marking_the_same_thing_again_writes_nothing(self):
        self.take_lesson(self.eze, self.a_english)
        self.mark_lesson(self.eze, self.a_english, self.tunde, "P")
        self.assertFalse(LessonMarkChange.objects.exists())

    def test_the_morning_register_is_untouched_by_a_lesson_mark(self):
        self.take(self.eze, self.jss1a, TODAY, tunde="A")
        before = list(StudentMark.objects.order_by("pk").values_list("student_id", "mark"))
        self.take_lesson(self.eze, self.a_english, tunde="P", chiamaka="A")
        self.assertEqual(
            list(StudentMark.objects.order_by("pk").values_list("student_id", "mark")), before,
        )


class DaysTests(LessonFixture):
    def test_a_day_that_has_not_come_cannot_be_marked(self):
        response = self.mark_lesson(
            self.eze, self.a_english, self.tunde, "P", on_date=dt.date(2025, 10, 20),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "DATE_IN_FUTURE")

    def test_a_closed_day_has_no_lessons_to_mark(self):
        body = self.data(self.lesson(self.eze, self.a_thursday, FOUNDERS_DAY))
        self.assertEqual(body["status"], "CLOSED_DAY")
        self.assertEqual(body["closed"]["reason"], "Founders' Day")
        self.assertEqual(body["rows"], [])
        response = self.mark_lesson(self.eze, self.a_thursday, self.tunde, "P", FOUNDERS_DAY)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "DAY_CLOSED_NO_REGISTER")
        self.assertEqual(self.fill_lesson(self.eze, self.a_thursday, FOUNDERS_DAY).status_code, 422)
        self.assertEqual(self.submit_lesson(self.eze, self.a_thursday, FOUNDERS_DAY).status_code, 422)

    def test_a_malformed_date_is_a_400(self):
        response = TenantAPIClient(self.admin).get(
            self.lesson_url("attendance-lesson", self.a_english, "13-10-2025"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "INVALID_DATE")


class EditWindowTests(LessonFixture):
    def test_the_person_who_submitted_changes_a_row_the_same_day_and_it_is_kept(self):
        self.take_lesson(self.eze, self.a_english)
        response = self.mark_lesson(self.eze, self.a_english, self.tunde, "A", note="Left early")
        self.assertEqual(response.status_code, 200, response.data)
        [entry] = self.data(response)["history"]
        self.assertEqual(
            (entry["student_name"], entry["from_mark"], entry["to_mark"], entry["note"]),
            ("Tunde Bello", "P", "A", "Left early"),
        )
        self.assertEqual(entry["changed_by_name"], "Chidi Eze")
        self.assertEqual(LessonMarkChange.objects.count(), 1)

    def test_somebody_else_cannot_change_a_submitted_register_unless_they_hold_mark_any(self):
        self.take_lesson(self.admin, self.a_english)
        refused = self.mark_lesson(self.eze, self.a_english, self.tunde, "A")
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(self.code(refused), "ONLY_SUBMITTER_MAY_CHANGE")
        self.assertEqual(
            self.mark_lesson(self.lekki_admin, self.a_english, self.tunde, "A").status_code, 200,
        )

    def test_filling_in_after_submission_is_a_change_and_is_kept(self):
        self.take_lesson(self.eze, self.a_english)
        LessonMark.objects.filter(student=self.tunde).delete()
        self.fill_lesson(self.eze, self.a_english)
        [entry] = self.data(self.lesson(self.eze, self.a_english))["history"]
        self.assertEqual((entry["from_mark"], entry["to_mark"]), (None, "P"))

    def test_once_the_day_has_closed_a_submitted_lesson_mark_is_frozen(self):
        self.take_lesson(self.eze, self.a_english, LAST_MONDAY, tunde="A")
        for user in (self.eze, self.admin):
            response = self.mark_lesson(user, self.a_english, self.tunde, "P", LAST_MONDAY)
            self.assertEqual(response.status_code, 422, response.data)
            self.assertEqual(self.code(response), "LESSON_DAY_CLOSED")
            self.assertIn("cannot be corrected", response.data["message"])
        mark = LessonMark.objects.get(student=self.tunde)
        self.assertEqual(mark.mark, "A")
        self.assertFalse(LessonMarkChange.objects.exists())

    def test_there_is_no_correction_for_a_lesson(self):
        self.take_lesson(self.eze, self.a_english, LAST_MONDAY)
        response = self.correct(
            self.eze, self.jss1a, self.tunde, "A", on_date=LAST_MONDAY, reason_code=self.reason_id(),
        )
        self.assertNotEqual(response.status_code, 201)
        self.assertFalse(CorrectionRequest.objects.exists())

    def test_the_edit_window_is_the_schools_setting(self):
        set_setting(CFG_EDIT_WINDOW_DAYS, 7, self.tenant, self.admin)
        self.take_lesson(self.eze, self.a_english, LAST_MONDAY, tunde="A")
        response = self.mark_lesson(self.eze, self.a_english, self.tunde, "P", LAST_MONDAY)
        self.assertEqual(response.status_code, 200, response.data)
        set_setting(CFG_EDIT_WINDOW_DAYS, 6, self.tenant, self.admin)
        closed = self.mark_lesson(self.eze, self.a_english, self.tunde, "A", LAST_MONDAY)
        self.assertEqual(self.code(closed), "LESSON_DAY_CLOSED")

    def test_a_register_never_submitted_can_still_be_taken_late(self):
        self.mark_lesson(self.eze, self.a_english, self.tunde, "A", LAST_MONDAY)
        response = self.take_lesson(self.eze, self.a_english, LAST_MONDAY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["status"], "SUBMITTED")


class NothingIsSentTests(LessonFixture):
    def test_a_lesson_register_sends_nothing_to_anyone(self):
        notifications = Notification.all_objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            self.take_lesson(self.eze, self.a_english, tunde="A", chiamaka="L")
            self.mark_lesson(self.eze, self.a_english, self.tunde, "P")
            self.mark_lesson(self.eze, self.a_english, self.tunde, "A")
        self.sent.assert_not_called()
        self.assertEqual(Notification.all_objects.count(), notifications)
        self.assertEqual(mail.outbox, [])


class DatabaseRulesTests(LessonFixture):
    """The rules the tables keep even for a write that bypasses the service."""

    def setUp(self):
        super().setUp()
        self.mark_lesson(self.eze, self.a_english, self.tunde, "P")
        self.register = LessonRegister.objects.get()

    def make(self, student, **fields):
        with transaction.atomic():
            return LessonMark.objects.create(
                tenant=self.tenant, register=self.register, student=student, **fields,
            )

    def test_a_lesson_mark_is_never_excused(self):
        with self.assertRaises(IntegrityError):
            self.make(self.kelechi, mark="E")

    def test_only_a_late_mark_carries_an_arrival_time(self):
        with self.assertRaises(IntegrityError):
            self.make(self.kelechi, mark="P", arrived_at=dt.time(8, 10))
        self.assertEqual(self.make(self.kelechi, mark="L", arrived_at=dt.time(8, 10)).mark, "L")

    def test_a_student_has_one_mark_in_a_register(self):
        with self.assertRaises(IntegrityError):
            self.make(self.tunde, mark="A")

    def test_a_change_to_a_lesson_mark_is_history_and_cannot_be_edited_or_deleted(self):
        self.fill_lesson(self.admin, self.a_english)
        self.submit_lesson(self.admin, self.a_english)
        self.mark_lesson(self.admin, self.a_english, self.tunde, "A")
        change = LessonMarkChange.objects.get()
        change.note = "Edited"
        with self.assertRaises(RuntimeError):
            change.save()
        with self.assertRaises(RuntimeError):
            change.delete()
