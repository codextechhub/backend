"""Missed a lesson, and the lesson blocks on the hubs, the student's tab and the report.

Monday 13 October 2025. The morning register of JSS1 A marks Tunde Absent,
Kelechi Late and Chiamaka Present. Mr Eze's English lesson then marks all three
Absent: Kelechi and Chiamaka were in school and missed it, Tunde was not in
school at all.
"""
from __future__ import annotations

import datetime as dt

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_calendar.models import PublishState, TimetableSlot

from ..constants import CFG_LESSON_MARKING_LEVELS
from .base import FOUNDERS_DAY, TODAY, set_setting
from .lesson_base import LAST_MONDAY, LessonFixture, publish


class MissedALessonTests(LessonFixture):
    def test_a_student_in_school_who_was_absent_from_a_lesson_is_listed(self):
        self.morning()
        self.english_lesson()
        rows = self.hub()["missed_lessons"]
        self.assertEqual(
            [(r["student"]["name"], r["morning_mark"]) for r in rows],
            [("Kelechi Eze", "L"), ("Chiamaka Nwosu", "P")],
        )
        first = rows[0]
        self.assertEqual(first["class_name"], "JSS1 A")
        self.assertEqual(first["period_label"], "Period 1")
        self.assertEqual(first["subject"], "English Language")
        self.assertIsNotNone(first["lesson_submitted_at"])
        self.assertEqual(first["student"]["student_number"], "BFS/003")
        self.assertEqual(self.hub()["kpis"]["missed_a_lesson"], 2)

    def test_a_student_absent_in_the_morning_has_not_missed_a_lesson(self):
        self.morning()
        self.english_lesson()
        names = [r["student"]["name"] for r in self.hub()["missed_lessons"]]
        self.assertNotIn("Tunde Bello", names)

    def test_a_student_excused_in_the_morning_has_not_missed_a_lesson(self):
        self.morning(tunde="E", kelechi="E")
        self.english_lesson()
        self.assertEqual(self.hub()["kpis"]["missed_a_lesson"], 1)

    def test_both_registers_must_be_submitted(self):
        self.english_lesson()
        self.assertEqual(self.hub()["kpis"]["missed_a_lesson"], 0)
        self.morning()
        self.assertEqual(self.hub()["kpis"]["missed_a_lesson"], 2)

    def test_a_lesson_register_still_being_filled_in_misses_nobody(self):
        self.morning()
        for pupil in (self.tunde, self.kelechi, self.chiamaka):
            self.mark_lesson(self.eze, self.a_english, pupil, "A")
        self.assertEqual(self.hub()["kpis"]["missed_a_lesson"], 0)

    def test_a_morning_register_still_being_filled_in_does_not_count(self):
        for pupil, kind in ((self.tunde, "P"), (self.kelechi, "P"), (self.chiamaka, "P")):
            self.mark(self.eze, self.jss1a, pupil, kind)
        self.english_lesson()
        self.assertEqual(self.hub()["kpis"]["missed_a_lesson"], 0)

    def test_nobody_missing_is_zero_and_an_empty_list_not_an_absent_key(self):
        self.morning()
        self.english_lesson(tunde="P", kelechi="P", chiamaka="P")
        body = self.hub()
        self.assertEqual(body["kpis"]["missed_a_lesson"], 0)
        self.assertEqual(body["missed_lessons"], [])

    def test_with_lesson_marking_off_neither_the_count_nor_the_list_is_sent(self):
        self.morning()
        self.english_lesson()
        self.switch_lessons_off()
        body = self.hub()
        self.assertNotIn("missed_lessons", body)
        self.assertNotIn("missed_a_lesson", body["kpis"])

    def test_a_scope_with_no_class_in_a_marking_level_does_not_send_them(self):
        set_setting(CFG_LESSON_MARKING_LEVELS, ["PRY4"], self.tenant, self.admin)
        body = self.hub(branch=self.ikeja.pk)
        self.assertNotIn("missed_lessons", body)
        self.assertNotIn("missed_a_lesson", body["kpis"])
        self.assertIn("missed_lessons", self.hub())

    def test_a_branch_sees_only_its_own_missed_lessons(self):
        self.morning()
        self.english_lesson()
        self.assertEqual(self.hub(self.ikeja_admin)["missed_lessons"], [])
        self.assertEqual(self.hub(self.lekki_admin)["kpis"]["missed_a_lesson"], 2)
        self.assertEqual(self.hub(branch=self.ikeja.pk)["kpis"]["missed_a_lesson"], 0)
        self.assertEqual(self.hub(branch=self.lekki.pk)["kpis"]["missed_a_lesson"], 2)

    def test_a_missed_lesson_is_the_days_own(self):
        self.morning(on_date=LAST_MONDAY, tunde="P", kelechi="P", chiamaka="P")
        self.english_lesson(LAST_MONDAY, chiamaka="A", tunde="P", kelechi="P")
        self.assertEqual(self.hub()["kpis"]["missed_a_lesson"], 0)
        self.assertEqual(self.hub(date=str(LAST_MONDAY))["kpis"]["missed_a_lesson"], 1)

    def test_at_a_school_with_one_branch(self):
        self.take(self.solo_teacher, self.solo_class, TODAY)
        self.take_lesson(self.solo_teacher, self.solo_slot, TODAY, ebube="A")
        body = self.hub(self.solo_admin)
        self.assertEqual(body["kpis"]["missed_a_lesson"], 1)
        self.assertEqual(body["missed_lessons"][0]["student"]["name"], "Ebube Okoro")

    def test_another_schools_lessons_are_not_counted(self):
        self.take(self.solo_teacher, self.solo_class, TODAY)
        self.take_lesson(self.solo_teacher, self.solo_slot, TODAY, ebube="A")
        self.assertEqual(self.hub(self.admin)["kpis"]["missed_a_lesson"], 0)

    def test_a_lesson_register_is_never_part_of_the_class_register_or_its_figures(self):
        self.morning()
        before = self.hub()["kpis"]
        self.english_lesson()
        after = self.hub()["kpis"]
        for name in ("rate", "absent", "late", "excused", "not_taken"):
            self.assertEqual(after[name], before[name], name)
        self.assertEqual(after["absent"], 1)
        self.assertEqual(after["rate"], 66.7)


