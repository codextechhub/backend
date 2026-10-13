"""Submitting a sheet, and where it goes."""
from __future__ import annotations

from vs_audit.models import AuditEvent

from schools.vs_gradebook.constants import SheetStatus
from schools.vs_gradebook.models import ScoreSheet

from .sheet_base import SheetFixture


class SubmitTests(SheetFixture):
    def fill(self, teacher, school_class, subject, students, *, structure_cells=None):
        for student in students:
            self.save(teacher, school_class, subject, self.full_row(student))

    def fill_junior(self):
        self.fill(self.eze, self.jss1a, self.maths, (self.chiamaka, self.tunde, self.kelechi, self.amina))

    def test_a_blank_cell_prevents_submission_and_the_count_is_given(self):
        self.save(self.eze, self.jss1a, self.maths, self.full_row(self.tunde))
        response = self.submit(self.eze, self.jss1a, self.maths)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "BLANKS_REMAIN")
        self.assertEqual(response.data["error"]["detail"]["blank_count"], 9)
        self.assertIn("9 cells are still blank", response.data["message"])
        self.assertEqual(ScoreSheet.objects.get().status, SheetStatus.IN_PROGRESS)

    def test_one_blank_is_said_in_the_singular(self):
        self.fill_junior()
        self.save(self.eze, self.jss1a, self.maths, [self.cell(self.amina, self.exam)])
        response = self.submit(self.eze, self.jss1a, self.maths)
        self.assertIn("1 cell is still blank", response.data["message"])

    def test_a_sheet_nobody_started_cannot_be_submitted(self):
        response = self.submit(self.eze, self.jss1a, self.maths)
        self.assertEqual(self.code(response), "BLANKS_REMAIN")
        self.assertEqual(response.data["error"]["detail"]["blank_count"], 12)
        self.assertFalse(ScoreSheet.objects.exists())

    def test_absent_and_excused_count_as_filled(self):
        self.fill_junior()
        self.save(self.eze, self.jss1a, self.maths, [
            self.cell(self.amina, self.ca1, marker="ABS"), self.cell(self.amina, self.ca2, marker="EXC"),
        ])
        self.assertEqual(self.submit(self.eze, self.jss1a, self.maths).status_code, 200)

    def test_an_unmoderated_class_goes_straight_to_the_class_teacher(self):
        self.fill_junior()
        response = self.submit(self.eze, self.jss1a, self.maths)
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertEqual(body["next"], "CLASS_TEACHER")
        self.assertEqual(body["sheet"]["status"], SheetStatus.SUBMITTED)
        self.assertFalse(body["sheet"]["needs_moderation"])
        sheet = ScoreSheet.objects.get()
        self.assertEqual((sheet.submitted_by, sheet.needs_moderation), (self.eze, False))
        self.assertIsNotNone(sheet.submitted_at)

    def test_a_moderated_programme_with_a_head_goes_to_moderation(self):
        self.moderate_seniors()
        for student in (self.zainab, self.femi):
            self.save(self.adewale, self.sss1a, self.maths, self.senior_row(student))
        response = self.submit(self.adewale, self.sss1a, self.maths)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["next"], "MODERATION")
        self.assertTrue(ScoreSheet.objects.get().needs_moderation)

    def test_a_moderated_programme_whose_department_has_no_head_is_not_moderated(self):
        self.moderate_seniors(head=False)
        for student in (self.zainab, self.femi):
            self.save(self.adewale, self.sss1a, self.maths, self.senior_row(student))
        self.assertEqual(self.data(self.submit(self.adewale, self.sss1a, self.maths))["next"], "CLASS_TEACHER")

    def test_a_head_who_has_left_does_not_count(self):
        from schools.vs_staff.models import StaffProfile

        self.moderate_seniors()
        StaffProfile.all_objects.filter(pk=self.okeke_staff.pk).update(employment_status="RESIGNED")
        for student in (self.zainab, self.femi):
            self.save(self.adewale, self.sss1a, self.maths, self.senior_row(student))
        self.assertEqual(self.data(self.submit(self.adewale, self.sss1a, self.maths))["next"], "CLASS_TEACHER")

    def test_a_head_without_a_moderated_programme_changes_nothing(self):
        self.set_head(self.sciences, self.okeke_staff)
        self.fill_junior()
        self.assertEqual(self.data(self.submit(self.eze, self.jss1a, self.maths))["next"], "CLASS_TEACHER")

    def test_submitting_clears_a_return_note(self):
        self.fill_junior()
        ScoreSheet.objects.update(
            status=SheetStatus.RETURNED, returned_by=self.okeke, returned_note="Check it.",
        )
        # The sheet exists only once a first write has made it.
        self.assertEqual(ScoreSheet.objects.count(), 1)
        body = self.data(self.submit(self.eze, self.jss1a, self.maths))
        self.assertIsNone(body["sheet"]["returned"])
        sheet = ScoreSheet.objects.get()
        self.assertEqual((sheet.returned_note, sheet.returned_by, sheet.returned_at), ("", None, None))

    def test_a_submitted_sheet_cannot_be_submitted_again(self):
        self.fill_junior()
        self.submit(self.eze, self.jss1a, self.maths)
        again = self.submit(self.eze, self.jss1a, self.maths)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.code(again), "SHEET_LOCKED")

    def test_only_the_main_teacher_or_an_enter_any_holder_submits(self):
        self.fill_junior()
        for user in (self.bello, self.musa):
            self.assertEqual(self.submit(user, self.jss1a, self.maths).status_code, 403)
        for user in (self.nobody, self.admin):
            self.assertEqual(self.submit(user, self.jss1a, self.maths).status_code, 403)
        self.assertEqual(self.submit(self.enterer, self.jss1a, self.maths).status_code, 200)

    def test_another_schools_sheet_is_a_404(self):
        self.assertEqual(self.submit(self.solo_teacher, self.jss1a, self.maths).status_code, 404)

    def test_the_submission_is_audited(self):
        self.fill_junior()
        self.submit(self.eze, self.jss1a, self.maths)
        event = AuditEvent.objects.get(module_key="GRADEBOOK", entity_type="ScoreSheet")
        self.assertEqual(event.actor_user, self.eze)
        self.assertIn("submitted Mathematics, JSS1 A, First Term", event.summary)

    def test_a_single_branch_school_submits_the_same_way(self):
        for pupil in (self.solo_pupil, self.solo_pupil_two):
            self.call(
                "put", self.solo_teacher, "gradebook-sheet-scores",
                {"cells": [
                    {"student": pupil.pk, "component": component.pk, "mark": 5}
                    for component in self.solo_structure.components.all()
                ]},
                term=self.solo_term, class_id=self.solo_class.pk, subject_id=self.solo_maths.pk,
            )
        response = self.call(
            "post", self.solo_teacher, "gradebook-sheet-submit", term=self.solo_term,
            class_id=self.solo_class.pk, subject_id=self.solo_maths.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["next"], "CLASS_TEACHER")
