"""Checking an uploaded score sheet: who may, what is said of each row and cell, and that nothing is saved."""
from __future__ import annotations

import datetime as dt

from schools.vs_gradebook.constants import SheetStatus, StoredStage
from schools.vs_gradebook.models import Score, ScoreImport, ScoreSheet

from .import_base import ADMISSION, HEADINGS, ImportFixture, csv_bytes, workbook


class PreviewAccessTests(ImportFixture):
    def post(self, user, school_class=None, subject=None, **kwargs):
        return self.upload(
            user, school_class or self.jss1a, subject or self.maths,
            workbook([["BFS/001", "x", 1, 1, 1]]), **kwargs,
        )

    def test_a_main_teacher_without_the_import_key_is_refused(self):
        self.assertEqual(self.post(self.keyless, self.jss1b).status_code, 403)

    def test_a_teacher_who_is_not_the_main_teacher_is_refused(self):
        for user in (self.bello, self.musa):
            with self.subTest(user=user.email):
                response = self.post(user)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data["error"]["code"], "NOT_MAIN_TEACHER")
        self.assertFalse(ScoreImport.objects.exists())

    def test_enter_any_without_the_import_key_is_refused(self):
        self.assertEqual(self.post(self.enter_only).status_code, 403)

    def test_another_schools_class_is_a_404(self):
        self.assertEqual(self.post(self.eze, self.solo_class, self.solo_maths).status_code, 404)

    def test_a_class_in_another_branch_is_a_404(self):
        self.assertEqual(self.post(self.ikeja_importer).status_code, 404)

    def test_a_subject_the_class_does_not_have_is_a_404(self):
        self.assertEqual(self.post(self.importer, self.sss1a, self.french).status_code, 404)

    def test_a_term_of_another_school_is_a_404(self):
        self.assertEqual(self.post(self.eze, term=self.solo_term).status_code, 404)

    def test_a_submitted_sheet_is_refused(self):
        self.make_sheet(self.jss1a, self.maths, status=SheetStatus.SUBMITTED)
        response = self.post(self.eze)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "SHEET_LOCKED")

    def test_a_class_that_has_gone_to_the_principal_is_refused(self):
        self.make_sheet(self.jss1a, self.maths)
        from schools.vs_gradebook.models import ClassTerm

        ClassTerm.all_objects.filter(school_class=self.jss1a).update(stage=StoredStage.AWAITING)
        response = self.post(self.eze)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CLASS_LOCKED")

    def test_the_sunrise_teacher_may_check_a_sheet_at_a_school_with_one_branch(self):
        response = self.upload(
            self.solo_teacher, self.solo_class, self.solo_maths,
            workbook([["SUN/001", "Ngozi", 10, 10, 30]]),
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["summary"]["ready"], 1)