class NoRateMovesTests(LessonFixture):
    def test_the_student_tab_the_report_and_the_chronic_list_ignore_lesson_marks(self):
        self.morning(tunde="P", kelechi="P", chiamaka="P")
        tab_before = self.tab(self.chiamaka)["counts"]
        report_before = self.report()
        self.english_lesson()
        self.assertEqual(self.tab(self.chiamaka)["counts"], tab_before)
        after = self.report()
        self.assertEqual(after["by_class"], report_before["by_class"])
        self.assertEqual(after["by_level"], report_before["by_level"])
        self.assertEqual(after["chronic"], report_before["chronic"])
        self.assertEqual(after["trend"], report_before["trend"])
        self.assertEqual(tab_before["absent"], 0)
        self.assertEqual(tab_before["rate"], 100.0)

    def test_a_student_absent_from_every_lesson_is_still_100_percent_present(self):
        for day in (LAST_MONDAY, TODAY):
            self.morning(on_date=day, tunde="P", kelechi="P", chiamaka="P")
            self.english_lesson(day)
        self.assertEqual(self.tab(self.chiamaka)["counts"]["rate"], 100.0)
        self.assertEqual(self.report()["chronic"], [])


class TeacherHubTests(LessonFixture):
    def mine(self, user, **params):
        response = self.today_mine(user, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    def test_a_teacher_sees_the_lessons_the_timetable_gives_them(self):
        body = self.mine(self.eze)
        self.assertTrue(body["lesson_marking_on"])
        [card] = body["lessons"]
        self.assertEqual(card, {
            "slot_id": self.a_english.pk, "period_label": "Period 1", "start_time": "08:00",
            "subject": "English Language", "class_name": "JSS1 A", "status": "NOT_STARTED",
            "submitted_at": None,
        })
        self.assertEqual([c["class_name"] for c in body["classes"]], ["JSS1 A"])

    def test_the_lessons_come_by_start_time_and_follow_the_register(self):
        body = self.mine(self.bello)
        self.assertEqual(
            [(c["start_time"], c["class_name"], c["subject"]) for c in body["lessons"]],
            [("08:00", "JSS1 B", "Mathematics"), ("08:45", "JSS1 A", "Mathematics")],
        )
        self.assertEqual(body["classes"][0]["class_name"], "JSS1 B")
        self.take_lesson(self.bello, self.b_maths)
        self.assertEqual(self.mine(self.bello)["lessons"][0]["status"], "SUBMITTED")
        self.assertIsNotNone(self.mine(self.bello)["lessons"][0]["submitted_at"])
        self.assertEqual(self.mine(self.bello)["lessons"][1]["status"], "NOT_STARTED")

    def test_a_teacher_with_no_lessons_gets_an_empty_list_and_the_flag(self):
        body = self.mine(self.admin)
        self.assertEqual(body["lessons"], [])
        self.assertTrue(body["lesson_marking_on"])

    def test_with_lesson_marking_off_the_list_is_empty_and_says_why(self):
        self.switch_lessons_off()
        body = self.mine(self.eze)
        self.assertEqual(body["lessons"], [])
        self.assertFalse(body["lesson_marking_on"])
        self.assertEqual(len(body["classes"]), 1)

    def test_a_lesson_of_a_draft_timetable_is_not_listed(self):
        publish(self.tenant, self.jss1a, PublishState.DRAFT)
        self.assertEqual(self.mine(self.eze)["lessons"], [])

    def test_no_lessons_on_a_closed_day(self):
        body = self.mine(self.eze, date=str(FOUNDERS_DAY))
        self.assertEqual(body["lessons"], [])

    def test_a_teacher_who_leads_no_class_still_sees_the_lessons_they_teach_that_they_can_open(self):
        TimetableSlot.objects.filter(pk=self.a_maths.pk).update(teacher=self.ikeja_admin)
        body = self.mine(self.ikeja_admin)
        self.assertEqual(body["classes"], [])
        # Ikeja's branch administrator cannot open Lekki's JSS1 A, so its
        # Mathematics is not theirs to mark however the timetable reads.
        self.assertEqual([c["class_name"] for c in body["lessons"]], ["SSS2 Science"])

    def test_at_a_school_with_one_branch(self):
        body = self.mine(self.solo_teacher)
        [card] = body["lessons"]
        self.assertEqual((card["class_name"], card["subject"]), ("Basic 3", "Mathematics"))

    def test_a_day_that_has_not_come_is_refused(self):
        self.assertEqual(self.today_mine(self.eze, date="2025-10-14").status_code, 422)


class StudentTabTests(LessonFixture):
    def test_the_lessons_a_student_was_absent_from_are_listed_with_the_morning_mark(self):
        self.morning()
        self.english_lesson()
        rows = self.tab(self.chiamaka)["lesson_absences"]
        self.assertEqual(rows, [{
            "date": "2025-10-13", "period_label": "Period 1",
            "subject": "English Language", "morning_mark": "P",
        }])
        self.assertEqual(self.tab(self.tunde)["lesson_absences"][0]["morning_mark"], "A")

    def test_the_newest_come_first(self):
        for day in (LAST_MONDAY, TODAY):
            self.morning(on_date=day, tunde="P", kelechi="P", chiamaka="P")
            self.english_lesson(day)
        self.assertEqual(
            [r["date"] for r in self.tab(self.chiamaka)["lesson_absences"]],
            ["2025-10-13", "2025-10-06"],
        )

    def test_a_lesson_attended_is_not_listed(self):
        self.morning()
        self.english_lesson(tunde="P", kelechi="P", chiamaka="L")
        self.assertEqual(self.tab(self.chiamaka)["lesson_absences"], [])

    def test_a_morning_register_not_yet_in_reads_null(self):
        self.english_lesson()
        self.assertIsNone(self.tab(self.chiamaka)["lesson_absences"][0]["morning_mark"])

    def test_the_key_is_left_out_when_the_pupils_level_does_not_mark_lessons(self):
        self.switch_lessons_off()
        self.assertNotIn("lesson_absences", self.tab(self.chiamaka))
        set_setting(CFG_LESSON_MARKING_LEVELS, ["PRY4"], self.tenant, self.admin)
        self.assertNotIn("lesson_absences", self.tab(self.chiamaka))
        set_setting(CFG_LESSON_MARKING_LEVELS, ["JSS1"], self.tenant, self.admin)
        self.assertEqual(self.tab(self.chiamaka)["lesson_absences"], [])

    def test_a_branch_bound_reader_cannot_open_another_branchs_student(self):
        self.assertEqual(self.student_tab(self.lekki_admin, self.ngozi).status_code, 404)

    def test_the_day_cells_are_untouched(self):
        self.morning(tunde="P", kelechi="P", chiamaka="P")
        before = self.tab(self.chiamaka)["days"]
        self.english_lesson()
        self.assertEqual(self.tab(self.chiamaka)["days"], before)


class ReportTests(LessonFixture):
    def test_one_row_per_subject_class_and_teacher_with_the_students_who_missed(self):
        self.morning(tunde="P", kelechi="L", chiamaka="P")
        self.english_lesson(tunde="A", kelechi="A", chiamaka="P")
        self.take_lesson(self.bello, self.a_maths)
        rows = self.report()["lessons_missed"]
        self.assertEqual(rows, [
            {
                "subject": "English Language", "class_name": "JSS1 A",
                "teacher_name": "Chidi Eze", "missed": 2,
                "students": ["Kelechi Eze", "Tunde Bello"],
            },
            {
                "subject": "Mathematics", "class_name": "JSS1 A",
                "teacher_name": "Sade Bello", "missed": 0, "students": [],
            },
        ])

    def test_a_student_who_missed_twice_is_counted_twice_and_named_once(self):
        for day in (LAST_MONDAY, TODAY):
            self.morning(on_date=day, tunde="P", kelechi="P", chiamaka="P")
            self.english_lesson(day, chiamaka="A", tunde="P", kelechi="P")
        [row] = [r for r in self.report()["lessons_missed"] if r["subject"] == "English Language"]
        self.assertEqual((row["missed"], row["students"]), (2, ["Chiamaka Nwosu"]))

    def test_a_change_of_teacher_makes_a_new_row_because_the_register_remembers_who_taught(self):
        self.morning(on_date=LAST_MONDAY, tunde="P", kelechi="P", chiamaka="P")
        self.english_lesson(LAST_MONDAY, chiamaka="A", tunde="P", kelechi="P")
        TimetableSlot.objects.filter(pk=self.a_english.pk).update(teacher=self.bello)
        self.morning(tunde="P", kelechi="P", chiamaka="P")
        response = self.take_lesson(self.bello, self.a_english, TODAY, kelechi="A")
        self.assertEqual(response.status_code, 200, response.data)
        rows = [r for r in self.report()["lessons_missed"] if r["subject"] == "English Language"]
        self.assertEqual(
            [(r["teacher_name"], r["missed"], r["students"]) for r in rows],
            [("Chidi Eze", 1, ["Chiamaka Nwosu"]), ("Sade Bello", 1, ["Kelechi Eze"])],
        )

    def test_the_table_is_left_out_when_lesson_marking_is_off(self):
        self.morning()
        self.english_lesson()
        self.assertIn("lessons_missed", self.report())
        self.switch_lessons_off()
        self.assertNotIn("lessons_missed", self.report())

    def test_the_table_is_left_out_for_a_branch_with_no_class_that_marks_lessons(self):
        set_setting(CFG_LESSON_MARKING_LEVELS, ["PRY4"], self.tenant, self.admin)
        self.assertNotIn("lessons_missed", self.report(branch=self.ikeja.pk))

    def test_a_level_switched_off_since_drops_out_of_the_table(self):
        self.morning()
        self.english_lesson()
        set_setting(CFG_LESSON_MARKING_LEVELS, ["PRY4"], self.tenant, self.admin)
        self.assertEqual(self.report()["lessons_missed"], [])

    def test_a_branch_sees_only_its_own_classes(self):
        self.morning()
        self.english_lesson()
        self.assertEqual(self.report(self.ikeja_reporter)["lessons_missed"], [])
        self.assertEqual(len(self.report(self.lekki_reporter)["lessons_missed"]), 1)

    def test_another_schools_lessons_are_not_in_the_table(self):
        self.take(self.solo_teacher, self.solo_class, TODAY)
        self.take_lesson(self.solo_teacher, self.solo_slot, TODAY, ebube="A")
        self.assertEqual(self.report()["lessons_missed"], [])
        [row] = self.report(self.solo_reporter)["lessons_missed"]
        self.assertEqual((row["class_name"], row["missed"]), ("Basic 3", 1))

    def test_the_exports_are_unchanged(self):
        self.morning()
        self.english_lesson()
        response = TenantAPIClient(self.reporter).get(
            reverse("attendance-report-export"), {"table": "by_class"},
        )
        self.assertEqual(response.status_code, 200)


class QueryCountTests(LessonFixture):
    """Every lesson screen costs the same however many classes and lessons there are."""

    def count(self, make_request):
        with CaptureQueriesContext(connection) as queries:
            response = make_request()
        self.assertEqual(response.status_code, 200, response.data)
        return len(queries), self.data(response)

    def grow(self, check, body_of, request, home=None):
        self.morning()
        self.english_lesson()
        self.count(request)
        few, body = self.count(request)
        check(body_of(body), 0)
        for number in range(4):
            self.add_class(number, home)
        many, body = self.count(request)
        check(body_of(body), 1)
        self.assertEqual(many, few)

    def test_the_lessons_list(self):
        self.grow(
            lambda rows, grown: self.assertEqual(len(rows), 4 + (4 if grown else 0)),
            lambda body: body["lessons"],
            lambda: self.lessons(self.admin),
        )

    def test_the_hub(self):
        self.grow(
            lambda rows, grown: self.assertEqual(len(rows), 2 + (8 if grown else 0)),
            lambda body: body["missed_lessons"],
            lambda: self.today(self.admin),
        )

    def test_the_teachers_day(self):
        self.grow(
            lambda rows, grown: self.assertEqual(len(rows), 2 + (4 if grown else 0)),
            lambda body: body["lessons"],
            lambda: self.today_mine(self.bello),
            home=self.lekki,
        )

    def test_the_student_tab(self):
        def request():
            return self.student_tab(self.admin, self.chiamaka)

        self.morning()
        self.english_lesson()
        self.count(request)
        few, body = self.count(request)
        self.assertEqual(len(body["lesson_absences"]), 1)
        for day in (LAST_MONDAY, dt.date(2025, 10, 8), dt.date(2025, 10, 1)):
            self.orm_morning(self.jss1a, self.lekki, day, {self.chiamaka: "P"})
            self.orm_lesson(self.a_english, self.lekki, day, {self.chiamaka: "A"})
        many, body = self.count(request)
        self.assertEqual(len(body["lesson_absences"]), 4)
        self.assertEqual(many, few)

    def test_the_report(self):
        self.grow(
            lambda rows, grown: self.assertEqual(len(rows), 1 + (4 if grown else 0)),
            lambda body: body["lessons_missed"],
            lambda: TenantAPIClient(self.reporter).get(reverse("attendance-reports")),
        )
