"""Correcting a mark after its day has closed.

Last Friday (10 October 2025) is a closed day on Monday 13 October: the edit
window is the same day only, so Eze's submitted register for it can no longer be
changed in place. Chiamaka's mother has an address, Tunde's has none and
Kelechi's has an account, which is what decides whether a corrected absence
writes to anybody.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.test_utils import TenantAPIClient
from vs_audit.models import AuditEvent
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from ..constants import (
    EVENT_ABSENCE_NOTICE,
    EVENT_CORRECTION_NOTICE,
    ChangeVia,
    CorrectionStatus,
    NoticeState,
)
from ..models import CorrectionRequest, MarkChange, ReasonCode, StudentMark
from .base import LAST_FRIDAY, TODAY, AttendanceFixture

SEND = "schools.vs_attendance.services.notices.send_notification"


class _Corrections(AttendanceFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def friday(self, **marks):
        """Eze takes and submits JSS1 A's register for last Friday."""
        response = self.take(self.eze, self.jss1a, LAST_FRIDAY, **marks)
        self.assertEqual(response.status_code, 200, response.data)

    def ask(self, student, to_mark, user=None, **body):
        body.setdefault("reason_code", self.reason_id())
        response = self.correct(user or self.eze, self.jss1a, student, to_mark, **body)
        self.assertEqual(response.status_code, 201, response.data)
        return self.data(response)

    def events(self):
        return [call.kwargs["event_key"] for call in self.send.call_args_list]

    def mark_of(self, student):
        return StudentMark.objects.get(student=student, register__date=LAST_FRIDAY)


