"""Reading a score sheet: who may open it, and what it carries."""
from __future__ import annotations

import datetime as dt

from schools.vs_calendar.models import CalendarEvent, EventType, Exam, ExamSlot, PublishState
from schools.vs_gradebook.constants import SheetStatus

from .sheet_base import SheetFixture


class WhoMayOpenASheetTests(SheetFixture):
    def test_the_main_teacher_and_an_assistant_read_it(self):
        for user in (self.eze, self.bello):
            with self.subTest(user=user.email):
                response = self.sheet(user, self.jss1a, self.maths)
                self.assertEqual(response.status_code, 200, response.data)

    def test_the_class_teacher_reads_the_sheets_of_their_class_without_teaching_them(self):
        # Ms Bello leads JSS1 B, which has no main teacher for Mathematics.
        self.assertEqual(self.sheet(self.bello, self.jss1b, self.maths).status_code, 200)
        self.assertEqual(self.sheet(self.musa, self.jss1b, self.maths).status_code, 403)

    def test_a_teacher_with_no_part_in_the_sheet_is_refused_in_words(self):
        response = self.sheet(self.musa, self.jss1a, self.maths)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.code(response), "NOT_YOUR_SHEET")

    def test_the_head_of_the_subjects_department_reads_it(self):
        self.assertEqual(self.sheet(self.okeke, self.jss1a, self.maths).status_code, 403)
        self.set_head(self.sciences, self.okeke_staff)
        self.assertEqual(self.sheet(self.okeke, self.jss1a, self.maths).status_code, 200)

    def test_holders_of_the_hub_key_and_of_enter_any_read_every_sheet_in_their_branches(self):
        for user in (self.admin, self.hub_reader, self.enterer, self.lekki_admin):
            with self.subTest(user=user.email):
                self.assertEqual(self.sheet(user, self.jss1a, self.maths).status_code, 200)

    def test_a_caller_without_a_sheet_key_is_refused(self):
        self.assertEqual(self.sheet(self.nobody, self.jss1a, self.maths).status_code, 403)
        self.assertEqual(self.sheet(self.viewer, self.jss1a, self.maths).status_code, 403)

    def test_another_schools_class_and_a_class_outside_the_callers_branch_are_a_404(self):
        self.assertEqual(self.sheet(self.solo_admin, self.jss1a, self.maths).status_code, 404)
        self.assertEqual(self.sheet(self.lekki_admin, self.jss1c, self.maths).status_code, 404)
        self.assertEqual(self.sheet(self.ikeja_admin, self.jss1a, self.maths).status_code, 404)
        self.assertEqual(self.sheet(self.ikeja_admin, self.jss1c, self.maths).status_code, 200)

    def test_another_schools_subject_and_term_are_a_404(self):
        self.assertEqual(self.sheet(self.admin, self.jss1a, self.solo_maths).status_code, 404)
        response = self.sheet(self.admin, self.jss1a, self.maths, term=self.solo_term)
        self.assertEqual(response.status_code, 404)

    def test_a_subject_the_level_does_not_offer_is_a_404(self):
        self.assertEqual(self.sheet(self.admin, self.sss1a, self.french).status_code, 404)

    def test_an_optional_subject_nobody_takes_is_a_404(self):
        self.assertEqual(self.sheet(self.admin, self.jss1b, self.french).status_code, 404)

    def test_a_class_with_no_branch_at_a_school_with_several_has_no_sheet(self):
        response = self.sheet(self.admin, self.shared, self.maths)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "CLASS_HAS_NO_BRANCH")

    def test_a_class_with_no_branch_at_a_school_with_one_belongs_to_that_branch(self):
        response = self.sheet(self.solo_admin, self.solo_class, self.solo_maths, term=self.solo_term)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_term_of_another_year_is_refused(self):
        from schools.vs_academics.models import AcademicSession, AcademicTerm

        old = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2024/2025", start_date=dt.date(2024, 9, 9),
            end_date=dt.date(2025, 7, 18), status="CLOSED",
        )
        term = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=old, name="First Term", order_index=1,
            start_date=dt.date(2024, 9, 9), end_date=dt.date(2024, 12, 13),
        )
        response = self.sheet(self.admin, self.jss1a, self.maths, term=term)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "INVALID_TERM")

    def test_no_structure_in_use_is_a_409_in_words(self):
        from schools.vs_gradebook.constants import SetupStatus

        self.junior_structure.__class__.all_objects.filter(pk=self.junior_structure.pk).update(
            status=SetupStatus.RETIRED,
        )
        response = self.sheet(self.admin, self.jss1a, self.maths)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "NO_STRUCTURE")


