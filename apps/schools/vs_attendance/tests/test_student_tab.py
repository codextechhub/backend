"""A student's Attendance tab over a short term.

The first term is shortened to start on Monday 6 October so the tab has six
weekdays to account for: the 6th, the 7th (Lekki Open Day, Lekki only), the 8th,
the 9th (Founders' Day, everyone), the 10th and today, the 13th. Registers are
written straight to the tables, so every shape of day is exact.

* Tunde: present on the 6th, absent on the 8th, excused on the 10th, nothing
  taken today.
* Chiamaka: late on the 6th, present after.
* Kelechi moved from JSS1 A to JSS1 B on the 10th, so her first days are judged
  against JSS1 A and her last against JSS1 B.
* Amina was suspended on the 6th and Obinna joins on the 20th.
"""
from __future__ import annotations

import datetime as dt

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_academics.models import AcademicTerm
from schools.vs_students.models import ClassEnrolment
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from ..constants import ChangeVia, CorrectionStatus, NoticeState
from ..models import ClassRegister, CorrectionRequest, MarkChange, ReasonCode, StudentMark
from ..services.reasons import ensure_default_reason_codes
from .base import AttendanceFixture, school_clock

D = dt.date


def at(day, hour=9):
    return dt.datetime(2025, 10, day, hour, 0, tzinfo=dt.timezone.utc)