class RequestingTests(_Corrections):
    def test_a_correction_is_refused_while_the_day_is_still_open(self):
        self.take(self.eze, self.jss1a, TODAY, tunde="A")
        response = self.correct(
            self.eze, self.jss1a, self.tunde, "P", on_date=TODAY, reason_code=self.reason_id(),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "CORRECTION_NOT_NEEDED")
        self.assertEqual(
            response.data["message"],
            "This day is still open. Change the mark on the register instead.",
        )
        self.assertFalse(CorrectionRequest.objects.exists())

    def test_a_correction_is_refused_for_a_register_that_was_never_submitted(self):
        self.mark(self.eze, self.jss1a, self.tunde, "A", on_date=LAST_FRIDAY)
        response = self.correct(
            self.eze, self.jss1a, self.tunde, "P", reason_code=self.reason_id(),
        )
        self.assertEqual(self.code(response), "CORRECTION_NOT_NEEDED")
        nothing = self.correct(
            self.eze, self.jss1a, self.tunde, "P", on_date=dt.date(2025, 10, 8),
            reason_code=self.reason_id(),
        )
        self.assertEqual(self.code(nothing), "CORRECTION_NOT_NEEDED")

    def test_the_class_teacher_asks_for_a_correction_and_it_waits(self):
        self.friday(tunde="A")
        row = self.ask(self.tunde, "P", note="Was at the clinic")
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual((row["from_mark"], row["to_mark"]), ("A", "P"))
        self.assertEqual(row["student"]["name"], "Tunde Bello")
        self.assertEqual(row["student"]["student_number"], "BFS/002")
        self.assertEqual(row["class_name"], "JSS1 A")
        self.assertEqual(row["date"], "2025-10-10")
        self.assertEqual(row["reason_label"], "Sick")
        self.assertEqual(row["note"], "Was at the clinic")
        self.assertEqual(row["requested_by_name"], "Chidi Eze")
        self.assertEqual(row["decided_by_name"], "")
        self.assertTrue(row["is_mine"])
        self.assertEqual(row["branch"]["name"], "Lekki Branch")
        self.assertEqual(self.mark_of(self.tunde).mark, "A")

    def test_asking_is_audited_under_the_attendance_module(self):
        self.friday(tunde="A")
        self.ask(self.tunde, "P")
        event = AuditEvent.objects.get(entity_type="CorrectionRequest")
        self.assertEqual(event.module_key, "ATTENDANCE")
        self.assertIn("Tunde Bello", event.summary)

    def test_it_is_refused_for_the_mark_the_student_already_has(self):
        self.friday(tunde="A")
        response = self.correct(
            self.eze, self.jss1a, self.tunde, "A", reason_code=self.reason_id(),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "NO_CHANGE")

    def test_a_second_request_for_the_same_mark_is_refused_whatever_it_asks_for(self):
        self.friday(tunde="A")
        self.ask(self.tunde, "P")
        again = self.correct(
            self.admin, self.jss1a, self.tunde, "E", reason_code=self.reason_id(),
        )
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.code(again), "CORRECTION_ALREADY_PENDING")
        self.assertEqual(CorrectionRequest.objects.count(), 1)

    def test_a_decided_request_does_not_stop_a_later_one(self):
        self.friday(tunde="A")
        first = self.ask(self.tunde, "P")
        self.decide(self.admin, first["id"], "reject")
        self.ask(self.tunde, "P")
        self.assertEqual(CorrectionRequest.objects.count(), 2)

    def test_every_correction_needs_a_live_reason(self):
        self.friday(tunde="A")
        missing = self.correct(self.eze, self.jss1a, self.tunde, "P")
        self.assertEqual(self.code(missing), "REASON_CODE_REQUIRED")
        unknown = self.correct(self.eze, self.jss1a, self.tunde, "P", reason_code=99999)
        self.assertEqual(self.code(unknown), "REASON_CODE_REQUIRED")
        ReasonCode.objects.filter(pk=self.reason_id()).update(retired_at=LAST_FRIDAY)
        retired = self.correct(
            self.eze, self.jss1a, self.tunde, "P", reason_code=self.reason_id(),
        )
        self.assertEqual(self.code(retired), "REASON_CODE_RETIRED")

    def test_a_reason_that_asks_for_a_note_needs_one(self):
        self.friday(tunde="A")
        other = self.reason_id("Other")
        refused = self.correct(
            self.eze, self.jss1a, self.tunde, "P", reason_code=other, note="  ",
        )
        self.assertEqual(self.code(refused), "NOTE_REQUIRED")
        self.assertEqual(refused.data["error"]["detail"]["field"], "note")
        accepted = self.correct(
            self.eze, self.jss1a, self.tunde, "P", reason_code=other, note="Marked in error",
        )
        self.assertEqual(accepted.status_code, 201)

    def test_late_needs_a_time_and_keeps_it(self):
        self.friday(tunde="A")
        refused = self.correct(
            self.eze, self.jss1a, self.tunde, "L", reason_code=self.reason_id(),
        )
        self.assertEqual(self.code(refused), "ARRIVAL_TIME_REQUIRED")
        row = self.ask(self.tunde, "L", arrived_at="08:45")
        self.assertEqual(row["arrived_at"], "08:45")

    def test_an_unknown_mark_is_refused(self):
        self.friday(tunde="A")
        response = self.correct(
            self.eze, self.jss1a, self.tunde, "Z", reason_code=self.reason_id(),
        )
        self.assertEqual(response.status_code, 400)

    def test_a_student_with_no_mark_on_the_register_cannot_be_corrected(self):
        self.friday()
        late_joiner = self.make_student("Obinna", "Okafor", self.jss1b, "BFS/910")
        response = self.correct(
            self.eze, self.jss1a, late_joiner, "A", reason_code=self.reason_id(),
        )
        self.assertEqual(response.status_code, 404)
        suspended = self.correct(
            self.eze, self.jss1a, self.amina, "A", reason_code=self.reason_id(),
        )
        self.assertEqual(suspended.status_code, 404)


class WhoMayAskTests(_Corrections):
    def test_a_teacher_cannot_ask_for_a_class_they_do_not_lead(self):
        self.friday(tunde="A")
        response = self.correct(
            self.bello, self.jss1a, self.tunde, "P", reason_code=self.reason_id(),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.code(response), "NOT_YOUR_CLASS")

    def test_an_administrator_asks_for_any_class_they_can_open(self):
        self.friday(tunde="A")
        row = self.ask(self.tunde, "P", user=self.lekki_admin)
        self.assertEqual(row["requested_by_name"], "Femi Lawal")

    def test_someone_with_no_attendance_key_is_refused(self):
        self.friday(tunde="A")
        response = self.correct(
            self.nobody, self.jss1a, self.tunde, "P", reason_code=self.reason_id(),
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CorrectionRequest.objects.exists())

    def test_a_branch_admin_cannot_ask_about_another_branchs_class(self):
        self.friday(tunde="A")
        response = self.correct(
            self.ikeja_admin, self.jss1a, self.tunde, "P", reason_code=self.reason_id(),
        )
        self.assertEqual(response.status_code, 404)

    def test_another_schools_class_and_student_are_not_found(self):
        self.friday(tunde="A")
        foreign_class = self.correct(
            self.solo_admin, self.jss1a, self.tunde, "P", reason_code=self.reason_id(),
        )
        self.assertEqual(foreign_class.status_code, 404)
        self.take(self.solo_teacher, self.solo_class, LAST_FRIDAY)
        foreign_student = self.correct(
            self.solo_admin, self.solo_class, self.tunde, "P",
            reason_code=self.reason_id(tenant=self.solo),
        )
        self.assertEqual(foreign_student.status_code, 404)

    def test_the_switch_on_the_mark_asked_for_governs_who_may_ask(self):
        self.friday(tunde="A")
        keys = install_declared_fields("attendance.correction")
        key = "attendance.correction.to_mark"
        self.assertIn(key, keys)
        set_field_access(self.teacher_role, key, read=True, write=False)
        response = self.correct(
            self.eze, self.jss1a, self.tunde, "P", reason_code=self.reason_id(),
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CorrectionRequest.objects.exists())


