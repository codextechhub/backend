"""Which sheet fields a role reads and changes, proved on every route that carries them.

The switches are the four of ``gradebook.sheet``: the mark (and its marker, and
the ``cells`` that carry them), the row's worked-out figures, the class
figures, and the return note. Each is switched on the teacher role, which the
main teacher, the head of the department and the class teacher all hold, and
proved on the sheet read, the save, the submit, the decisions and the queue.
"""
from __future__ import annotations

import datetime as dt

from django.urls import reverse

from core.test_utils import TenantAPIClient
from vs_rbac.models import FieldDefinition
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from schools.vs_gradebook.constants import SheetStatus
from schools.vs_gradebook.models import Score, ScoreSheet

from .sheet_base import SheetFixture

RESOURCE = "gradebook.sheet"
MARK = "gradebook.sheet.mark"
ROW_RESULT = "gradebook.sheet.row_result"
CLASS_STATS = "gradebook.sheet.class_stats"
RETURN_NOTE = "gradebook.sheet.return_note"

ROW_FIGURES = (
    "total", "obtainable", "percentage", "grade", "remark", "is_fail", "complete",
    "excused_note",
)
CLASS_FIGURES = ("class_average", "highest", "lowest", "pass_rate")


class _Switches(SheetFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.install_declared = install_declared_fields(RESOURCE)
        cls.moderate_seniors()

    def close(self, *keys, read=False, write=False):
        set_field_access(self.teacher_role, *keys, read=read, write=write)

    def queue(self, user):
        return TenantAPIClient(user).get(reverse("gradebook-moderation"))

    def filled_junior(self, **kwargs):
        return self.make_sheet(
            self.jss1a, self.maths, status=SheetStatus.IN_PROGRESS,
            rows={self.chiamaka: (18, 15, 46), self.tunde: ("EXC", 15, 46)}, **kwargs,
        )

    def submitted_senior(self):
        return self.make_sheet(
            self.sss1a, self.maths, status=SheetStatus.SUBMITTED, needs_moderation=True,
            rows={self.zainab: (18, 15, 46), self.femi: (5, 5, 10)},
        )


class DefaultAccessTests(_Switches):
    def test_the_declaration_matches_what_the_registry_holds(self):
        self.assertEqual(
            set(FieldDefinition.objects.filter(key__startswith=RESOURCE).values_list("key", flat=True)),
            {MARK, ROW_RESULT, CLASS_STATS, RETURN_NOTE},
        )

    def test_the_note_travels_as_note(self):
        self.assertEqual(FieldDefinition.objects.get(key=RETURN_NOTE).api_names, ["note"])

    def test_derived_fields_are_read_only_and_the_others_are_writable(self):
        writable = dict(FieldDefinition.objects.filter(key__startswith=RESOURCE).values_list("key", "writable"))
        self.assertEqual(
            writable, {MARK: True, ROW_RESULT: False, CLASS_STATS: False, RETURN_NOTE: True},
        )

    def test_without_a_switch_everything_is_read_and_written(self):
        self.filled_junior()
        body = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        row = body["rows"][0]
        for name in ROW_FIGURES + ("cells",):
            self.assertIn(name, row)
        self.assertEqual(set(body["stats"]), set(CLASS_FIGURES))
        self.assertEqual(set(body["progress"]), {"complete", "total"})
        saved = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.kelechi, self.ca1, 9)])
        self.assertEqual(saved.status_code, 200)


class MarkSwitchTests(_Switches):
    def test_hiding_the_marks_removes_the_cells_from_the_read(self):
        self.filled_junior()
        self.close(MARK)
        response = self.sheet(self.eze, self.jss1a, self.maths)
        self.assertEqual(response.status_code, 200, response.data)
        for row in self.data(response)["rows"]:
            self.assertNotIn("cells", row)
            self.assertIn("total", row)

    def test_hiding_the_marks_removes_them_from_the_submit_answer_too(self):
        for student in (self.chiamaka, self.tunde, self.kelechi, self.amina):
            self.save(self.eze, self.jss1a, self.maths, self.full_row(student))
        self.close(MARK)
        body = self.data(self.submit(self.eze, self.jss1a, self.maths))
        self.assertEqual(body["next"], "CLASS_TEACHER")
        for row in body["sheet"]["rows"]:
            self.assertNotIn("cells", row)

    def test_a_role_that_may_not_write_marks_is_refused_the_save_and_nothing_is_written(self):
        self.close(MARK, read=True, write=False)
        response = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 9)])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertFalse(Score.objects.exists())
        self.assertFalse(ScoreSheet.objects.exists())

    def test_a_blank_cell_is_a_write_too(self):
        self.filled_junior()
        self.close(MARK, read=True, write=False)
        response = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.chiamaka, self.ca1)])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Score.objects.count(), 6)

    def test_a_role_that_cannot_read_or_write_marks_is_refused_as_well(self):
        self.close(MARK)
        response = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 9)])
        self.assertEqual(response.status_code, 403)

    def test_the_switch_does_not_stop_a_stranger_being_refused_for_the_right_reason(self):
        self.close(MARK, read=True, write=False)
        response = self.save(self.musa, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 9)])
        self.assertEqual(self.code(response), "NOT_MAIN_TEACHER")

    def test_a_role_without_the_switch_on_the_other_role_is_unaffected(self):
        self.close(MARK)
        body = self.data(self.sheet(self.admin, self.jss1a, self.maths))
        self.assertIn("cells", body["rows"][0])