class _Tab(AttendanceFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        AcademicTerm.all_objects.filter(pk=cls.first_term.pk).update(start_date=D(2025, 10, 6))
        cls.first_term.refresh_from_db()
        ensure_default_reason_codes(cls.tenant)
        cls.sick = ReasonCode.objects.get(tenant=cls.tenant, name="Sick")
        ClassEnrolment.all_objects.filter(student=cls.kelechi).update(is_active=False)
        ClassEnrolment.all_objects.create(
            tenant=cls.tenant, student=cls.kelechi, school_class=cls.jss1b,
            session=cls.year, is_active=True, effective_date=D(2025, 10, 10),
        )
        cls.ngozi = cls.make_student("Ngozi", "Ibe", cls.sss2, "BFS/401", branch=cls.ikeja)

        def register(school_class, day):
            return ClassRegister.objects.create(
                tenant=cls.tenant, school_class=school_class, date=D(2025, 10, day),
                branch=cls.lekki, session=cls.year, term=cls.first_term,
                submitted_at=at(day), submitted_by=cls.eze,
            )

        def mark(reg, student, kind, **extra):
            return StudentMark.objects.create(
                tenant=cls.tenant, register=reg, student=student, mark=kind,
                marked_by=cls.eze, **extra,
            )

        monday, wednesday, friday = register(cls.jss1a, 6), register(cls.jss1a, 8), register(cls.jss1a, 10)
        friday_b = register(cls.jss1b, 10)
        mark(monday, cls.tunde, "P")
        mark(monday, cls.chiamaka, "L", arrived_at=dt.time(8, 40))
        mark(monday, cls.kelechi, "P")
        cls.tunde_absent = mark(
            wednesday, cls.tunde, "A", note="Mother phoned", notice_state=NoticeState.NO_EMAIL,
        )
        mark(wednesday, cls.chiamaka, "P")
        mark(wednesday, cls.kelechi, "P")
        cls.tunde_excused = mark(friday, cls.tunde, "E", reason_code=cls.sick, note="Clinic")
        mark(friday, cls.chiamaka, "P")
        mark(friday_b, cls.kelechi, "A")

        MarkChange.objects.create(
            tenant=cls.tenant, mark=cls.tunde_absent, from_mark="P", to_mark="A",
            changed_by=cls.eze, changed_at=at(8), via=ChangeVia.SAME_DAY, note="Mother phoned",
        )
        CorrectionRequest.objects.create(
            tenant=cls.tenant, mark=cls.tunde_absent, from_mark="A", to_mark="P",
            reason_code=cls.sick, note="Was ill", requested_by=cls.eze, requested_at=at(9, 8),
            status=CorrectionStatus.REJECTED, decided_by=cls.admin, decided_at=at(9, 10),
            decision_note="No letter",
        )
        CorrectionRequest.objects.create(
            tenant=cls.tenant, mark=cls.tunde_excused, from_mark="A", to_mark="E",
            reason_code=cls.sick, note="Clinic", requested_by=cls.eze, requested_at=at(11, 8),
            status=CorrectionStatus.APPROVED, decided_by=cls.admin, decided_at=at(12, 8),
        )
        MarkChange.objects.create(
            tenant=cls.tenant, mark=cls.tunde_excused, from_mark="A", to_mark="E",
            changed_by=cls.admin, changed_at=at(12, 8), via=ChangeVia.CORRECTION,
        )
        CorrectionRequest.objects.create(
            tenant=cls.tenant, mark=cls.tunde_absent, from_mark="A", to_mark="E",
            reason_code=cls.sick, requested_by=cls.eze,
        )

    def tab(self, user, student, **params):
        response = self.student_tab(user, student, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    @staticmethod
    def states(body):
        return {cell["date"]: cell["state"] for cell in body["days"]}


class CountsTests(_Tab):
    def test_a_pupil_with_every_kind_of_day(self):
        body = self.tab(self.admin, self.tunde)
        self.assertEqual(body["counts"], {
            "present": 1, "late": 0, "absent": 1, "excused": 1,
            "not_taken": 1, "opened": 4, "rate": 33.3,
        })
        self.assertEqual(self.states(body), {
            "2025-10-06": "MARKED", "2025-10-07": "CLOSED", "2025-10-08": "MARKED",
            "2025-10-09": "CLOSED", "2025-10-10": "MARKED", "2025-10-13": "NOT_TAKEN",
        })

    def test_late_counts_as_present_and_as_late(self):
        body = self.tab(self.admin, self.chiamaka)
        counts = body["counts"]
        self.assertEqual(
            (counts["present"], counts["late"], counts["absent"], counts["rate"]),
            (3, 1, 0, 100.0),
        )
        self.assertEqual((body["days"][0]["mark"], body["days"][0]["arrived_at"]), ("L", "08:40"))

    def test_a_pupil_who_changed_class_is_judged_against_the_class_they_were_in(self):
        body = self.tab(self.admin, self.kelechi)
        self.assertEqual(body["student"]["class_name"], "JSS1 B")
        self.assertEqual(
            {k: body["counts"][k] for k in ("present", "absent", "not_taken", "rate")},
            {"present": 2, "absent": 1, "not_taken": 1, "rate": 66.7},
        )
        self.assertEqual(
            {c["date"]: c["mark"] for c in body["days"] if c["state"] == "MARKED"},
            {"2025-10-06": "P", "2025-10-08": "P", "2025-10-10": "A"},
        )

    def test_each_day_names_the_class_whose_register_it_belongs_to(self):
        days = {c["date"]: c["class_id"] for c in self.tab(self.admin, self.kelechi)["days"]}
        self.assertEqual(days["2025-10-08"], self.jss1a.pk)
        self.assertEqual(days["2025-10-10"], self.jss1b.pk)
        off_roll = self.tab(self.admin, self.obinna)["days"]
        self.assertEqual({c["class_id"] for c in off_roll}, {None})

    def test_a_suspended_pupil_is_counted_nowhere(self):
        body = self.tab(self.admin, self.amina)
        self.assertEqual(self.states(body), {
            "2025-10-06": "SUSPENDED", "2025-10-07": "CLOSED", "2025-10-08": "SUSPENDED",
            "2025-10-09": "CLOSED", "2025-10-10": "SUSPENDED", "2025-10-13": "SUSPENDED",
        })
        self.assertEqual(body["counts"], {
            "present": 0, "late": 0, "absent": 0, "excused": 0,
            "not_taken": 0, "opened": 0, "rate": None,
        })

    def test_a_pupil_not_yet_on_the_roll_has_no_expected_days(self):
        body = self.tab(self.admin, self.obinna)
        self.assertEqual(set(self.states(body).values()), {"NOT_ON_ROLL"})
        self.assertEqual(body["counts"]["opened"], 0)
        self.assertIsNone(body["counts"]["rate"])
        self.assertIsNone(body["student"]["class_name"])

    def test_a_closed_day_names_why_and_an_open_one_does_not(self):
        days = {c["date"]: c for c in self.tab(self.admin, self.tunde)["days"]}
        self.assertEqual(days["2025-10-07"]["closed_reason"], "Lekki Open Day")
        self.assertEqual(days["2025-10-09"]["closed_reason"], "Founders' Day")
        self.assertIsNone(days["2025-10-08"]["closed_reason"])
        self.assertIsNone(days["2025-10-07"]["mark"])

    def test_a_branch_closure_leaves_a_pupil_at_another_branch_open(self):
        states = self.states(self.tab(self.ikeja_admin, self.ngozi))
        self.assertEqual(states["2025-10-07"], "NOT_TAKEN")
        self.assertEqual(states["2025-10-09"], "CLOSED")

    def test_weekends_are_not_cells(self):
        dates = [cell["date"] for cell in self.tab(self.admin, self.tunde)["days"]]
        self.assertNotIn("2025-10-11", dates)
        self.assertEqual(dates, sorted(dates))

    def test_a_register_not_submitted_is_not_taken(self):
        ClassRegister.objects.filter(school_class=self.jss1a, date=D(2025, 10, 8)).update(
            submitted_at=None,
        )
        body = self.tab(self.admin, self.tunde)
        self.assertEqual(self.states(body)["2025-10-08"], "NOT_TAKEN")
        self.assertEqual(body["counts"]["absent"], 0)

    def test_the_student_block_names_the_branch_only_where_there_are_several(self):
        self.assertEqual(self.tab(self.admin, self.tunde)["student"], {
            "id": self.tunde.pk, "name": "Tunde Bello", "student_number": "BFS/002",
            "class_name": "JSS1 A", "branch": {"id": self.lekki.pk, "name": "Lekki Branch"},
        })
        self.assertIsNone(self.tab(self.solo_admin, self.solo_pupil)["student"]["branch"])


class TermTests(_Tab):
    def test_the_current_term_is_the_default_and_the_pills_are_listed(self):
        body = self.tab(self.admin, self.tunde)
        self.assertEqual(body["term"]["id"], self.first_term.pk)
        self.assertEqual(body["term"]["session_name"], "2025/2026")
        self.assertTrue(body["term"]["is_current"])
        self.assertEqual([t["id"] for t in body["terms"]], [self.first_term.pk])

    def test_a_term_that_has_not_begun_has_no_days(self):
        second = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=self.year, name="Second Term", order_index=2,
            start_date=D(2026, 1, 12), end_date=D(2026, 4, 3),
        )
        body = self.tab(self.admin, self.tunde, term=second.pk)
        self.assertEqual(body["term"]["name"], "Second Term")
        self.assertEqual(body["days"], [])
        self.assertEqual(body["counts"]["opened"], 0)
        self.assertEqual([t["id"] for t in body["terms"]], [self.first_term.pk])

    def test_between_terms_the_latest_past_term_is_the_default(self):
        with school_clock(dt.datetime(2025, 12, 20, 12, tzinfo=dt.timezone.utc)):
            body = self.tab(self.admin, self.tunde)
        self.assertEqual(body["term"]["id"], self.first_term.pk)
        self.assertFalse(body["term"]["is_current"])
        self.assertEqual(body["days"][-1]["date"], "2025-12-12")

    def test_another_schools_term_is_not_found_and_a_bad_term_is_refused(self):
        solo_term = AcademicTerm.all_objects.filter(tenant=self.solo).first()
        self.assertEqual(self.student_tab(self.admin, self.tunde, term=solo_term.pk).status_code, 404)
        self.assertEqual(self.student_tab(self.admin, self.tunde, term="first").status_code, 400)

    def test_a_school_with_no_term_yet_answers_with_nothing_to_show(self):
        AcademicTerm.all_objects.filter(tenant=self.solo).delete()
        body = self.tab(self.solo_admin, self.solo_pupil)
        self.assertIsNone(body["term"])
        self.assertEqual(
            (body["days"], body["absences"], body["history"], body["terms"]), ([], [], [], []),
        )


class AbsencesAndHistoryTests(_Tab):
    def test_absent_and_excused_days_come_newest_first(self):
        rows = self.tab(self.admin, self.tunde)["absences"]
        self.assertEqual(
            [(r["date"], r["mark"]) for r in rows], [("2025-10-10", "E"), ("2025-10-08", "A")],
        )
        self.assertEqual((rows[0]["reason_label"], rows[0]["note"]), ("Sick", "Clinic"))
        self.assertEqual((rows[1]["notice_state"], rows[1]["note"]), ("NO_EMAIL", "Mother phoned"))
        self.assertEqual(rows[0]["class_name"], "JSS1 A")

    def test_a_pupil_with_no_absence_has_an_empty_list(self):
        self.assertEqual(self.tab(self.admin, self.chiamaka)["absences"], [])

    def test_history_holds_changes_and_decided_corrections_newest_first(self):
        history = self.tab(self.admin, self.tunde)["history"]
        self.assertEqual(
            [(e["kind"], e.get("status")) for e in history],
            [("correction", "APPROVED"), ("correction", "REJECTED"), ("change", None)],
        )
        approved, rejected, change = history
        self.assertEqual((approved["from_mark"], approved["to_mark"]), ("A", "E"))
        self.assertEqual(approved["requested_by_name"], "Chidi Eze")
        self.assertEqual(approved["decided_by_name"], "Ngozi Adeyemi")
        self.assertEqual(approved["via"], "CORRECTION")
        self.assertEqual((rejected["to_mark"], rejected["decision_note"]), ("P", "No letter"))
        self.assertEqual(rejected["reason_label"], "Sick")
        self.assertEqual(
            (change["from_mark"], change["to_mark"], change["changed_by_name"]),
            ("P", "A", "Chidi Eze"),
        )

    def test_a_correction_still_waiting_is_not_history_and_an_approval_is_one_entry(self):
        history = self.tab(self.admin, self.tunde)["history"]
        self.assertEqual(len(history), 3)
        self.assertEqual([e for e in history if e["kind"] == "change" and e["via"] == "CORRECTION"], [])

    def test_a_pupil_nothing_happened_to_has_an_empty_history(self):
        self.assertEqual(self.tab(self.admin, self.chiamaka)["history"], [])


class FieldAccessTests(_Tab):
    def setUp(self):
        super().setUp()
        install_declared_fields("attendance.register", "attendance.correction", "school.guardians")

    def test_a_hidden_mark_leaves_the_days_but_not_what_they_were(self):
        set_field_access(self.admin_role, "attendance.register.mark", read=False, write=False)
        body = self.tab(self.admin, self.tunde)
        for cell in body["days"]:
            self.assertNotIn("mark", cell)
        self.assertEqual(self.states(body)["2025-10-08"], "MARKED")
        self.assertEqual(self.states(body)["2025-10-07"], "CLOSED")
        for row in body["absences"]:
            self.assertNotIn("mark", row)
        [change] = [e for e in body["history"] if e["kind"] == "change"]
        self.assertNotIn("from_mark", change)

    def test_the_note_reason_and_notice_follow_their_switches_everywhere(self):
        set_field_access(
            self.admin_role, "attendance.register.note", "attendance.register.reason_code",
            "attendance.register.notice_state", "attendance.register.arrived_at",
            read=False, write=False,
        )
        body = self.tab(self.admin, self.tunde)
        for row in body["absences"]:
            self.assertEqual(set(row), {"date", "class_name", "mark"})
        self.assertNotIn("arrived_at", body["days"][0])
        [change] = [e for e in body["history"] if e["kind"] == "change"]
        self.assertNotIn("note", change)

    def test_the_counts_have_their_own_switch(self):
        set_field_access(self.admin_role, "attendance.register.counts", read=False, write=False)
        self.assertNotIn("counts", self.tab(self.admin, self.tunde))

    def test_decided_corrections_follow_the_correction_switches(self):
        set_field_access(
            self.admin_role, "attendance.correction.note", "attendance.correction.decision_note",
            "attendance.correction.decided_by", read=False, write=False,
        )
        approved = self.tab(self.admin, self.tunde)["history"][0]
        for name in ("note", "decision_note", "decided_by_name", "decided_at"):
            self.assertNotIn(name, approved)
        self.assertEqual(approved["status"], "APPROVED")

    def test_the_guardian_is_read_through_the_student_modules_switches(self):
        self.assertEqual(
            self.tab(self.admin, self.chiamaka)["primary_guardian"],
            {"name": "Mrs. Ngozi Nwosu", "email": "ngozi.nwosu@example.ng"},
        )
        self.assertEqual(
            self.tab(self.admin, self.tunde)["primary_guardian"],
            {"name": "Mrs. Aduke Bello", "email": None},
        )

    def test_a_hidden_guardian_email_is_left_out_not_sent_blank(self):
        set_field_access(self.admin_role, "school.guardians.email", read=False, write=False)
        self.assertEqual(
            self.tab(self.admin, self.chiamaka)["primary_guardian"], {"name": "Mrs. Ngozi Nwosu"},
        )
        self.assertNotIn("email", self.tab(self.admin, self.tunde)["primary_guardian"])

    def test_a_guardian_whose_name_and_email_are_both_hidden_is_left_out(self):
        set_field_access(
            self.admin_role, "school.guardians.email", "school.guardians.first_name",
            "school.guardians.middle_name", "school.guardians.last_name",
            read=False, write=False,
        )
        self.assertNotIn("primary_guardian", self.tab(self.admin, self.chiamaka))

    def test_a_pupil_with_no_primary_guardian_has_a_null_one(self):
        self.assertIsNone(self.tab(self.admin, self.amina)["primary_guardian"])


class AccessTests(_Tab):
    def test_someone_with_no_attendance_key_is_refused(self):
        self.assertEqual(self.student_tab(self.nobody, self.tunde).status_code, 403)

    def test_another_schools_student_is_not_found(self):
        self.assertEqual(self.student_tab(self.solo_admin, self.tunde).status_code, 404)
        self.assertEqual(self.student_tab(self.admin, self.solo_pupil).status_code, 404)

    def test_a_branch_bound_reader_cannot_open_another_branchs_student(self):
        self.assertEqual(self.student_tab(self.lekki_admin, self.ngozi).status_code, 404)
        self.assertEqual(self.student_tab(self.ikeja_admin, self.tunde).status_code, 404)
        self.assertEqual(self.student_tab(self.ikeja_admin, self.ngozi).status_code, 200)
        self.assertEqual(self.student_tab(self.lekki_admin, self.tunde).status_code, 200)

    def test_a_teacher_with_the_view_key_reads_the_tab(self):
        self.assertEqual(self.student_tab(self.eze, self.tunde).status_code, 200)

    def test_an_unknown_student_is_not_found(self):
        response = TenantAPIClient(self.admin).get(
            reverse("attendance-student", kwargs={"student_id": 999999}),
        )
        self.assertEqual(response.status_code, 404)


class QueryCountTests(_Tab):
    def count(self, student):
        with CaptureQueriesContext(connection) as queries:
            response = self.student_tab(self.admin, student)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_the_queries_do_not_depend_on_how_many_changes_and_corrections(self):
        self.count(self.tunde)
        before = self.count(self.tunde)
        for hour in range(10, 14):
            MarkChange.objects.create(
                tenant=self.tenant, mark=self.tunde_absent, from_mark="A", to_mark="E",
                changed_by=self.eze, changed_at=at(8, hour), via=ChangeVia.SAME_DAY,
            )
            CorrectionRequest.objects.create(
                tenant=self.tenant, mark=self.tunde_excused, from_mark="E", to_mark="P",
                reason_code=self.sick, requested_by=self.eze, requested_at=at(9, hour),
                status=CorrectionStatus.REJECTED, decided_by=self.admin, decided_at=at(9, hour),
            )
        self.assertEqual(len(self.tab(self.admin, self.tunde)["history"]), 3 + 8)
        self.assertEqual(self.count(self.tunde), before)
        self.assertLess(before, 45)

    def test_a_longer_term_costs_no_more(self):
        self.count(self.tunde)
        short = self.count(self.tunde)
        AcademicTerm.all_objects.filter(pk=self.first_term.pk).update(start_date=D(2025, 9, 8))
        self.assertEqual(self.count(self.tunde), short)
