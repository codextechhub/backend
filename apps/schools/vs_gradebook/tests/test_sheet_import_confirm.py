"""Confirming a checked upload: only the chosen clean rows are saved, all or nothing, and never over newer marks."""
from __future__ import annotations

import datetime as dt

from schools.vs_gradebook.constants import SheetStatus, StoredStage
from schools.vs_gradebook.models import ClassTerm, Score, ScoreImport, ScoreSheet
from schools.vs_students.models import ClassEnrolment
from vs_audit.models import AuditEvent

from .import_base import ImportFixture

ROWS = [
    ["BFS/001", "Chiamaka", 18, 16, None],
    ["BFS/002", "Tunde", 10, 12, 50],
    ["BFS/003", "Kelechi", "ABS", "EXC", 30],
    ["BFS/999", "Nobody", 1, 1, 1],
]


class _Confirm(ImportFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.make_sheet(
            cls.jss1a, cls.maths, rows={cls.chiamaka: (18, 15, 46), cls.tunde: (10, None, None)},
        )

    def setUp(self):
        super().setUp()
        self.found = self.preview(self.eze, self.jss1a, self.maths, ROWS)
        self.token = self.found["token"]

    def go(self, rows, user=None, token=None):
        return self.confirm(user or self.eze, self.jss1a, self.maths, token or self.token, rows)

    def stored(self, student, component):
        row = Score.objects.filter(student=student, component=component).first()
        return None if row is None else (row.mark, row.marker)


class ConfirmGuardTests(_Confirm):
    def test_a_user_with_no_import_key_is_refused(self):
        self.assertEqual(self.go([3], user=self.nobody).status_code, 403)

    def test_another_users_token_is_a_404_even_for_someone_who_may_enter_the_sheet(self):
        for user in (self.importer, self.bello):
            with self.subTest(user=user.email):
                self.assertEqual(self.go([3], user=user).status_code, 404)
        self.assertEqual(self.stored(self.tunde, self.ca2), None)

    def test_a_token_of_another_school_is_a_404(self):
        response = self.confirm(
            self.solo_teacher, self.solo_class, self.solo_maths, self.token, [3],
        )
        self.assertEqual(response.status_code, 404)

    def test_a_token_used_on_another_sheet_is_a_404(self):
        response = self.confirm(self.eze, self.jss1b, self.maths, self.token, [3])
        self.assertEqual(response.status_code, 404)

    def test_an_unknown_token_is_a_404(self):
        self.assertEqual(self.go([3], token="nonsense").status_code, 404)

    def test_an_expired_token_is_a_404(self):
        ScoreImport.objects.update(expires_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(self.go([3]).status_code, 404)
        self.assertEqual(self.stored(self.tunde, self.ca2), None)

    def test_a_token_is_used_once(self):
        self.assertEqual(self.go([3]).status_code, 200)
        self.assertEqual(self.go([3]).status_code, 404)

    def test_a_lost_branch_is_a_404_at_confirm_time(self):
        found = self.preview(self.lekki_importer, self.jss1a, self.maths, ROWS)
        response = self.confirm(self.ikeja_importer, self.jss1a, self.maths, found["token"], [3])
        self.assertEqual(response.status_code, 404)


class ConfirmSaveTests(_Confirm):
    def test_it_saves_the_chosen_rows_only(self):
        response = self.go([3, 4])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(self.tunde, self.ca2), (12, None))
        self.assertEqual(self.stored(self.tunde, self.exam), (50, None))
        self.assertEqual(self.stored(self.kelechi, self.ca1), (None, "ABS"))
        self.assertEqual(self.stored(self.kelechi, self.ca2), (None, "EXC"))
        self.assertEqual(self.stored(self.kelechi, self.exam), (30, None))
        self.assertEqual(self.stored(self.chiamaka, self.ca2), (15, None))

    def test_a_blank_cell_in_the_file_leaves_the_mark_on_the_sheet(self):
        self.assertEqual(self.go([2]).status_code, 200)
        self.assertEqual(self.stored(self.chiamaka, self.ca2), (16, None))
        self.assertEqual(self.stored(self.chiamaka, self.exam), (46, None))

    def test_it_answers_with_the_whole_sheet_as_now_saved(self):
        body = self.data(self.go([2, 3]))
        self.assertEqual(body["subject"]["id"], self.maths.pk)
        self.assertEqual(len(body["components"]), 3)
        row = self.row_for(body, self.tunde)
        self.assertEqual([c["mark"] for c in row["cells"]], [10, 12, 50])

    def test_the_marks_record_who_entered_them(self):
        self.go([3])
        self.assertEqual(Score.objects.get(student=self.tunde, component=self.ca2).entered_by, self.eze)

    def test_a_chosen_row_that_changes_nothing_is_not_an_error(self):
        found = self.preview(self.eze, self.jss1a, self.maths, [["BFS/001", "x", 18, 15, 46]])
        self.assertEqual(self.confirm(self.eze, self.jss1a, self.maths, found["token"], [2]).status_code, 200)
        self.assertEqual(Score.objects.count(), 4)

    def test_the_same_row_numbers_in_any_order_or_repeated_are_taken_once(self):
        self.assertEqual(self.go([4, 3, 3]).status_code, 200)
        self.assertEqual(self.stored(self.tunde, self.ca2), (12, None))

    def test_the_save_is_audited_without_the_marks(self):
        self.go([2, 3])
        event = AuditEvent.objects.get(module_key="GRADEBOOK", entity_type="ScoreSheet")
        self.assertEqual(event.actor_user, self.eze)
        self.assertIn("uploaded scores for Mathematics, JSS1 A, First Term", event.summary)
        self.assertIn("replacing", event.summary)
        self.assertEqual(
            {k: event.metadata[k] for k in ("rows", "scores", "replaced")},
            {"rows": 2, "scores": 3, "replaced": 1},
        )

    def test_a_sheet_nobody_has_started_is_started_by_the_first_upload(self):
        found = self.preview(self.adewale, self.sss1a, self.maths, [["BFS/008", "x", 5, 6, 7]])
        response = self.confirm(self.adewale, self.sss1a, self.maths, found["token"], [2])
        self.assertEqual(response.status_code, 200, response.data)
        sheet = ScoreSheet.objects.get(class_term__school_class=self.sss1a)
        self.assertEqual(sheet.status, SheetStatus.IN_PROGRESS)
        self.assertEqual(Score.objects.filter(sheet=sheet).count(), 3)

    def test_an_elective_sheet_saves_for_a_taker(self):
        found = self.preview(self.importer, self.jss1a, self.french, [
            ["BFS/001", "x", 5, 5, 5], ["BFS/003", "x", 5, 5, 5],
        ])
        self.assertEqual(
            self.confirm(self.importer, self.jss1a, self.french, found["token"], [3]).status_code, 422,
        )
        self.assertEqual(
            self.confirm(self.importer, self.jss1a, self.french, found["token"], [2]).status_code, 200,
        )
        self.assertEqual(Score.objects.filter(sheet__subject=self.french).count(), 3)

    def test_a_school_with_one_branch_uploads_and_confirms(self):
        rows = [["SUN/001", "x", 10, 10, 30], ["SUN/002", "x", 5, 5, 5]]
        found = self.preview(
            self.solo_teacher, self.solo_class, self.solo_maths, rows, term=self.solo_term,
        )
        response = self.confirm(
            self.solo_teacher, self.solo_class, self.solo_maths, found["token"], [2, 3],
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Score.all_objects.filter(tenant=self.solo).count(), 6)


class ConfirmRefusalTests(_Confirm):
    def test_a_chosen_row_with_an_issue_is_refused_and_nothing_is_saved(self):
        response = self.go([3, 5])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "ROW_HAS_ISSUES")
        self.assertEqual(self.stored(self.tunde, self.ca2), None)
        self.assertEqual(self.stored(self.kelechi, self.exam), None)

    def test_a_row_that_is_not_in_the_upload_is_refused(self):
        response = self.go([3, 40])
        self.assertEqual(response.data["error"]["code"], "UNKNOWN_UPLOAD_ROW")
        self.assertEqual(self.stored(self.tunde, self.ca2), None)

    def test_choosing_no_rows_is_refused(self):
        self.assertEqual(self.go([]).data["error"]["code"], "NO_ROWS_CHOSEN")

    def test_a_refused_confirm_does_not_use_up_the_token(self):
        self.go([5])
        self.assertEqual(self.go([3]).status_code, 200)

    def test_a_student_who_left_the_roll_since_the_preview_refuses_the_whole_batch(self):
        ClassEnrolment.all_objects.filter(student=self.kelechi).update(
            effective_date=dt.date(2025, 12, 1),
        )
        response = self.go([3, 4])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "CELLS_REFUSED")
        self.assertEqual(self.stored(self.tunde, self.ca2), None)
        self.assertEqual(self.stored(self.kelechi, self.exam), None)

    def test_a_grid_edit_after_the_preview_is_not_written_over(self):
        saved = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca2, 3)])
        self.assertEqual(saved.status_code, 200)
        response = self.go([3])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "SHEET_CHANGED_SINCE_PREVIEW")
        self.assertEqual(self.stored(self.tunde, self.ca2), (3, None))
        self.assertEqual(self.stored(self.kelechi, self.exam), None)

    def test_an_edit_to_a_student_in_no_chosen_row_still_refuses(self):
        self.save(self.eze, self.jss1a, self.maths, [self.cell(self.amina, self.ca1, 1)])
        self.assertEqual(self.go([3]).data["error"]["code"], "SHEET_CHANGED_SINCE_PREVIEW")

    def test_a_cleared_cell_after_the_preview_refuses_too(self):
        self.save(self.eze, self.jss1a, self.maths, [self.cell(self.chiamaka, self.exam)])
        self.assertEqual(self.go([2]).status_code, 409)

    def test_the_sheet_being_started_after_a_preview_of_an_unstarted_one_refuses(self):
        found = self.preview(self.adewale, self.sss1a, self.maths, [["BFS/008", "x", 5, 6, 7]])
        self.save(self.adewale, self.sss1a, self.maths, [self.cell(self.zainab, self.sca1, 9)])
        response = self.confirm(self.adewale, self.sss1a, self.maths, found["token"], [2])
        self.assertEqual(response.data["error"]["code"], "SHEET_CHANGED_SINCE_PREVIEW")

    def test_a_sheet_submitted_after_the_preview_is_refused_as_locked(self):
        ScoreSheet.objects.update(status=SheetStatus.SUBMITTED)
        response = self.go([3])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "SHEET_LOCKED")
        self.assertEqual(self.stored(self.tunde, self.ca2), None)

    def test_a_class_sent_to_the_principal_after_the_preview_is_refused(self):
        ClassTerm.objects.update(stage=StoredStage.AWAITING)
        self.assertEqual(self.go([3]).data["error"]["code"], "CLASS_LOCKED")
