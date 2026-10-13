"""Saving scores to a sheet and submitting it."""
from __future__ import annotations

from schools.vs_gradebook.constants import SheetStatus, StoredStage
from schools.vs_gradebook.models import ClassTerm, Score, ScoreSheet

from .sheet_base import SheetFixture


class WhoMayEnterTests(SheetFixture):
    def cells(self):
        return [self.cell(self.chiamaka, self.ca1, 10)]

    def test_the_main_teacher_saves(self):
        response = self.save(self.eze, self.jss1a, self.maths, self.cells())
        self.assertEqual(response.status_code, 200, response.data)

    def test_an_assistant_and_a_stranger_cannot_save_though_one_can_read(self):
        for user in (self.bello, self.musa):
            with self.subTest(user=user.email):
                response = self.save(user, self.jss1a, self.maths, self.cells())
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.code(response), "NOT_MAIN_TEACHER")
        self.assertFalse(Score.objects.exists())
        self.assertFalse(ClassTerm.objects.exists())

    def test_a_caller_without_the_enter_key_is_refused(self):
        for user in (self.nobody, self.admin, self.hub_reader):
            with self.subTest(user=user.email):
                self.assertEqual(
                    self.save(user, self.jss1a, self.maths, self.cells()).status_code, 403,
                )

    def test_the_holder_of_enter_any_saves_any_sheet_in_their_branches(self):
        self.assertEqual(
            self.save(self.enterer, self.jss1a, self.maths, self.cells()).status_code, 200,
        )

    def test_another_schools_sheet_and_one_outside_the_branch_are_a_404(self):
        self.assertEqual(
            self.save(self.solo_teacher, self.jss1a, self.maths, self.cells()).status_code, 404,
        )
        lekki_enterer = self.make_user(
            "lekki-enterer@brightfield.test", "Len", "Enterer", self.enterer_role,
            branch=self.lekki,
        )
        response = self.save(lekki_enterer, self.jss1c, self.maths, [
            self.cell(self.sade, self.ca1, 10),
        ])
        self.assertEqual(response.status_code, 404)