class DecidingTests(_Corrections):
    def test_approving_changes_the_mark_and_keeps_the_old_one_in_history(self):
        self.friday(tunde="A")
        row = self.ask(self.tunde, "P", note="Was at the clinic")
        response = self.decide(self.admin, row["id"], "approve", decision_note="Seen the letter")
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertEqual(body["status"], "APPROVED")
        self.assertEqual(body["decided_by_name"], "Ngozi Adeyemi")
        self.assertEqual(body["decision_note"], "Seen the letter")

        mark = self.mark_of(self.tunde)
        self.assertEqual(mark.mark, "P")
        self.assertEqual(mark.note, "Was at the clinic")
        [change] = MarkChange.objects.filter(mark=mark)
        self.assertEqual((change.from_mark, change.to_mark), ("A", "P"))
        self.assertEqual(change.via, ChangeVia.CORRECTION)
        self.assertEqual(change.changed_by, self.admin)

        register = self.data(self.read(self.admin, self.jss1a, LAST_FRIDAY))
        [entry] = register["history"]
        self.assertEqual((entry["from_mark"], entry["to_mark"], entry["via"]), ("A", "P", "CORRECTION"))

    def test_correcting_an_absence_that_was_emailed_sends_the_correction_notice(self):
        self.friday(chiamaka="A")
        self.assertEqual(self.events(), [EVENT_ABSENCE_NOTICE])
        row = self.ask(self.chiamaka, "P")
        self.assertEqual(self.events(), [EVENT_ABSENCE_NOTICE])
        self.decide(self.admin, row["id"], "approve")
        self.assertEqual(self.events(), [EVENT_ABSENCE_NOTICE, EVENT_CORRECTION_NOTICE])
        call = self.send.call_args.kwargs
        self.assertEqual(call["context"]["mark_label"], "Present")
        self.assertEqual(call["context"]["student_first_name"], "Chiamaka")
        self.assertEqual(self.mark_of(self.chiamaka).notice_state, NoticeState.SENT)

    def test_correcting_an_absence_nobody_was_told_about_sends_nothing(self):
        self.friday(tunde="A")
        self.assertEqual(self.mark_of(self.tunde).notice_state, NoticeState.NO_EMAIL)
        row = self.ask(self.tunde, "P")
        self.decide(self.admin, row["id"], "approve")
        self.assertEqual(self.events(), [])
        self.assertEqual(self.mark_of(self.tunde).notice_state, NoticeState.NONE)

    def test_correcting_a_mark_to_absent_never_sends_an_absence_notice(self):
        self.friday()
        row = self.ask(self.chiamaka, "A")
        self.decide(self.admin, row["id"], "approve")
        self.assertEqual(self.mark_of(self.chiamaka).mark, "A")
        self.assertEqual(self.mark_of(self.chiamaka).notice_state, NoticeState.NONE)
        self.assertEqual(self.events(), [])

    def test_the_details_follow_the_mark_asked_for(self):
        self.friday(tunde="L", kelechi="E", chiamaka="A")
        self.assertIsNotNone(self.mark_of(self.tunde).arrived_at)
        self.assertIsNotNone(self.mark_of(self.kelechi).reason_code_id)

        self.decide(self.admin, self.ask(self.tunde, "E", reason_code=self.reason_id("Travel"))["id"], "approve")
        excused = self.mark_of(self.tunde)
        self.assertEqual((excused.mark, excused.arrived_at), ("E", None))
        self.assertEqual(excused.reason_code.name, "Travel")

        self.decide(self.admin, self.ask(self.kelechi, "L", arrived_at="09:05")["id"], "approve")
        late = self.mark_of(self.kelechi)
        self.assertEqual(late.mark, "L")
        self.assertEqual(late.arrived_at, dt.time(9, 5))
        self.assertIsNone(late.reason_code_id)

    def test_rejecting_leaves_the_mark_and_keeps_the_decision(self):
        self.friday(chiamaka="A")
        row = self.ask(self.chiamaka, "P")
        response = self.decide(self.admin, row["id"], "reject", decision_note="No proof")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.data(response)["status"], "REJECTED")
        self.assertEqual(self.mark_of(self.chiamaka).mark, "A")
        self.assertFalse(MarkChange.objects.exists())
        self.assertEqual(self.events(), [EVENT_ABSENCE_NOTICE])
        stored = CorrectionRequest.objects.get(pk=row["id"])
        self.assertEqual((stored.status, stored.decision_note), (CorrectionStatus.REJECTED, "No proof"))

    def test_a_decided_correction_cannot_be_decided_again(self):
        self.friday(tunde="A")
        row = self.ask(self.tunde, "P")
        self.decide(self.admin, row["id"], "approve")
        for verb in ("approve", "reject"):
            response = self.decide(self.lekki_admin, row["id"], verb)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.code(response), "ALREADY_DECIDED")
        self.assertEqual(MarkChange.objects.count(), 1)

    def test_the_person_who_asked_cannot_decide_it_even_holding_the_key(self):
        self.friday(tunde="A")
        row = self.ask(self.tunde, "P", user=self.admin)
        response = self.decide(self.admin, row["id"], "approve")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.code(response), "SELF_DECISION")
        self.assertEqual(CorrectionRequest.objects.get().status, CorrectionStatus.PENDING)
        self.assertEqual(self.decide(self.lekki_admin, row["id"], "approve").status_code, 200)

    def test_a_teacher_cannot_decide(self):
        self.friday(tunde="A")
        row = self.ask(self.tunde, "P")
        self.assertEqual(self.decide(self.eze, row["id"], "approve").status_code, 403)
        self.assertEqual(self.decide(self.bello, row["id"], "reject").status_code, 403)

    def test_another_schools_correction_is_not_found(self):
        self.friday(tunde="A")
        row = self.ask(self.tunde, "P")
        self.assertEqual(self.decide(self.solo_admin, row["id"], "approve").status_code, 404)
        self.assertEqual(CorrectionRequest.objects.get().status, CorrectionStatus.PENDING)

    def test_a_branch_admin_cannot_decide_another_branchs_correction(self):
        self.friday(tunde="A")
        row = self.ask(self.tunde, "P")
        self.assertEqual(self.decide(self.ikeja_admin, row["id"], "approve").status_code, 404)
        self.assertEqual(self.decide(self.lekki_admin, row["id"], "approve").status_code, 200)

    def test_a_mark_that_moved_on_since_the_request_is_not_overwritten(self):
        self.friday(tunde="A")
        row = self.ask(self.tunde, "P")
        StudentMark.objects.filter(student=self.tunde).update(mark="E", reason_code_id=self.reason_id())
        response = self.decide(self.admin, row["id"], "approve")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "CORRECTION_STALE")
        self.assertEqual(self.mark_of(self.tunde).mark, "E")
        self.assertEqual(self.decide(self.admin, row["id"], "reject").status_code, 200)

    def test_the_decision_is_audited_under_the_attendance_module(self):
        self.friday(tunde="A")
        row = self.ask(self.tunde, "P")
        self.decide(self.admin, row["id"], "approve")
        events = AuditEvent.objects.filter(entity_type="CorrectionRequest")
        self.assertEqual({e.module_key for e in events}, {"ATTENDANCE"})
        self.assertEqual(events.count(), 2)
        self.assertTrue(any("approved" in e.summary for e in events))

    def test_the_decision_note_is_governed_by_its_own_switch(self):
        self.friday(tunde="A")
        row = self.ask(self.tunde, "P")
        install_declared_fields("attendance.correction")
        set_field_access(self.admin_role, "attendance.correction.decision_note", read=True, write=False)
        refused = self.decide(self.lekki_admin, row["id"], "approve", decision_note="Fine")
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(self.decide(self.lekki_admin, row["id"], "approve").status_code, 200)