class RowResultSwitchTests(_Switches):
    def test_hiding_the_figures_removes_every_one_of_them_and_the_progress_count(self):
        self.filled_junior()
        self.close(ROW_RESULT)
        for response in (
            self.sheet(self.eze, self.jss1a, self.maths),
            self.save(self.eze, self.jss1a, self.maths, [self.cell(self.kelechi, self.ca1, 9)]),
        ):
            body = self.data(response)
            self.assertEqual(body["progress"], {})
            for row in body["rows"]:
                for name in ROW_FIGURES:
                    self.assertNotIn(name, row)
                self.assertIn("cells", row)
                self.assertIn("student", row)
            self.assertIn("blank_count", body)

    def test_the_figures_are_read_only_so_nobody_is_offered_a_write(self):
        self.assertFalse(FieldDefinition.objects.get(key=ROW_RESULT).writable)


class ClassStatsSwitchTests(_Switches):
    def test_hiding_the_class_figures_removes_them_from_the_sheet(self):
        self.filled_junior()
        self.close(CLASS_STATS)
        body = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        self.assertEqual(body["stats"], {})
        self.assertIn("total", body["rows"][0])

    def test_hiding_the_class_figures_removes_them_from_the_moderation_queue(self):
        self.submitted_senior()
        self.close(CLASS_STATS)
        row = self.data(self.queue(self.okeke))["sheets"][0]
        for name in CLASS_FIGURES:
            self.assertNotIn(name, row)
        self.assertEqual((row["marked"], row["roll"]), (2, 2))
        self.assertIn("can_decide", row)

    def test_the_queue_shows_the_figures_to_a_role_that_has_the_switch(self):
        self.submitted_senior()
        row = self.data(self.queue(self.hub_reader))["sheets"][0]
        for name in CLASS_FIGURES:
            self.assertIn(name, row)


class ReturnNoteSwitchTests(_Switches):
    def returned_sheet(self):
        return self.make_sheet(
            self.sss1a, self.maths, status=SheetStatus.RETURNED, needs_moderation=True,
            returned_by=self.okeke, returned_note="Recheck.",
            returned_at=dt.datetime(2025, 10, 10, 9, 0, tzinfo=dt.timezone.utc),
            rows={self.zainab: (18, 15, 46), self.femi: (5, 5, 10)},
        )

    def test_hiding_the_note_keeps_who_and_when_and_drops_the_note(self):
        self.returned_sheet()
        self.close(RETURN_NOTE)
        returned = self.data(self.sheet(self.adewale, self.sss1a, self.maths))["returned"]
        self.assertEqual(returned["by"], "Ify Okeke")
        self.assertIn("at", returned)
        self.assertNotIn("note", returned)

    def test_a_role_that_cannot_read_the_note_cannot_write_one_either(self):
        self.submitted_senior()
        self.close(RETURN_NOTE)
        response = self.send_back(self.okeke, self.sss1a, self.maths, "Recheck.")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertEqual(ScoreSheet.objects.get().status, SheetStatus.SUBMITTED)

    def test_a_role_that_may_not_write_the_note_is_refused_a_return(self):
        self.submitted_senior()
        self.close(RETURN_NOTE, read=True, write=False)
        response = self.send_back(self.okeke, self.sss1a, self.maths, "Recheck.")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertEqual(ScoreSheet.objects.get().status, SheetStatus.SUBMITTED)

    def test_a_role_that_may_not_write_the_note_can_still_approve(self):
        self.submitted_senior()
        self.close(RETURN_NOTE, read=True, write=False)
        self.assertEqual(self.approve(self.okeke, self.sss1a, self.maths).status_code, 200)

    def test_a_stranger_is_refused_for_the_right_reason_not_the_field(self):
        self.submitted_senior()
        self.close(RETURN_NOTE, read=True, write=False)
        response = self.send_back(self.musa, self.sss1a, self.maths, "Recheck.")
        self.assertEqual(self.code(response), "NOT_YOUR_SHEET")