class SavingTests(SheetFixture):
    def test_the_first_mark_creates_the_class_term_fixes_the_structure_and_starts_the_sheet(self):
        response = self.save(
            self.eze, self.jss1a, self.maths, [self.cell(self.chiamaka, self.ca1, 18)],
        )
        self.assertEqual(response.status_code, 200, response.data)
        class_term = ClassTerm.objects.get(school_class=self.jss1a, term=self.first_term)
        self.assertEqual(class_term.structure, self.junior_structure)
        self.assertEqual(class_term.branch, self.lekki)
        self.assertIsNone(class_term.stage)
        sheet = ScoreSheet.objects.get(class_term=class_term, subject=self.maths)
        self.assertEqual(sheet.status, SheetStatus.IN_PROGRESS)
        self.assertIsNotNone(sheet.saved_at)
        score = Score.objects.get(sheet=sheet)
        self.assertEqual((score.mark, score.marker, score.entered_by), (18, None, self.eze))

    def test_the_answer_is_the_whole_sheet_as_just_saved(self):
        body = self.data(self.save(
            self.eze, self.jss1a, self.maths,
            self.full_row(self.chiamaka, 18, 15, 46),
        ))
        row = self.row_for(body, self.chiamaka)
        self.assertEqual(row["total"], 79)
        self.assertEqual(body["status"], SheetStatus.IN_PROGRESS)
        self.assertIsNotNone(body["saved_at"])
        self.assertEqual(body["progress"], {"complete": 1, "total": 4})
        self.assertEqual(body["blank_count"], 9)

    def test_a_save_replaces_a_mark_and_a_blank_deletes_it(self):
        self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 10)])
        self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 12)])
        self.assertEqual(Score.objects.get().mark, 12)
        body = self.data(self.save(
            self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1)],
        ))
        self.assertFalse(Score.objects.exists())
        self.assertIsNone(self.row_for(body, self.tunde)["cells"][0]["mark"])

    def test_absent_and_excused_are_markers_not_marks(self):
        self.save(self.eze, self.jss1a, self.maths, [
            self.cell(self.tunde, self.ca1, marker="ABS"),
            self.cell(self.tunde, self.ca2, marker="EXC"),
            self.cell(self.tunde, self.exam, 40),
        ])
        absent, excused, mark = Score.objects.order_by("component__order")
        self.assertEqual((absent.mark, absent.marker), (None, "ABS"))
        self.assertEqual((excused.mark, excused.marker), (None, "EXC"))
        self.assertEqual((mark.mark, mark.marker), (40, None))

    def test_a_mark_may_replace_a_marker_and_the_reverse(self):
        self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, marker="ABS")])
        self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 7)])
        self.assertEqual((Score.objects.get().mark, Score.objects.get().marker), (7, None))
        self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, marker="EXC")])
        self.assertEqual((Score.objects.get().mark, Score.objects.get().marker), (None, "EXC"))

    def test_a_zero_is_a_mark_and_the_maximum_is_allowed(self):
        self.save(self.eze, self.jss1a, self.maths, [
            self.cell(self.tunde, self.ca1, 0), self.cell(self.tunde, self.exam, 60),
        ])
        self.assertEqual(sorted(Score.objects.values_list("mark", flat=True)), [0, 60])

    def test_a_save_of_only_blanks_on_a_sheet_nobody_started_creates_nothing(self):
        response = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1)])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ClassTerm.objects.exists())
        self.assertFalse(ScoreSheet.objects.exists())

    def test_the_second_sheet_of_a_class_shares_the_structure_fixed_by_the_first(self):
        self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 10)])
        # A new structure becomes the one in force, but the class's columns do not move.
        from schools.vs_gradebook.constants import SetupStatus
        from schools.vs_gradebook.models import AssessmentStructure

        AssessmentStructure.all_objects.filter(pk=self.junior_structure.pk).update(
            status=SetupStatus.RETIRED,
        )
        replacement = self.make_structure(
            self.junior, name="Newer", components=(("Test", "TST", 40, False), ("Exam", "EX", 60, True)),
        )
        body = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        self.assertEqual([c["id"] for c in body["components"]], [
            self.ca1.pk, self.ca2.pk, self.exam.pk,
        ])
        fresh = self.data(self.sheet(self.admin, self.jss1b, self.maths))
        self.assertEqual(
            [c["id"] for c in fresh["components"]],
            [c.pk for c in replacement.components.all()],
        )

    def test_no_structure_in_use_refuses_a_save_and_writes_nothing(self):
        from schools.vs_gradebook.constants import SetupStatus
        from schools.vs_gradebook.models import AssessmentStructure

        AssessmentStructure.all_objects.filter(pk=self.junior_structure.pk).update(
            status=SetupStatus.RETIRED,
        )
        response = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 10)])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "NO_STRUCTURE")

    def test_an_optional_subject_is_saved_by_its_assigned_teacher_for_its_takers(self):
        from schools.vs_staff.constants import TeachingPart
        from schools.vs_staff.models import TeachingAssignment

        TeachingAssignment.all_objects.create(
            tenant=self.tenant, staff=self.bello_staff, school_class=self.jss1a,
            subject=self.french, session=self.year, part=TeachingPart.LEAD,
        )
        response = self.save(self.bello, self.jss1a, self.french, [
            self.cell(self.chiamaka, self.ca1, 12),
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Score.objects.get().student, self.chiamaka)


class RefusedCellTests(SheetFixture):
    def refused(self, cells, *, status=422):
        response = self.save(self.eze, self.jss1a, self.maths, cells)
        self.assertEqual(response.status_code, status, response.data)
        self.assertFalse(Score.objects.exists())
        self.assertFalse(ClassTerm.objects.exists())
        return response.data["error"]

    def test_a_mark_over_the_maximum_names_the_cell_and_the_maximum(self):
        error = self.refused([
            self.cell(self.tunde, self.ca1, 21), self.cell(self.tunde, self.exam, 40),
        ])
        self.assertEqual(error["code"], "CELLS_REFUSED")
        self.assertEqual(error["detail"]["cells"], [{
            "student": self.tunde.pk, "component": self.ca1.pk,
            "code": "OVER_MAX", "message": "Max 20",
        }])

    def test_one_refused_cell_saves_nothing_of_the_batch(self):
        self.refused([self.cell(self.chiamaka, self.exam, 50), self.cell(self.tunde, self.exam, 61)])

    def test_every_refused_cell_is_listed_at_once(self):
        error = self.refused([
            self.cell(self.tunde, self.ca1, 25),
            self.cell(self.kelechi, self.ca2, 21),
            self.cell(self.chiamaka, self.exam, 59),
        ])
        self.assertEqual(
            [(c["student"], c["code"]) for c in error["detail"]["cells"]],
            [(self.tunde.pk, "OVER_MAX"), (self.kelechi.pk, "OVER_MAX")],
        )

    def test_marks_must_be_whole_numbers(self):
        for bad in (12.5, "12", True, [12]):
            with self.subTest(mark=bad):
                error = self.refused([self.cell(self.tunde, self.ca1, bad)])
                self.assertEqual(error["detail"]["cells"][0]["code"], "NOT_A_WHOLE_MARK")

    def test_a_whole_number_sent_with_a_decimal_point_is_accepted(self):
        self.assertEqual(
            self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 12.0)]).status_code,
            200,
        )
        self.assertEqual(Score.objects.get().mark, 12)

    def test_a_negative_mark_is_refused(self):
        error = self.refused([self.cell(self.tunde, self.ca1, -1)])
        self.assertEqual(error["detail"]["cells"][0]["code"], "NEGATIVE_MARK")

    def test_a_mark_and_a_marker_together_are_refused(self):
        error = self.refused([self.cell(self.tunde, self.ca1, 10, "ABS")])
        self.assertEqual(error["detail"]["cells"][0]["code"], "MARK_AND_MARKER")

    def test_an_unknown_marker_is_refused(self):
        error = self.refused([self.cell(self.tunde, self.ca1, marker="N/A")])
        self.assertEqual(error["detail"]["cells"][0]["code"], "INVALID_MARKER")

    def test_a_student_not_on_the_sheet_is_refused(self):
        error = self.refused([
            self.cell(self.obinna, self.ca1, 10),      # joins in November
            self.cell(self.sade, self.ca1, 10),        # another class
            self.cell(self.solo_pupil, self.ca1, 10),  # another school
        ])
        self.assertEqual(
            {c["code"] for c in error["detail"]["cells"]}, {"STUDENT_NOT_ON_SHEET"},
        )
        self.assertEqual(len(error["detail"]["cells"]), 3)

    def test_a_component_of_another_structure_is_refused(self):
        error = self.refused([
            {"student": self.tunde.pk, "component": self.senior_structure.components.first().pk, "mark": 5},
            {"student": self.tunde.pk, "component": 99999999, "mark": 5},
        ])
        self.assertEqual(
            {c["code"] for c in error["detail"]["cells"]}, {"UNKNOWN_COMPONENT"},
        )

    def test_a_cell_named_twice_is_refused(self):
        error = self.refused([self.cell(self.tunde, self.ca1, 5), self.cell(self.tunde, self.ca1, 6)])
        self.assertEqual(error["detail"]["cells"][0]["code"], "DUPLICATE_CELL")

    def test_an_optional_subjects_non_taker_cannot_be_written(self):
        response = self.save(self.admin, self.jss1a, self.french, [])
        self.assertEqual(response.status_code, 403)
        response = self.save(self.enterer, self.jss1a, self.french, [
            self.cell(self.kelechi, self.ca1, 10),
        ])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["detail"]["cells"][0]["code"], "STUDENT_NOT_ON_SHEET")
        self.assertFalse(Score.objects.exists())

    def test_a_malformed_body_is_a_400(self):
        for body in ({}, {"cells": []}, {"cells": [{"component": self.ca1.pk}]}):
            with self.subTest(body=body):
                response = self.call(
                    "put", self.eze, "gradebook-sheet-scores", body,
                    class_id=self.jss1a.pk, subject_id=self.maths.pk,
                )
                self.assertEqual(response.status_code, 400)