class SheetShapeTests(SheetFixture):
    def test_the_header_of_a_sheet_nobody_has_started(self):
        body = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        self.assertEqual(body["subject"], {"id": self.maths.pk, "name": "Mathematics"})
        self.assertEqual(body["class"], {"id": self.jss1a.pk, "name": "JSS1 A"})
        self.assertEqual(body["term"], {"id": self.first_term.pk, "name": "First Term"})
        self.assertEqual(body["main_teacher"]["name"], "Chidi Eze")
        self.assertEqual(body["main_teacher"]["id"], self.eze_staff.pk)
        self.assertEqual(body["assistants"], ["Sade Bello"])
        self.assertIsNone(body["exam_paper"])
        self.assertEqual(body["status"], SheetStatus.NOT_STARTED)
        self.assertFalse(body["needs_moderation"])
        self.assertFalse(body["is_elective"])
        self.assertIsNone(body["takers"])
        self.assertIsNone(body["roll"])
        self.assertIsNone(body["returned"])
        self.assertIsNone(body["saved_at"])
        self.assertEqual(body["progress"], {"complete": 0, "total": 4})
        self.assertTrue(body["can_enter"])
        self.assertFalse(body["can_submit"])
        self.assertEqual(body["blank_count"], 12)

    def test_the_columns_are_the_structures_components(self):
        body = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        self.assertEqual(
            [(c["short_name"], c["max_score"], c["is_exam"]) for c in body["components"]],
            [("CA1", 20, False), ("CA2", 20, False), ("EXAM", 60, True)],
        )

    def test_the_roll_is_the_class_as_at_today_with_a_suspended_student_on_it(self):
        body = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        self.assertEqual(
            [row["student"]["name"] for row in body["rows"]],
            ["Tunde Bello", "Kelechi Eze", "Chiamaka Nwosu", "Amina Yusuf"],
        )
        first = body["rows"][0]
        self.assertEqual(first["student"]["admission_number"], "BFS/002")
        self.assertEqual(
            first["cells"],
            [{"component": c.pk, "mark": None, "marker": None}
             for c in (self.ca1, self.ca2, self.exam)],
        )
        self.assertFalse(first["complete"])
        self.assertIsNone(first["total"])
        self.assertIsNone(first["grade"])

    def test_a_complete_row_is_worked_out_and_the_percentage_is_a_two_place_string(self):
        self.make_sheet(self.jss1a, self.maths, rows={self.chiamaka: (18, 15, 46)})
        row = self.row_for(self.data(self.sheet(self.eze, self.jss1a, self.maths)), self.chiamaka)
        self.assertEqual(row["total"], 79)
        self.assertEqual(row["obtainable"], 100)
        self.assertEqual(row["percentage"], "79.00")
        self.assertEqual((row["grade"], row["remark"], row["is_fail"]), ("A", "Excellent", False))
        self.assertTrue(row["complete"])
        self.assertIsNone(row["excused_note"])

    def test_an_excused_component_leaves_what_the_student_could_obtain_and_says_so(self):
        self.make_sheet(self.jss1a, self.maths, rows={self.chiamaka: ("EXC", 15, 46)})
        row = self.row_for(self.data(self.sheet(self.eze, self.jss1a, self.maths)), self.chiamaka)
        self.assertEqual((row["total"], row["obtainable"]), (61, 80))
        self.assertEqual(row["percentage"], "76.25")
        self.assertEqual(row["grade"], "A")
        self.assertEqual(row["excused_note"], "61 out of 80 obtainable, graded at 76%")
        self.assertEqual(row["cells"][0]["marker"], "EXC")
        self.assertIsNone(row["cells"][0]["mark"])

    def test_absent_counts_as_zero_and_keeps_its_maximum(self):
        self.make_sheet(self.jss1a, self.maths, rows={self.tunde: ("ABS", 18, 42)})
        row = self.row_for(self.data(self.sheet(self.eze, self.jss1a, self.maths)), self.tunde)
        self.assertEqual((row["total"], row["obtainable"]), (60, 100))
        self.assertEqual(row["percentage"], "60.00")
        self.assertEqual(row["grade"], "B")
        self.assertIsNone(row["excused_note"])

    def test_a_row_with_a_blank_has_no_total_grade_or_remark(self):
        self.make_sheet(self.jss1a, self.maths, rows={self.kelechi: (10, None, 30)})
        body = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        row = self.row_for(body, self.kelechi)
        self.assertEqual(
            (row["total"], row["obtainable"], row["percentage"], row["grade"], row["remark"]),
            (None, None, None, None, None),
        )
        self.assertFalse(row["complete"])
        self.assertEqual(body["blank_count"], 12 - 2)

    def test_a_row_excused_from_everything_has_no_grade_and_a_calm_note(self):
        self.make_sheet(self.jss1a, self.maths, rows={self.amina: ("EXC", "EXC", "EXC")})
        row = self.row_for(self.data(self.sheet(self.eze, self.jss1a, self.maths)), self.amina)
        self.assertTrue(row["complete"])
        self.assertIsNone(row["grade"])
        self.assertIsNone(row["percentage"])
        self.assertEqual(row["excused_note"], "Excused from every component, so not graded.")

    def test_the_class_figures_come_from_the_graded_rows(self):
        self.make_sheet(self.jss1a, self.maths, rows={
            self.chiamaka: (18, 15, 46),   # 79
            self.tunde: (10, 10, 20),      # 40
            self.kelechi: (5, 5, 10),      # 20, a fail
            self.amina: (10, None, 30),    # incomplete, left out
        })
        stats = self.data(self.sheet(self.eze, self.jss1a, self.maths))["stats"]
        self.assertEqual(stats["class_average"], "46.33")
        self.assertEqual((stats["highest"], stats["lowest"]), (79, 20))
        self.assertEqual(stats["pass_rate"], 67)

    def test_marks_of_a_student_no_longer_on_the_roll_are_not_shown_or_counted(self):
        self.make_sheet(self.jss1a, self.maths, rows={self.obinna: (20, 20, 60)})
        body = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        self.assertNotIn("Obinna Okafor", [r["student"]["name"] for r in body["rows"]])
        self.assertEqual(body["blank_count"], 12)
        self.assertIsNone(body["stats"]["highest"])

    def test_an_optional_subject_lists_only_those_who_take_it(self):
        body = self.data(self.sheet(self.admin, self.jss1a, self.french))
        self.assertEqual(
            [row["student"]["name"] for row in body["rows"]], ["Tunde Bello", "Chiamaka Nwosu"],
        )
        self.assertTrue(body["is_elective"])
        self.assertEqual((body["takers"], body["roll"]), (2, 4))
        self.assertIsNone(body["main_teacher"])
        self.assertFalse(body["can_enter"])

    def test_the_exam_paper_is_the_published_slot_inside_the_term(self):
        event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="First term exams",
            event_type=EventType.EXAM_PERIOD, start_date=dt.date(2025, 12, 1),
            end_date=dt.date(2025, 12, 10),
        )
        published = Exam.all_objects.create(
            tenant=self.tenant, calendar_event=event, name="First term",
            status=PublishState.PUBLISHED,
        )
        draft = Exam.all_objects.create(
            tenant=self.tenant, calendar_event=event, name="Draft", status=PublishState.DRAFT,
        )
        ExamSlot.all_objects.create(
            tenant=self.tenant, exam=draft, school_class=self.jss1a, subject=self.maths,
            exam_date=dt.date(2025, 12, 2), sitting="MORNING",
        )
        ExamSlot.all_objects.create(
            tenant=self.tenant, exam=published, school_class=self.jss1a, subject=self.maths,
            exam_date=dt.date(2025, 12, 4), sitting="AFTERNOON",
        )
        ExamSlot.all_objects.create(
            tenant=self.tenant, exam=published, school_class=self.jss1b, subject=self.maths,
            exam_date=dt.date(2025, 12, 3), sitting="MORNING",
        )
        paper = self.data(self.sheet(self.eze, self.jss1a, self.maths))["exam_paper"]
        self.assertEqual(
            paper, {"date": "2025-12-04", "sitting": "AFTERNOON", "sitting_label": "Afternoon"},
        )

    def test_a_sheet_returned_carries_who_when_and_the_note(self):
        self.make_sheet(
            self.jss1a, self.maths, status=SheetStatus.RETURNED, returned_by=self.okeke,
            returned_at=dt.datetime(2025, 10, 10, 9, 0, tzinfo=dt.timezone.utc),
            returned_note="Recheck the examination column.",
        )
        returned = self.data(self.sheet(self.eze, self.jss1a, self.maths))["returned"]
        self.assertEqual(returned["by"], "Ify Okeke")
        self.assertEqual(returned["note"], "Recheck the examination column.")
        self.assertTrue(returned["at"].startswith("2025-10-10T"))

    def test_a_submitted_sheet_is_locked_and_cannot_be_entered_or_submitted(self):
        self.make_sheet(
            self.jss1a, self.maths, status=SheetStatus.SUBMITTED,
            rows={s: (10, 10, 30) for s in (self.chiamaka, self.tunde, self.kelechi, self.amina)},
        )
        body = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        self.assertFalse(body["can_enter"])
        self.assertFalse(body["can_submit"])

    def test_a_filled_sheet_can_be_submitted_by_its_teacher_and_not_by_a_reader(self):
        rows = {s: (10, 10, 30) for s in (self.chiamaka, self.tunde, self.kelechi, self.amina)}
        self.make_sheet(self.jss1a, self.maths, rows=rows)
        mine = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        self.assertTrue(mine["can_submit"])
        self.assertEqual(mine["progress"], {"complete": 4, "total": 4})
        theirs = self.data(self.sheet(self.bello, self.jss1a, self.maths))
        self.assertFalse(theirs["can_enter"])
        self.assertFalse(theirs["can_submit"])

    def test_needs_moderation_before_submission_says_where_the_sheet_will_go(self):
        self.moderate_seniors()
        junior = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        senior = self.data(self.sheet(self.adewale, self.sss1a, self.maths))
        self.assertFalse(junior["needs_moderation"])
        self.assertTrue(senior["needs_moderation"])
        self.assertEqual(senior["main_teacher"]["name"], "Kunle Adewale")

    def test_a_single_branch_school_reads_the_same_shape(self):
        body = self.data(self.sheet(
            self.solo_teacher, self.solo_class, self.solo_maths, term=self.solo_term,
        ))
        self.assertEqual(body["main_teacher"]["name"], "Bola Ade")
        self.assertEqual(len(body["rows"]), 2)
        self.assertTrue(body["can_enter"])