class PreviewRowTests(ImportFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.make_sheet(
            cls.jss1a, cls.maths, rows={cls.chiamaka: (18, 15, 46), cls.tunde: (10, None, None)},
        )
        cls.rows = [
            ["BFS/001", "Chiamaka", 18, 16, None],
            ["BFS/002", "Tunde", 10, 12, 50],
            ["BFS/003", "Kelechi", "ABS", "EXC", "30"],
            ["BFS/999", "Nobody", 1, 1, 1],
            ["BFS/006", "Sade", 1, 1, 1],
            ["BFS/005", "Obinna", 1, 1, 1],
            ["BFS/004", "Amina", 25, 15.5, "abc"],
        ]

    def setUp(self):
        super().setUp()
        self.found = self.preview(self.eze, self.jss1a, self.maths, self.rows)

    def outcomes(self, number):
        return [c["outcome"] for c in self.row_numbered(self.found, number)["cells"]]

    def test_the_answer_carries_a_token_that_expires_in_a_day(self):
        self.assertTrue(self.found["token"])
        batch = ScoreImport.objects.get(token=self.found["token"])
        self.assertEqual(batch.user, self.eze)
        self.assertAlmostEqual(
            (batch.expires_at - batch.created_at).total_seconds(), 24 * 3600, delta=60,
        )
        self.assertEqual(self.found["file_issues"], [])

    def test_the_rows_keep_the_files_row_numbers(self):
        self.assertEqual([r["row_number"] for r in self.found["rows"]], [2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(self.row_numbered(self.found, 2)["student"]["name"], "Chiamaka Nwosu")

    def test_a_cell_the_same_as_the_sheet_is_same_a_different_one_replaces_and_a_blank_is_blank(self):
        self.assertEqual(self.outcomes(2), ["SAME", "REPLACES", "BLANK"])
        cell = self.cell_of(self.row_numbered(self.found, 2), self.ca2)
        self.assertEqual((cell["value"], cell["existing"]), (16, 15))
        blank = self.cell_of(self.row_numbered(self.found, 2), self.exam)
        self.assertEqual((blank["value"], blank["existing"]), (None, 46))

    def test_a_cell_filling_an_empty_one_is_new(self):
        self.assertEqual(self.outcomes(3), ["SAME", "NEW", "NEW"])
        cell = self.cell_of(self.row_numbered(self.found, 3), self.ca2)
        self.assertEqual((cell["value"], cell["existing"]), (12, None))

    def test_abs_and_exc_are_markers_and_a_numeric_string_is_a_mark(self):
        row = self.row_numbered(self.found, 4)
        self.assertEqual(
            [(c["value"], c["outcome"]) for c in row["cells"]],
            [("ABS", "NEW"), ("EXC", "NEW"), (30, "NEW")],
        )
        self.assertEqual(row["issues"], [])

    def test_an_unknown_admission_number_is_an_issue_with_no_student(self):
        row = self.row_numbered(self.found, 5)
        self.assertIsNone(row["student"])
        self.assertEqual(self.issue_codes(row), ["UNKNOWN_ADMISSION_NUMBER"])
        self.assertEqual(self.outcomes(5), [None, None, None])

    def test_a_student_not_yet_on_the_roll_is_not_on_this_sheet(self):
        row = self.row_numbered(self.found, 7)
        self.assertEqual(self.issue_codes(row), ["NOT_ON_THIS_SHEET"])
        self.assertEqual(row["student"]["name"], "Obinna Okafor")

    def test_a_student_of_another_branch_is_unknown_to_a_teacher_pinned_to_this_one(self):
        row = self.row_numbered(self.found, 6)
        self.assertEqual(self.issue_codes(row), ["UNKNOWN_ADMISSION_NUMBER"])
        self.assertIsNone(row["student"])

    def test_each_cell_problem_is_named_against_its_column(self):
        row = self.row_numbered(self.found, 8)
        by_code = {i["code"]: i for i in row["issues"]}
        self.assertEqual(set(by_code), {"OVER_MAX", "NOT_A_WHOLE_MARK", "UNREADABLE_VALUE"})
        self.assertEqual(by_code["OVER_MAX"]["message"], "Max 20")
        self.assertEqual(by_code["OVER_MAX"]["component"], self.ca1.pk)
        self.assertEqual(by_code["NOT_A_WHOLE_MARK"]["component"], self.ca2.pk)
        self.assertEqual(by_code["UNREADABLE_VALUE"]["component"], self.exam.pk)
        self.assertEqual(self.outcomes(8), [None, None, None])

    def test_the_summary_counts_rows_ready_rows_with_issues_and_replacements(self):
        self.assertEqual(
            self.found["summary"], {"rows": 7, "ready": 3, "with_issues": 4, "replaces": 1},
        )

    def test_nothing_is_saved_by_a_preview(self):
        self.assertEqual(Score.objects.count(), 4)
        self.assertEqual(ScoreSheet.objects.count(), 1)
        sheet = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        self.assertEqual(self.row_for(sheet, self.chiamaka)["cells"][1]["mark"], 15)


class PreviewNegativeAndShapeTests(ImportFixture):
    def test_a_negative_mark_is_refused_in_the_grids_words(self):
        found = self.preview(self.eze, self.jss1a, self.maths, [["BFS/001", "x", -3, 1, 1]])
        self.assertEqual(self.issue_codes(found["rows"][0]), ["NEGATIVE_MARK"])

    def test_a_whole_number_written_as_a_decimal_is_a_whole_mark(self):
        found = self.preview(self.eze, self.jss1a, self.maths, [["BFS/001", "x", 15.0, "16.0", 1]])
        self.assertEqual(found["rows"][0]["issues"], [])

    def test_a_student_named_twice_is_flagged_on_both_rows(self):
        found = self.preview(self.eze, self.jss1a, self.maths, [
            ["BFS/001", "x", 1, 1, 1], ["BFS/002", "x", 1, 1, 1], ["bfs/001", "x", 2, 2, 2],
        ])
        self.assertEqual(self.issue_codes(found["rows"][0]), ["DUPLICATE_ROW"])
        self.assertEqual(self.issue_codes(found["rows"][2]), ["DUPLICATE_ROW"])
        self.assertIn("row 4", found["rows"][0]["issues"][0]["message"])
        self.assertEqual(found["rows"][1]["issues"], [])

    def test_an_admission_number_is_matched_without_regard_to_case_or_spaces(self):
        found = self.preview(self.eze, self.jss1a, self.maths, [[" bfs/002 ", "x", 1, 1, 1]])
        self.assertEqual(found["rows"][0]["student"]["id"], self.tunde.pk)

    def test_a_row_with_no_admission_number_is_an_unknown_one(self):
        found = self.preview(self.eze, self.jss1a, self.maths, [[None, "Someone", 1, 1, 1]])
        self.assertEqual(self.issue_codes(found["rows"][0]), ["UNKNOWN_ADMISSION_NUMBER"])

    def test_blank_rows_are_skipped_and_the_numbers_after_them_stay_true(self):
        found = self.preview(self.eze, self.jss1a, self.maths, [
            [None, None, None, None, None], ["BFS/001", "x", 1, 1, 1],
        ])
        self.assertEqual([r["row_number"] for r in found["rows"]], [3])

    def test_columns_may_be_in_any_order_and_named_by_their_short_name(self):
        found = self.preview(
            self.eze, self.jss1a, self.maths, [[40, 5, "BFS/001", 6]],
            headings=["Examination", "CA1", ADMISSION, "2nd CA"],
        )
        self.assertEqual(
            [(c["component"], c["value"]) for c in found["rows"][0]["cells"]],
            [(self.ca1.pk, 5), (self.ca2.pk, 6), (self.exam.pk, 40)],
        )

    def test_a_csv_is_read_as_well(self):
        response = self.upload(
            self.eze, self.jss1a, self.maths, csv_bytes([["BFS/001", "x", 5, 6, "ABS"]]),
            name="scores.csv", content_type="text/csv",
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = self.data(response)["rows"][0]
        self.assertEqual([c["value"] for c in row["cells"]], [5, 6, "ABS"])

    def test_an_elective_sheet_refuses_a_student_who_does_not_take_it(self):
        found = self.preview(self.importer, self.jss1a, self.french, [
            ["BFS/001", "x", 5, 5, 5], ["BFS/003", "x", 5, 5, 5],
        ])
        self.assertEqual(found["rows"][0]["issues"], [])
        self.assertEqual(self.issue_codes(found["rows"][1]), ["NOT_ON_THIS_SHEET"])
        self.assertIn("optional", found["rows"][1]["issues"][0]["message"])

    def test_a_branch_pinned_caller_cannot_learn_of_a_student_in_another_branch(self):
        school_wide = self.preview(self.importer, self.jss1a, self.maths, [["BFS/006", "x", 1, 1, 1]])
        pinned = self.preview(self.lekki_importer, self.jss1a, self.maths, [["BFS/006", "x", 1, 1, 1]])
        self.assertEqual(self.issue_codes(school_wide["rows"][0]), ["NOT_ON_THIS_SHEET"])
        self.assertEqual(self.issue_codes(pinned["rows"][0]), ["UNKNOWN_ADMISSION_NUMBER"])
        self.assertIsNone(pinned["rows"][0]["student"])

    def test_a_student_from_another_school_is_unknown(self):
        found = self.preview(self.eze, self.jss1a, self.maths, [["SUN/001", "x", 1, 1, 1]])
        self.assertEqual(self.issue_codes(found["rows"][0]), ["UNKNOWN_ADMISSION_NUMBER"])

    def test_a_preview_of_the_term_asked_for_uses_that_term(self):
        found = self.preview(
            self.importer, self.jss1a, self.maths, [["BFS/001", "x", 1, 1, 1]], term=self.second_term,
        )
        self.assertEqual(found["summary"]["ready"], 1)
        self.assertEqual(ScoreImport.objects.get(token=found["token"]).term, self.second_term)

    def test_old_uploads_are_removed_when_a_new_one_is_made(self):
        old = self.preview(self.eze, self.jss1a, self.maths, [["BFS/001", "x", 1, 1, 1]])
        ScoreImport.objects.filter(token=old["token"]).update(
            expires_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
        )
        self.preview(self.eze, self.jss1a, self.maths, [["BFS/001", "x", 1, 1, 1]])
        self.assertFalse(ScoreImport.objects.filter(token=old["token"]).exists())


class FileProblemTests(ImportFixture):
    def post(self, content, **kwargs):
        return self.upload(self.eze, self.jss1a, self.maths, content, **kwargs)

    def file_issues(self, headings, rows=()):
        response = self.post(workbook(rows, headings))
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertIsNone(body["token"])
        self.assertEqual(body["rows"], [])
        self.assertFalse(ScoreImport.objects.exists())
        return body["file_issues"]

    def test_a_missing_component_column_is_a_file_issue(self):
        issues = self.file_issues([ADMISSION, "1st CA (max 20)", "2nd CA (max 20)"])
        self.assertEqual([i["code"] for i in issues], ["MISSING_COLUMN"])
        self.assertEqual(issues[0]["component"], self.exam.pk)

    def test_a_missing_admission_column_is_a_file_issue(self):
        issues = self.file_issues(HEADINGS[1:])
        self.assertEqual([i["code"] for i in issues], ["MISSING_COLUMN"])
        self.assertNotIn("component", issues[0])

    def test_a_column_the_sheet_does_not_have_is_a_file_issue(self):
        issues = self.file_issues(HEADINGS + ["Project (max 10)"])
        self.assertEqual([i["code"] for i in issues], ["UNKNOWN_COLUMN"])

    def test_a_column_made_for_a_different_maximum_is_a_file_issue(self):
        issues = self.file_issues(HEADINGS[:2] + ["1st CA (max 30)", "2nd CA (max 20)", "Examination (max 60)"])
        self.assertEqual([i["code"] for i in issues], ["UNKNOWN_COLUMN"])
        self.assertEqual(issues[0]["component"], self.ca1.pk)

    def test_a_column_with_no_heading_but_values_is_a_file_issue(self):
        issues = self.file_issues(HEADINGS + [None], [["BFS/001", "x", 1, 1, 1, "oops"]])
        self.assertEqual([i["code"] for i in issues], ["UNKNOWN_COLUMN"])

    def test_a_file_of_the_wrong_type_is_refused(self):
        for name in ("scores.pdf", "scores.xls", "scores"):
            with self.subTest(name=name):
                response = self.post(b"data", name=name)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.data["error"]["code"], "UNSUPPORTED_FILE_TYPE")

    def test_text_named_as_a_workbook_is_unreadable(self):
        response = self.post(b"not a workbook at all", name="scores.xlsx")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "UNREADABLE_FILE")

    def test_a_damaged_workbook_is_unreadable(self):
        response = self.post(workbook([])[:200], name="scores.xlsx")
        self.assertEqual(response.data["error"]["code"], "UNREADABLE_FILE")

    def test_an_empty_file_and_a_file_of_only_blanks_are_unreadable(self):
        self.assertEqual(self.post(b"", name="a.csv").status_code, 400)
        response = self.post(b"\n\n,,\n", name="a.csv")
        self.assertEqual(response.data["error"]["code"], "UNREADABLE_FILE")

    def test_an_oversize_file_is_refused(self):
        response = self.post(b"x" * (2 * 1024 * 1024 + 10), name="scores.csv")
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.data["error"]["code"], "FILE_TOO_LARGE")

    def test_a_file_with_more_rows_than_a_class_has_is_refused(self):
        rows = [["BFS/001", "x", 1, 1, 1]] * 1001
        response = self.post(csv_bytes(rows), name="scores.csv")
        self.assertEqual(response.status_code, 413)

    def test_no_file_is_a_validation_error(self):
        from core.test_utils import TenantAPIClient

        response = TenantAPIClient(self.eze).post(
            self._url("gradebook-sheet-import", class_id=self.jss1a.pk, subject_id=self.maths.pk),
            {}, format="multipart",
        )
        self.assertEqual(response.status_code, 400)