class LockTests(SheetFixture):
    def fill_and_submit(self):
        for student in (self.chiamaka, self.tunde, self.kelechi, self.amina):
            self.save(self.eze, self.jss1a, self.maths, self.full_row(student))
        return self.submit(self.eze, self.jss1a, self.maths)

    def test_a_submitted_sheet_takes_no_more_scores(self):
        self.assertEqual(self.fill_and_submit().status_code, 200)
        response = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 1)])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "SHEET_LOCKED")
        self.assertEqual(Score.objects.get(student=self.tunde, component=self.ca1).mark, 15)

    def test_every_sheet_of_a_class_awaiting_the_principal_is_locked(self):
        self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 10)])
        ClassTerm.objects.filter(school_class=self.jss1a).update(stage=StoredStage.AWAITING)
        response = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 11)])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "CLASS_LOCKED")
        self.assertEqual(
            self.submit(self.eze, self.jss1a, self.maths).data["error"]["code"], "CLASS_LOCKED",
        )

    def test_a_published_class_is_locked_too(self):
        self.publish(self.jss1a, self.junior_structure)
        response = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 11)])
        self.assertEqual(self.code(response), "CLASS_LOCKED")

    def test_a_returned_sheet_takes_scores_again(self):
        self.make_sheet(self.jss1a, self.maths, status=SheetStatus.RETURNED)
        response = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 11)])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["status"], SheetStatus.RETURNED)


class StructureAndSizeTests(SheetFixture):
    def test_a_save_read_against_a_structure_the_class_has_since_left_is_refused(self):
        from schools.vs_gradebook.services import sheets

        stale = sheets.load(
            self.tenant, self.eze, self.jss1a.pk, self.maths.pk, self.first_term, rule=sheets.ENTER,
        )
        other = self.make_structure(
            self.junior, name="Other", status="DRAFT",
            components=(("Test", "TST", 40, False), ("Exam", "EX", 60, True)),
        )
        self.make_class_term(self.jss1a, self.first_term, structure=other)
        from schools.vs_gradebook.exceptions import StructureChanged

        with self.assertRaises(StructureChanged):
            sheets.save_scores(stale, [
                {"student": self.tunde.pk, "component": self.ca1.pk, "mark": 5},
            ])
        self.assertFalse(Score.objects.exists())

    def test_a_batch_larger_than_a_sheet_can_hold_is_a_400(self):
        cells = [self.cell(self.tunde, self.ca1, 1)] * 1001
        response = self.save(self.eze, self.jss1a, self.maths, cells)
        self.assertEqual(response.status_code, 400)
