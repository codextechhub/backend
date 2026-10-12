"""Taking a register: who may, what a row must hold, and how it is submitted.

Mr Eze leads JSS1 A and holds only ``mark``. Ms Bello holds only ``mark`` too
and leads JSS1 B. The registrar holds ``mark_any``.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_staff.models import LeaveRequest

from ..constants import Mark, NoticeState
from ..models import ClassRegister, ReasonCode, StudentMark
from ..services.reasons import ensure_default_reason_codes
from .base import FOUNDERS_DAY, LAST_FRIDAY, TODAY, AttendanceFixture


class WhoMayTakeTheRegisterTests(AttendanceFixture):
    def test_a_class_teacher_with_mark_takes_their_own_class(self):
        response = self.mark(self.eze, self.jss1a, self.tunde, "P")
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_teacher_with_mark_cannot_take_a_class_they_do_not_lead(self):
        response = self.mark(self.bello, self.jss1a, self.tunde, "P")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.code(response), "NOT_YOUR_CLASS")
        self.assertIn("class you lead", response.data["message"])
        self.assertFalse(ClassRegister.objects.filter(school_class=self.jss1a).exists())

    def test_the_same_refusal_covers_filling_and_submitting(self):
        for response in (
            self.fill(self.bello, self.jss1a), self.submit(self.bello, self.jss1a),
        ):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.code(response), "NOT_YOUR_CLASS")

    def test_a_holder_of_mark_any_takes_any_class_in_their_branches(self):
        self.assertEqual(self.mark(self.admin, self.jss1a, self.tunde, "P").status_code, 200)
        self.assertEqual(self.mark(self.lekki_admin, self.jss1a, self.kelechi, "P").status_code, 200)

    def test_a_branch_admin_cannot_take_another_branchs_class(self):
        self.assertEqual(self.mark(self.lekki_admin, self.sss2, self.tunde, "P").status_code, 404)

    def test_a_caller_with_no_attendance_key_is_refused(self):
        self.assertEqual(self.mark(self.nobody, self.jss1a, self.tunde, "P").status_code, 403)

    def test_a_class_with_no_teacher_can_be_taken_only_with_mark_any(self):
        self.assertEqual(self.mark(self.eze, self.pry4a, self.tunde, "P").status_code, 403)

    def test_submitting_for_a_teacher_records_it_was_on_their_behalf(self):
        self.fill(self.admin, self.jss1a)
        self.submit(self.admin, self.jss1a)
        taken = self.data(self.read(self.admin, self.jss1a))["taken_by"]
        self.assertEqual(taken["submitted_by"], "Ngozi Adeyemi")
        self.assertEqual(taken["on_behalf_of"], "Chidi Eze")
        self.assertIsNone(taken["on_leave"])

    def test_submitting_your_own_register_records_no_behalf(self):
        self.fill(self.eze, self.jss1a)
        self.submit(self.eze, self.jss1a)
        taken = self.data(self.read(self.eze, self.jss1a))["taken_by"]
        self.assertEqual(taken["submitted_by"], "Chidi Eze")
        self.assertIsNone(taken["on_behalf_of"])

    def test_a_class_teacher_on_approved_leave_is_named_with_the_leave(self):
        LeaveRequest.all_objects.create(
            tenant=self.tenant, staff=self.eze_staff, leave_type="SICK", days=3,
            start_date=dt.date(2025, 10, 13), end_date=dt.date(2025, 10, 15),
            status="APPROVED",
        )
        self.fill(self.admin, self.jss1a)
        self.submit(self.admin, self.jss1a)
        leave = self.data(self.read(self.admin, self.jss1a))["taken_by"]["on_leave"]
        self.assertEqual(leave["leave_type_label"], "Sick")
        self.assertEqual(str(leave["ends_on"]), "2025-10-15")

    def test_pending_leave_is_not_mentioned(self):
        LeaveRequest.all_objects.create(
            tenant=self.tenant, staff=self.eze_staff, leave_type="SICK", days=1,
            start_date=TODAY, end_date=TODAY, status="PENDING",
        )
        self.fill(self.admin, self.jss1a)
        self.submit(self.admin, self.jss1a)
        self.assertIsNone(self.data(self.read(self.admin, self.jss1a))["taken_by"]["on_leave"])


class TheDayTests(AttendanceFixture):
    def test_a_future_date_is_refused(self):
        response = self.mark(self.eze, self.jss1a, self.tunde, "P", on_date=dt.date(2025, 10, 14))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "DATE_IN_FUTURE")

    def test_a_closed_day_has_no_register_to_take(self):
        response = self.mark(self.eze, self.jss1a, self.tunde, "P", on_date=FOUNDERS_DAY)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "DAY_CLOSED_NO_REGISTER")
        self.assertIn("Founders' Day", response.data["message"])

    def test_a_weekend_has_no_register_to_take(self):
        response = self.mark(self.admin, self.jss1a, self.tunde, "P", on_date=dt.date(2025, 10, 11))
        self.assertEqual(self.code(response), "DAY_CLOSED_NO_REGISTER")
        self.assertIn("not a teaching day", response.data["message"])

    def test_a_day_nobody_took_can_still_be_taken_late(self):
        response = self.mark(self.eze, self.jss1a, self.tunde, "P", on_date=LAST_FRIDAY)
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_register_belongs_to_the_classs_branch_and_term(self):
        self.mark(self.eze, self.jss1a, self.tunde, "P")
        register = ClassRegister.objects.get(school_class=self.jss1a, date=TODAY)
        self.assertEqual(register.branch, self.lekki)
        self.assertEqual(register.term, self.first_term)
        self.assertEqual(register.session, self.year)


class TheRosterTests(AttendanceFixture):
    def test_a_suspended_student_cannot_be_marked(self):
        response = self.mark(self.eze, self.jss1a, self.amina, "P")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "STUDENT_SUSPENDED")

    def test_a_student_placed_after_the_date_is_not_on_the_register(self):
        response = self.mark(self.eze, self.jss1a, self.obinna, "P")
        self.assertEqual(response.status_code, 404)

    def test_another_schools_student_is_not_on_the_register(self):
        response = self.mark(self.admin, self.jss1a, self.solo_pupil, "P")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(StudentMark.objects.filter(student=self.solo_pupil).exists())

    def test_a_student_of_another_class_is_not_on_this_register(self):
        ikeja_pupil = self.make_student(
            "Ngozi", "Ibe", self.sss2, "BFS/900", branch=self.ikeja,
        )
        self.assertEqual(self.mark(self.admin, self.jss1a, ikeja_pupil, "P").status_code, 404)

    def test_a_pupil_back_from_suspension_is_markable_on_the_day_they_return(self):
        from schools.vs_students.models import StudentStatusLog

        StudentStatusLog.all_objects.filter(student=self.amina).delete()
        self.make_suspension(self.amina, dt.date(2025, 10, 6), TODAY)
        self.assertEqual(self.mark(self.eze, self.jss1a, self.amina, "P").status_code, 200)


class TheMarkTests(AttendanceFixture):
    def test_a_mark_that_is_not_one_of_the_four_is_refused(self):
        response = self.mark(self.eze, self.jss1a, self.tunde, "X")
        self.assertEqual(response.status_code, 400)

    def test_a_late_mark_needs_an_arrival_time(self):
        response = self.mark(self.eze, self.jss1a, self.tunde, "L")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "ARRIVAL_TIME_REQUIRED")
        self.assertEqual(response.data["error"]["detail"]["field"], "arrived_at")

    def test_a_late_mark_keeps_its_arrival_time_and_a_note(self):
        self.mark(self.eze, self.jss1a, self.tunde, "L", arrived_at="08:20", note="Bus broke down")
        row = self.rows(self.read(self.eze, self.jss1a))[self.tunde.pk]
        self.assertEqual((row["mark"], row["arrived_at"], row["note"]), ("L", "08:20", "Bus broke down"))

    def test_an_excused_mark_needs_a_reason(self):
        response = self.mark(self.eze, self.jss1a, self.tunde, "E")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "REASON_CODE_REQUIRED")

    def test_an_excused_mark_needs_a_live_reason(self):
        ensure_default_reason_codes(self.tenant)
        sick = ReasonCode.objects.get(tenant=self.tenant, name="Sick")
        ok = self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=sick.pk)
        self.assertEqual(ok.status_code, 200, ok.data)
        row = self.rows(self.read(self.eze, self.jss1a))[self.tunde.pk]
        self.assertEqual((row["mark"], row["reason_label"]), ("E", "Sick"))

    def test_a_retired_reason_is_not_offered_on_a_new_mark(self):
        ensure_default_reason_codes(self.tenant)
        travel = ReasonCode.objects.get(tenant=self.tenant, name="Travel")
        ReasonCode.objects.filter(pk=travel.pk).update(retired_at=dt.datetime(2025, 9, 1, tzinfo=dt.timezone.utc))
        response = self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=travel.pk)
        self.assertEqual(self.code(response), "REASON_CODE_RETIRED")

    def test_a_retired_reason_stays_on_a_mark_that_already_carried_it(self):
        ensure_default_reason_codes(self.tenant)
        travel = ReasonCode.objects.get(tenant=self.tenant, name="Travel")
        self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=travel.pk)
        ReasonCode.objects.filter(pk=travel.pk).update(retired_at=dt.datetime(2025, 9, 1, tzinfo=dt.timezone.utc))
        again = self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=travel.pk)
        self.assertEqual(again.status_code, 200, again.data)

    def test_another_schools_reason_is_not_a_reason_here(self):
        ensure_default_reason_codes(self.solo)
        theirs = ReasonCode.objects.get(tenant=self.solo, name="Sick")
        response = self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=theirs.pk)
        self.assertEqual(self.code(response), "REASON_CODE_REQUIRED")

    def test_other_needs_a_note(self):
        ensure_default_reason_codes(self.tenant)
        other = ReasonCode.objects.get(tenant=self.tenant, name="Other")
        response = self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=other.pk)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "NOTE_REQUIRED")
        self.assertEqual(response.data["error"]["detail"]["field"], "note")

        blank = self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=other.pk, note="   ")
        self.assertEqual(self.code(blank), "NOTE_REQUIRED")

        ok = self.mark(
            self.eze, self.jss1a, self.tunde, "E", reason_code=other.pk, note="Court date",
        )
        self.assertEqual(ok.status_code, 200, ok.data)

    def test_a_mark_that_is_not_late_carries_no_arrival_and_not_excused_no_reason(self):
        ensure_default_reason_codes(self.tenant)
        sick = ReasonCode.objects.get(tenant=self.tenant, name="Sick")
        self.mark(self.eze, self.jss1a, self.tunde, "L", arrived_at="08:30")
        self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=sick.pk, arrived_at="08:30")
        self.mark(self.eze, self.jss1a, self.tunde, "P", reason_code=sick.pk, arrived_at="08:30")
        stored = StudentMark.objects.get(student=self.tunde)
        self.assertEqual(stored.mark, Mark.PRESENT)
        self.assertIsNone(stored.arrived_at)
        self.assertIsNone(stored.reason_code_id)

    def test_a_note_that_is_not_sent_is_kept(self):
        self.mark(self.eze, self.jss1a, self.tunde, "A", note="Mother phoned")
        self.mark(self.eze, self.jss1a, self.tunde, "P")
        self.assertEqual(StudentMark.objects.get(student=self.tunde).note, "Mother phoned")

    def test_marking_the_same_row_again_writes_nothing(self):
        self.mark(self.eze, self.jss1a, self.tunde, "A")
        before = StudentMark.objects.get(student=self.tunde)
        self.mark(self.eze, self.jss1a, self.tunde, "A")
        after = StudentMark.objects.get(student=self.tunde)
        self.assertEqual(before.updated_at, after.updated_at)

    def test_a_mark_response_is_the_whole_register_as_it_now_stands(self):
        body = self.data(self.mark(self.eze, self.jss1a, self.tunde, "A"))
        self.assertEqual(body["counts"]["absent"], 1)
        self.assertEqual(body["counts"]["unmarked"], 2)
        self.assertEqual(body["submit_preview"]["no_email"], 1)

    def test_default_reason_codes_are_written_once_for_a_school(self):
        self.assertTrue(ensure_default_reason_codes(self.tenant))
        self.assertFalse(ensure_default_reason_codes(self.tenant))
        self.assertEqual(
            list(ReasonCode.objects.filter(tenant=self.tenant).values_list("name", flat=True)),
            ["Sick", "Medical appointment", "Family matter", "Religious observance",
             "Travel", "Representing the school", "Other"],
        )
        self.assertEqual(ReasonCode.objects.filter(tenant=self.solo).count(), 0)


class FillPresentTests(AttendanceFixture):
    def test_it_fills_only_the_unmarked_and_unlocked_rows(self):
        self.mark(self.eze, self.jss1a, self.tunde, "A")
        response = self.fill(self.eze, self.jss1a)
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertEqual(body["filled"], 2)
        rows = {row["name"]: row for row in body["rows"]}
        self.assertEqual(rows["Tunde Bello"]["mark"], "A")
        self.assertEqual(rows["Kelechi Eze"]["mark"], "P")
        self.assertEqual(rows["Chiamaka Nwosu"]["mark"], "P")
        self.assertIsNone(rows["Amina Yusuf"]["mark"])
        self.assertFalse(StudentMark.objects.filter(student=self.amina).exists())

    def test_it_submits_nothing(self):
        self.fill(self.eze, self.jss1a)
        register = ClassRegister.objects.get(school_class=self.jss1a, date=TODAY)
        self.assertIsNone(register.submitted_at)
        self.assertEqual(self.data(self.read(self.eze, self.jss1a))["status"], "READY")

    def test_with_nothing_left_to_fill_it_says_so_and_creates_no_register(self):
        response = self.fill(self.eze, self.pry4a)
        # Not Eze's class.
        self.assertEqual(response.status_code, 403)
        empty = self.fill(self.admin, self.pry4a)
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(self.data(empty)["filled"], 0)
        self.assertFalse(ClassRegister.objects.filter(school_class=self.pry4a).exists())

    def test_filling_twice_fills_nothing_the_second_time(self):
        self.fill(self.eze, self.jss1a)
        self.assertEqual(self.data(self.fill(self.eze, self.jss1a))["filled"], 0)


class SubmitTests(AttendanceFixture):
    def test_a_register_cannot_be_submitted_with_an_unmarked_row(self):
        self.mark(self.eze, self.jss1a, self.tunde, "P")
        response = self.submit(self.eze, self.jss1a)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "UNMARKED_ROWS")
        self.assertEqual(
            response.data["message"],
            "Still 2 unmarked. A register cannot be submitted with an unmarked row.",
        )
        self.assertEqual(response.data["error"]["detail"]["unmarked"], 2)

    def test_a_register_nobody_started_cannot_be_submitted(self):
        response = self.submit(self.eze, self.jss1a)
        self.assertEqual(self.code(response), "UNMARKED_ROWS")
        self.assertFalse(ClassRegister.objects.filter(school_class=self.jss1a).exists())

    def test_the_locked_row_does_not_stand_in_the_way(self):
        self.fill(self.eze, self.jss1a)
        response = self.submit(self.eze, self.jss1a)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["status"], "SUBMITTED")

    def test_submitting_records_who_and_when(self):
        self.fill(self.eze, self.jss1a)
        self.submit(self.eze, self.jss1a)
        register = ClassRegister.objects.get(school_class=self.jss1a, date=TODAY)
        self.assertEqual(register.submitted_by, self.eze)
        self.assertIsNotNone(register.submitted_at)

    def test_a_register_cannot_be_submitted_twice(self):
        self.fill(self.eze, self.jss1a)
        self.submit(self.eze, self.jss1a)
        again = self.submit(self.eze, self.jss1a)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.code(again), "ALREADY_SUBMITTED")

    def test_a_class_with_nobody_in_it_has_no_register_to_submit(self):
        response = self.submit(self.admin, self.pry4a)
        self.assertEqual(self.code(response), "EMPTY_ROSTER")

    def test_a_closed_day_cannot_be_submitted(self):
        response = self.submit(self.admin, self.jss1a, FOUNDERS_DAY)
        self.assertEqual(self.code(response), "DAY_CLOSED_NO_REGISTER")

    def test_submitting_is_audited(self):
        from vs_audit.models import AuditEvent

        self.fill(self.eze, self.jss1a)
        self.submit(self.eze, self.jss1a)
        event = AuditEvent.objects.get(
            tenant=self.tenant, entity_type="ClassRegister",
            summary__contains="was submitted",
        )
        self.assertIn("3 present, 0 late, 0 absent, 0 excused", event.summary)
        self.assertEqual(event.actor_user_id, self.eze.pk)
        self.assertEqual(event.module_key, "ATTENDANCE")

    def test_the_submit_preview_is_null_once_submitted(self):
        self.fill(self.eze, self.jss1a)
        self.assertIsNotNone(self.data(self.read(self.eze, self.jss1a))["submit_preview"])
        self.submit(self.eze, self.jss1a)
        self.assertIsNone(self.data(self.read(self.eze, self.jss1a))["submit_preview"])

    def test_a_school_with_one_branch_takes_and_submits_a_register(self):
        from core.test_utils import TenantAPIClient

        client = TenantAPIClient(self.solo_teacher)
        base = f"/v1/attendance/registers/{self.solo_class.pk}/{TODAY}"
        self.assertEqual(client.post(f"{base}/fill-present/").status_code, 200)
        done = client.post(f"{base}/submit/")
        self.assertEqual(done.status_code, 200, done.data)
        register = ClassRegister.objects.get(school_class=self.solo_class, date=TODAY)
        self.assertEqual(register.branch, self.solo_branch)
        self.assertEqual(register.submitted_by, self.solo_teacher)
        self.assertIsNone(register.on_behalf_of)
        self.assertEqual(NoticeState.NONE, StudentMark.objects.get().notice_state)
