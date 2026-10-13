"""Compiling a student, and submitting the class to the principal."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from schools.vs_attendance.services.term_summary import StudentTermCounts, class_term_summary
from schools.vs_gradebook.constants import SheetStatus, StoredStage
from schools.vs_gradebook.models import ClassTerm, StudentTermResult
from vs_audit.models import AuditEvent

from .class_base import ClassFixture

SUMMARY = "schools.vs_gradebook.services.classbook.class_term_summary"


class CompileSecurityTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sheets_in()

    def test_the_class_teacher_reads_and_saves(self):
        self.assertEqual(self.compile_get(self.eze, self.tunde).status_code, 200)
        self.assertEqual(
            self.compile_put(self.eze, self.tunde, {"teacher_comment": "Good."}).status_code, 200,
        )

    def test_a_principal_may_read_the_drawer_but_not_write_it(self):
        self.assertEqual(self.compile_get(self.principal, self.tunde).status_code, 200)
        response = self.compile_put(self.principal, self.tunde, {"teacher_comment": "Good."})
        self.assertEqual(response.status_code, 403)

    def test_a_teacher_who_is_not_the_class_teacher_can_neither_read_nor_write(self):
        for response in (
            self.compile_get(self.bello, self.tunde),
            self.compile_put(self.bello, self.tunde, {"teacher_comment": "Good."}),
        ):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.code(response), "NOT_CLASS_TEACHER")
        self.assertFalse(StudentTermResult.objects.exists())

    def test_a_caller_with_no_class_key_is_refused(self):
        for user in (self.nobody, self.bursar, self.hub_reader):
            with self.subTest(user=user.email):
                self.assertEqual(self.compile_get(user, self.tunde).status_code, 403)

    def test_a_class_outside_the_callers_branch_and_another_schools_class_are_404s(self):
        self.assertEqual(self.compile_get(self.ikeja_principal, self.tunde).status_code, 404)
        response = self.call(
            "get", self.solo_teacher, "gradebook-compile", class_id=self.jss1a.pk,
            student_id=self.tunde.pk, term=self.first_term,
        )
        self.assertEqual(response.status_code, 404)

    def test_a_student_who_is_not_on_the_roll_is_a_404(self):
        self.assertEqual(self.compile_get(self.eze, self.sade).status_code, 404)
        self.assertEqual(
            self.compile_put(self.eze, self.sade, {"teacher_comment": "Hi."}).status_code, 404,
        )
        self.assertEqual(self.compile_get(self.eze, self.obinna).status_code, 404)


class CompileReadTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sheets_in()

    def test_the_drawer_lists_the_subjects_the_student_takes_with_totals_and_grades(self):
        body = self.data(self.compile_get(self.eze, self.chiamaka))
        self.assertEqual(body["student"]["name"], "Chiamaka Nwosu")
        self.assertEqual(
            [(s["subject"]["name"], s["total"], s["grade"], s["state"]) for s in body["subjects"]],
            [("French", 50, "C", "TOTAL"), ("Mathematics", 79, "A", "TOTAL")],
        )
        self.assertEqual(body["average"], "64.50")

    def test_an_optional_subject_the_student_does_not_take_is_not_listed(self):
        body = self.data(self.compile_get(self.eze, self.kelechi))
        self.assertEqual([s["subject"]["name"] for s in body["subjects"]], ["Mathematics"])

    def test_index_of_and_the_neighbours_walk_the_roll(self):
        first = self.data(self.compile_get(self.eze, self.tunde))
        self.assertEqual((first["index"], first["of"]), (1, 4))
        self.assertIsNone(first["previous"])
        self.assertEqual(first["next"], self.kelechi.pk)
        middle = self.data(self.compile_get(self.eze, self.chiamaka))
        self.assertEqual((middle["index"], middle["previous"], middle["next"]), (3, self.kelechi.pk, self.amina.pk))
        last = self.data(self.compile_get(self.eze, self.amina))
        self.assertEqual(last["index"], 4)
        self.assertIsNone(last["next"])

    def test_attendance_comes_from_the_term_summary(self):
        counts = class_term_summary(self.tenant, self.jss1a, self.first_term).students[self.tunde.pk]
        body = self.data(self.compile_get(self.eze, self.tunde))
        self.assertEqual(
            body["attendance"],
            {"opened": counts.opened, "present": counts.present, "absent": counts.absent},
        )

    def test_a_student_the_summary_does_not_name_has_zero_counts(self):
        with mock.patch(SUMMARY, return_value=SimpleNamespace(days_not_taken=0, students={})):
            body = self.data(self.compile_get(self.eze, self.tunde))
        self.assertEqual(body["attendance"], {"opened": 0, "present": 0, "absent": 0})

    def test_the_summary_figures_are_passed_through(self):
        summary = SimpleNamespace(days_not_taken=2, students={
            self.tunde.pk: StudentTermCounts(
                opened=30, present=27, absent=3, late=1, excused=0, not_taken=0,
            ),
        })
        with mock.patch(SUMMARY, return_value=summary):
            body = self.data(self.compile_get(self.eze, self.tunde))
        self.assertEqual(body["attendance"], {"opened": 30, "present": 27, "absent": 3})

    def test_ratings_and_traits_are_there_when_the_school_records_them(self):
        body = self.data(self.compile_get(self.eze, self.tunde))
        self.assertEqual(body["ratings"], {})
        self.assertEqual(body["traits"][0], "Punctuality")
        self.assertEqual(len(body["traits"]), 10)

    def test_ratings_and_traits_are_absent_when_the_school_does_not_record_them(self):
        self.make_sheet(
            self.solo_class, self.solo_maths, status=SheetStatus.SUBMITTED,
            rows={self.solo_pupil: (18, 15, 46), self.solo_pupil_two: (10, 10, 20)},
        )
        body = self.data(self.call(
            "get", self.solo_teacher, "gradebook-compile", class_id=self.solo_class.pk,
            student_id=self.solo_pupil.pk, term=self.solo_term,
        ))
        self.assertNotIn("ratings", body)
        self.assertNotIn("traits", body)
        self.assertIn("teacher_comment", body)

    def test_a_subject_not_in_yet_reads_not_in(self):
        self.make_sheet(
            self.sss1a, self.maths, status=SheetStatus.IN_PROGRESS,
            rows={self.zainab: (1, 1, 1)},
        )
        body = self.data(self.call(
            "get", self.principal, "gradebook-compile", class_id=self.sss1a.pk,
            student_id=self.zainab.pk, term=self.first_term,
        ))
        [maths] = body["subjects"]
        self.assertEqual((maths["state"], maths["total"], maths["grade"]), ("NOT_IN", None, None))
        self.assertIsNone(body["average"])


class CompileWriteTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sheets_in()

    def test_a_save_stores_the_comment_and_creates_the_rows_it_needs(self):
        response = self.compile_put(
            self.eze, self.tunde, {"teacher_comment": "  A joy to teach.  "},
        )
        self.assertEqual(response.status_code, 200, response.data)
        result = StudentTermResult.objects.get()
        self.assertEqual(result.teacher_comment, "A joy to teach.")
        self.assertEqual((result.comment_by, result.student), (self.eze, self.tunde))
        self.assertIsNotNone(result.comment_at)
        self.assertEqual(self.data(response)["teacher_comment"], "A joy to teach.")
        self.assertEqual(ClassTerm.objects.get().stage, None)

    def test_a_comment_is_required(self):
        for body in ({}, {"teacher_comment": ""}, {"teacher_comment": "   "}, {"teacher_comment": None}):
            with self.subTest(body=body):
                response = self.compile_put(self.eze, self.tunde, body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.code(response), "COMMENT_REQUIRED")
        self.assertFalse(StudentTermResult.objects.exists())

    def test_ratings_are_saved_against_the_traits_named(self):
        response = self.compile_put(self.eze, self.tunde, {
            "teacher_comment": "Good.", "ratings": {"Neatness": 4, "Punctuality": 5},
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(StudentTermResult.objects.get().ratings, {"Punctuality": 5, "Neatness": 4})
        self.assertEqual(self.data(response)["ratings"], {"Punctuality": 5, "Neatness": 4})

    def test_a_save_without_ratings_leaves_the_ratings_alone(self):
        self.compile_put(self.eze, self.tunde, {"teacher_comment": "One.", "ratings": {"Sports": 3}})
        self.compile_put(self.eze, self.tunde, {"teacher_comment": "Two."})
        result = StudentTermResult.objects.get()
        self.assertEqual((result.teacher_comment, result.ratings), ("Two.", {"Sports": 3}))

    def test_an_empty_ratings_object_clears_them(self):
        self.compile_put(self.eze, self.tunde, {"teacher_comment": "One.", "ratings": {"Sports": 3}})
        self.compile_put(self.eze, self.tunde, {"teacher_comment": "Two.", "ratings": {}})
        self.assertEqual(StudentTermResult.objects.get().ratings, {})

    def test_a_rating_for_a_trait_the_school_does_not_use_is_refused(self):
        response = self.compile_put(
            self.eze, self.tunde, {"teacher_comment": "Good.", "ratings": {"Cooking": 4}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "INVALID_RATING")
        self.assertIn("Cooking", self.message(response))
        self.assertFalse(StudentTermResult.objects.exists())

    def test_a_rating_outside_one_to_five_or_not_whole_is_refused(self):
        for value in (0, 6, -1, 2.5, "4", True, None):
            with self.subTest(value=value):
                response = self.compile_put(
                    self.eze, self.tunde, {"teacher_comment": "Good.", "ratings": {"Sports": value}},
                )
                self.assertEqual(self.code(response), "INVALID_RATING")

    def test_ratings_must_be_an_object(self):
        response = self.compile_put(
            self.eze, self.tunde, {"teacher_comment": "Good.", "ratings": [1, 2]},
        )
        self.assertEqual(self.code(response), "INVALID_RATING")

    def test_a_school_that_does_not_record_ratings_refuses_any(self):
        self.make_sheet(
            self.solo_class, self.solo_maths, status=SheetStatus.SUBMITTED,
            rows={self.solo_pupil: (18, 15, 46), self.solo_pupil_two: (10, 10, 20)},
        )
        url_kwargs = dict(class_id=self.solo_class.pk, student_id=self.solo_pupil.pk, term=self.solo_term)
        refused = self.call(
            "put", self.solo_teacher, "gradebook-compile",
            {"teacher_comment": "Good.", "ratings": {"Sports": 3}}, **url_kwargs,
        )
        self.assertEqual(self.code(refused), "INVALID_RATING")
        accepted = self.call(
            "put", self.solo_teacher, "gradebook-compile", {"teacher_comment": "Good."},
            **url_kwargs,
        )
        self.assertEqual(accepted.status_code, 200, accepted.data)

    def test_a_save_is_audited(self):
        self.compile_put(self.eze, self.tunde, {"teacher_comment": "Good."})
        event = AuditEvent.objects.get(module_key="GRADEBOOK", entity_type="StudentTermResult")
        self.assertIn("wrote the class teacher's comment for Tunde Bello, JSS1 A, First Term", event.summary)

    def test_a_class_awaiting_approval_or_published_cannot_be_changed(self):
        class_term = ClassTerm.objects.get(school_class=self.jss1a)
        for stage in (StoredStage.AWAITING, StoredStage.PUBLISHED):
            ClassTerm.objects.filter(pk=class_term.pk).update(stage=stage)
            with self.subTest(stage=stage):
                response = self.compile_put(self.eze, self.tunde, {"teacher_comment": "Late."})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.code(response), "CLASS_LOCKED")
        self.assertFalse(StudentTermResult.objects.exists())

    def test_the_drawer_says_whether_it_can_be_edited(self):
        self.assertTrue(self.data(self.compile_get(self.eze, self.tunde))["can_edit"])
        self.assertFalse(self.data(self.compile_get(self.principal, self.tunde))["can_edit"])
        ClassTerm.objects.filter(school_class=self.jss1a).update(stage=StoredStage.AWAITING)
        self.assertFalse(self.data(self.compile_get(self.eze, self.tunde))["can_edit"])


class ClassSubmitTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sheets_in()

    def test_a_class_with_everything_in_goes_to_the_principal(self):
        self.comment_all()
        response = self.submit_class(self.eze)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["stage"], "AWAITING")
        class_term = ClassTerm.objects.get(school_class=self.jss1a)
        self.assertEqual(class_term.stage, StoredStage.AWAITING)
        self.assertEqual(class_term.submitted_by, self.eze)
        self.assertIsNotNone(class_term.submitted_at)

    def test_only_the_class_teacher_can_submit(self):
        self.comment_all()
        for user in (self.bello, self.principal, self.musa):
            with self.subTest(user=user.email):
                response = self.submit_class(user)
                self.assertEqual(response.status_code, 403)
        self.assertIsNone(ClassTerm.objects.get(school_class=self.jss1a).stage)

    def test_a_missing_sheet_blocks_the_submission_and_names_the_subject(self):
        from schools.vs_gradebook.models import ScoreSheet

        ScoreSheet.objects.filter(subject=self.french).update(status=SheetStatus.IN_PROGRESS)
        self.comment_all()
        response = self.submit_class(self.eze)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "SUBJECTS_MISSING")
        self.assertIn("French", self.message(response))
        detail = response.data["error"]["detail"]
        self.assertEqual([s["name"] for s in detail["subjects"]], ["French"])
        self.assertIsNone(ClassTerm.objects.get(school_class=self.jss1a).stage)

    def test_a_missing_comment_blocks_the_submission_and_counts_them(self):
        for student in (self.tunde, self.kelechi):
            self.compile_put(self.eze, student, {"teacher_comment": "Good."})
        response = self.submit_class(self.eze)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "COMMENTS_MISSING")
        self.assertEqual(response.data["error"]["detail"]["count"], 2)

    def test_the_sheets_are_checked_before_the_comments(self):
        from schools.vs_gradebook.models import ScoreSheet

        ScoreSheet.objects.filter(subject=self.french).update(status=SheetStatus.RETURNED)
        self.assertEqual(self.code(self.submit_class(self.eze)), "SUBJECTS_MISSING")

    def test_a_class_with_no_sheets_has_nothing_to_submit(self):
        response = self.submit_class(self.bello, school_class=self.jss1b)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "SUBJECTS_MISSING")

    def test_a_class_already_submitted_cannot_be_submitted_again(self):
        self.comment_all()
        self.submit_class(self.eze)
        response = self.submit_class(self.eze)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "CLASS_LOCKED")

    def test_submitting_clears_the_note_the_class_was_returned_with(self):
        self.comment_all()
        ClassTerm.objects.filter(school_class=self.jss1a).update(
            returned_by=self.principal, returned_note="Fix it.",
        )
        self.submit_class(self.eze)
        class_term = ClassTerm.objects.get(school_class=self.jss1a)
        self.assertEqual((class_term.returned_note, class_term.returned_by), ("", None))

    def test_the_submission_is_audited(self):
        self.comment_all()
        self.submit_class(self.eze)
        event = AuditEvent.objects.get(module_key="GRADEBOOK", entity_type="ClassTerm")
        self.assertIn("submitted JSS1 A, First Term to the principal", event.summary)