class ListingTests(_Corrections):
    def setUp(self):
        super().setUp()
        self.friday(tunde="A", chiamaka="A")
        self.mine = self.ask(self.tunde, "P")
        self.eze_row = self.ask(self.chiamaka, "P", user=self.admin)

    def ids(self, response):
        return [row["id"] for row in self.data(response)]

    def test_an_approver_reads_every_correction_in_their_branches_newest_first(self):
        response = self.corrections(self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ids(response), [self.eze_row["id"], self.mine["id"]])
        self.assertEqual(response.data["pagination"]["totalItems"], 2)

    def test_a_requester_without_the_approve_key_reads_only_their_own(self):
        self.assertEqual(self.ids(self.corrections(self.eze)), [self.mine["id"]])
        self.assertEqual(self.ids(self.corrections(self.bello)), [])

    def test_a_branch_bound_reader_sees_only_their_own_branchs_corrections(self):
        self.assertEqual(self.ids(self.corrections(self.ikeja_admin)), [])
        self.assertEqual(len(self.ids(self.corrections(self.lekki_admin))), 2)

    def test_someone_with_neither_key_is_refused(self):
        self.assertEqual(self.corrections(self.nobody).status_code, 403)

    def test_status_filters_split_waiting_from_decided(self):
        self.decide(self.lekki_admin, self.mine["id"], "approve")
        pending = self.corrections(self.admin, status="pending")
        decided = self.corrections(self.admin, status="decided")
        self.assertEqual(self.ids(pending), [self.eze_row["id"]])
        self.assertEqual(self.ids(decided), [self.mine["id"]])
        self.assertEqual(self.data(decided)[0]["status"], "APPROVED")
        self.assertEqual(self.corrections(self.admin, status="everything").status_code, 400)

    def test_the_branch_filter_narrows_and_refuses_a_branch_the_caller_cannot_see(self):
        self.assertEqual(len(self.ids(self.corrections(self.admin, branch=self.lekki.pk))), 2)
        self.assertEqual(self.ids(self.corrections(self.admin, branch=self.ikeja.pk)), [])
        self.assertEqual(self.corrections(self.lekki_admin, branch=self.ikeja.pk).status_code, 404)
        self.assertEqual(self.corrections(self.admin, branch=999999).status_code, 404)

    def test_awaiting_me_leaves_out_the_requests_the_caller_made(self):
        # The admin asked for eze_row, so only the other request waits on them;
        # both wait on the Lekki administrator, who asked for neither.
        self.assertEqual(self.ids(self.corrections(self.admin, awaiting="me")), [self.mine["id"]])
        self.assertEqual(len(self.ids(self.corrections(self.lekki_admin, awaiting="me"))), 2)
        self.assertEqual(self.corrections(self.admin, awaiting="others").status_code, 400)

    def test_an_empty_queue_is_an_empty_list(self):
        response = self.corrections(self.admin, status="decided")
        self.assertEqual(response.data["data"], [])
        self.assertEqual(response.data["pagination"]["totalItems"], 0)

    def test_the_list_is_paginated(self):
        response = self.corrections(self.admin, page_size=1)
        self.assertEqual(len(self.data(response)), 1)
        self.assertEqual(response.data["pagination"]["totalPages"], 2)

    def test_a_single_branch_school_has_no_branch_column(self):
        self.assertEqual(
            self.take(self.solo_teacher, self.solo_class, LAST_FRIDAY).status_code, 200,
        )
        response = self.correct(
            self.solo_teacher, self.solo_class, self.solo_pupil, "A",
            reason_code=self.reason_id(tenant=self.solo),
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(self.data(response)["branch"])
        [row] = self.data(TenantAPIClient(self.solo_admin).get(reverse("attendance-corrections")))
        self.assertIsNone(row["branch"])
        self.assertEqual(row["class_name"], "Basic 3")

    def test_the_number_of_queries_does_not_grow_with_the_corrections(self):
        def count():
            with CaptureQueriesContext(connection) as queries:
                response = self.corrections(self.admin)
            self.assertEqual(response.status_code, 200)
            return len(queries)

        self.decide(self.lekki_admin, self.mine["id"], "reject")
        self.decide(self.lekki_admin, self.eze_row["id"], "reject")
        self.ask(self.tunde, "P")
        few = count()
        self.ask(self.chiamaka, "P")
        self.ask(self.kelechi, "A")
        self.assertEqual(len(self.data(self.corrections(self.admin))), 5)
        self.assertEqual(count(), few)

    def test_a_correction_field_the_role_may_not_read_is_absent(self):
        keys = install_declared_fields("attendance.correction")
        set_field_access(self.admin_role, "attendance.correction.note", read=False, write=False)
        self.assertIn("attendance.correction.note", keys)
        row = self.data(self.corrections(self.admin))[0]
        self.assertNotIn("note", row)
        self.assertIn("status", row)
