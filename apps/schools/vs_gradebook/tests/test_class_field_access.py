"""Which compile, broadsheet, principal-comment and hold fields a role reads and changes.

The switches are those of ``gradebook.result`` (the average, the position, the
ratings and traits, the class teacher's comment, the principal's comment),
``gradebook.sheet`` (the totals and grades, the return note) and
``gradebook.hold`` (the reason, the lift note, the balance), proved on every
route that carries them. The attendance block of the drawer and the warning of
the broadsheet follow the register's own switches.
"""
from __future__ import annotations

import datetime as dt
from vs_rbac.models import FieldDefinition
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from schools.vs_gradebook.models import ClassTerm, PrincipalComment, ResultHold, StudentTermResult

from .class_base import ClassFixture

AVERAGE = "gradebook.result.overall_average"
POSITION = "gradebook.result.position"
RATINGS = "gradebook.result.ratings"
TEACHER_COMMENT = "gradebook.result.teacher_comment"
PRINCIPAL_COMMENT = "gradebook.result.principal_comment"
ROW_RESULT = "gradebook.sheet.row_result"
RETURN_NOTE = "gradebook.sheet.return_note"
REASON = "gradebook.hold.reason"
BALANCE = "gradebook.hold.balance"
TEMPLATES = "gradebook.setup.remark_templates"
COUNTS = "attendance.register.counts"
STATUS = "attendance.register.status"


class _Switches(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields(
            "gradebook.result", "gradebook.sheet", "gradebook.hold", "gradebook.setup",
            "attendance.register",
        )
        cls.sheets_in()

    def close(self, role, *keys, read=False, write=False):
        set_field_access(role, *keys, read=read, write=write)


class ResultSwitchTests(_Switches):
    def test_without_a_switch_everything_is_read_and_written(self):
        row = self.row_of(self.data(self.broadsheet(self.eze)), self.tunde)
        for name in ("average", "position", "position_of", "comment_done"):
            self.assertIn(name, row)
        drawer = self.data(self.compile_get(self.eze, self.tunde))
        for name in ("average", "ratings", "traits", "teacher_comment", "attendance"):
            self.assertIn(name, drawer)
        saved = self.compile_put(self.eze, self.tunde, {"teacher_comment": "Good.", "ratings": {"Sports": 4}})
        self.assertEqual(saved.status_code, 200, saved.data)

    def test_hiding_the_average_removes_it_from_the_broadsheet_and_the_drawer(self):
        self.close(self.teacher_role, AVERAGE)
        row = self.row_of(self.data(self.broadsheet(self.eze)), self.tunde)
        self.assertNotIn("average", row)
        self.assertIn("position", row)
        drawer = self.data(self.compile_get(self.eze, self.tunde))
        self.assertNotIn("average", drawer)
        self.assertIn("teacher_comment", drawer)

    def test_the_position_has_a_switch_of_its_own_apart_from_the_average(self):
        self.close(self.teacher_role, POSITION)
        row = self.row_of(self.data(self.broadsheet(self.eze)), self.tunde)
        self.assertNotIn("position", row)
        self.assertIn("average", row)
        self.assertIn("position_of", row)

    def test_hiding_the_class_teachers_comment_hides_it_and_whether_it_is_written(self):
        self.compile_put(self.eze, self.tunde, {"teacher_comment": "Good."})
        self.close(self.teacher_role, TEACHER_COMMENT)
        row = self.row_of(self.data(self.broadsheet(self.eze)), self.tunde)
        self.assertNotIn("comment_done", row)
        drawer = self.data(self.compile_get(self.eze, self.tunde))
        self.assertNotIn("teacher_comment", drawer)
        self.assertIn("ratings", drawer)

    def test_hiding_the_ratings_hides_the_traits_with_them(self):
        self.close(self.teacher_role, RATINGS)
        drawer = self.data(self.compile_get(self.eze, self.tunde))
        self.assertNotIn("ratings", drawer)
        self.assertNotIn("traits", drawer)
        self.assertIn("teacher_comment", drawer)

    def test_the_principal_reads_what_their_own_role_lets_them_read(self):
        self.close(self.teacher_role, AVERAGE)
        row = self.row_of(self.data(self.broadsheet(self.principal)), self.tunde)
        self.assertIn("average", row)
        self.assertNotIn("average", self.row_of(self.data(self.broadsheet(self.eze)), self.tunde))

    def test_a_role_that_may_not_write_the_comment_is_refused_the_save_and_nothing_is_written(self):
        self.close(self.teacher_role, TEACHER_COMMENT, read=True, write=False)
        response = self.compile_put(self.eze, self.tunde, {"teacher_comment": "Good."})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertFalse(StudentTermResult.objects.exists())

    def test_a_role_that_cannot_read_or_write_the_comment_is_refused_as_well(self):
        self.close(self.teacher_role, TEACHER_COMMENT)
        response = self.compile_put(self.eze, self.tunde, {"teacher_comment": "Good."})
        self.assertEqual(response.status_code, 403)

    def test_a_role_that_may_write_the_comment_but_not_ratings_is_refused_only_when_it_sends_them(self):
        self.close(self.teacher_role, RATINGS, read=True, write=False)
        self.assertEqual(
            self.compile_put(self.eze, self.tunde, {"teacher_comment": "Good."}).status_code, 200,
        )
        refused = self.compile_put(
            self.eze, self.tunde, {"teacher_comment": "Good.", "ratings": {"Sports": 4}},
        )
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(StudentTermResult.objects.get().ratings, {})

    def test_an_empty_ratings_object_is_a_write_too(self):
        self.close(self.teacher_role, RATINGS, read=True, write=False)
        response = self.compile_put(self.eze, self.tunde, {"teacher_comment": "Good.", "ratings": {}})
        self.assertEqual(response.status_code, 403)

    def test_the_switch_does_not_stop_a_stranger_being_refused_for_the_right_reason(self):
        self.close(self.teacher_role, TEACHER_COMMENT, read=True, write=False)
        response = self.compile_put(self.bello, self.tunde, {"teacher_comment": "Good."})
        self.assertEqual(self.code(response), "NOT_CLASS_TEACHER")


class FigureSwitchTests(_Switches):
    def test_hiding_the_sheets_figures_removes_totals_and_grades_but_not_the_states(self):
        self.close(self.teacher_role, ROW_RESULT)
        row = self.row_of(self.data(self.broadsheet(self.eze)), self.tunde)
        for cell in row["cells"]:
            self.assertNotIn("total", cell)
            self.assertEqual(cell["state"], "TOTAL")
        drawer = self.data(self.compile_get(self.eze, self.tunde))
        for subject in drawer["subjects"]:
            self.assertNotIn("total", subject)
            self.assertNotIn("grade", subject)
            self.assertEqual(subject["state"], "TOTAL")
        self.assertIn("average", row)

    def test_the_returned_note_follows_the_return_note_switch_on_the_broadsheet(self):
        ClassTerm.objects.filter(school_class=self.jss1a).update(
            returned_by=self.principal, returned_note="Recheck.",
            returned_at=dt.datetime(2025, 10, 10, 9, 0, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(self.data(self.broadsheet(self.eze))["returned"]["note"], "Recheck.")
        self.close(self.teacher_role, RETURN_NOTE)
        returned = self.data(self.broadsheet(self.eze))["returned"]
        self.assertNotIn("note", returned)
        self.assertEqual(returned["by"], "Pauline Principal")

    def test_a_role_that_may_not_write_the_return_note_cannot_return_a_class(self):
        self.comment_all()
        self.submit_class(self.eze)
        self.close(self.principal_role, RETURN_NOTE, read=True, write=False)
        response = self.return_class(self.principal, "Recheck it.")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, "AWAITING")


class PrincipalCommentSwitchTests(_Switches):
    def test_hiding_the_principals_comment_removes_the_override_and_keeps_the_template(self):
        PrincipalComment.objects.create(
            tenant=self.tenant, class_term=ClassTerm.objects.get(school_class=self.jss1a),
            band_min=70, text="Outstanding.", written_by=self.principal,
        )
        self.assertEqual(
            self.data(self.principal_comments(self.principal))["bands"][0]["override"]["text"],
            "Outstanding.",
        )
        self.close(self.principal_role, PRINCIPAL_COMMENT)
        for band in self.data(self.principal_comments(self.principal))["bands"]:
            self.assertNotIn("override", band)
            self.assertIn("template", band)

    def test_the_remark_template_follows_its_own_setup_switch(self):
        self.close(self.principal_role, TEMPLATES)
        for band in self.data(self.principal_comments(self.principal))["bands"]:
            self.assertNotIn("template", band)
            self.assertIn("override", band)

    def test_a_role_that_may_not_write_the_comment_is_refused_the_save(self):
        self.close(self.principal_role, PRINCIPAL_COMMENT, read=True, write=False)
        response = self.write_principal_comments(self.principal, [{"band_min": 70, "text": "x"}])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertFalse(PrincipalComment.objects.exists())

    def test_a_role_with_neither_read_nor_write_is_refused_the_save_too(self):
        self.close(self.principal_role, PRINCIPAL_COMMENT)
        response = self.write_principal_comments(self.principal, [{"band_min": 70, "text": "x"}])
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PrincipalComment.objects.exists())


class AttendanceSwitchTests(_Switches):
    def test_the_drawers_attendance_follows_the_register_counts_switch(self):
        self.assertIn("attendance", self.data(self.compile_get(self.eze, self.tunde)))
        self.close(self.teacher_role, COUNTS)
        self.assertNotIn("attendance", self.data(self.compile_get(self.eze, self.tunde)))

    def test_the_broadsheets_warning_follows_the_register_status_switch(self):
        self.assertIsNotNone(self.data(self.broadsheet(self.eze))["attendance_warning"])
        self.close(self.teacher_role, STATUS)
        self.assertNotIn("attendance_warning", self.data(self.broadsheet(self.eze)))


class HoldSwitchTests(_Switches):
    def setUp(self):
        super().setUp()
        self.finance((self.tunde, 8_500_000, dt.date(2025, 9, 20)))

    def test_the_balance_is_closed_to_a_role_until_it_is_given_read(self):
        response = self.holds(self.bursar)
        row = self.data(response)[0]
        for name in ("balance", "oldest_unpaid_on", "days_owed"):
            self.assertNotIn(name, row)
        self.assertNotIn("as_of", response.data)
        self.assertNotIn("finance_available", response.data)
        self.close(self.bursar_role, BALANCE, read=True, write=False)
        opened = self.holds(self.bursar)
        row = next(r for r in self.data(opened) if r["student"]["id"] == self.tunde.pk)
        self.assertEqual(row["balance"], 8_500_000)
        self.assertIn("as_of", opened.data)
        self.assertTrue(opened.data["finance_available"])

    def test_hiding_the_reason_removes_it_from_the_hold_and_refuses_a_hold_without_write(self):
        self.close(self.bursar_role, BALANCE, read=True, write=False)
        self.close(self.bursar_role, REASON, read=True, write=True)
        placed = self.hold(self.bursar, self.tunde)
        self.assertEqual(placed.status_code, 201, placed.data)
        self.assertIn("reason", self.data(placed)["hold"])
        self.close(self.bursar_role, REASON, read=False, write=False)
        listed = self.holds(self.bursar)
        row = next(r for r in self.data(listed) if r["student"]["id"] == self.tunde.pk)
        self.assertNotIn("reason", row["hold"])
        self.assertIn("placed_by", row["hold"])

    def test_a_role_that_may_not_write_the_reason_cannot_place_a_hold(self):
        self.close(self.bursar_role, REASON, read=True, write=False)
        response = self.hold(self.bursar, self.tunde)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertFalse(ResultHold.objects.exists())

    def test_a_role_that_may_not_write_the_lift_note_cannot_send_one_but_can_lift_without(self):
        placed = self.hold(self.bursar, self.tunde)
        hold_id = placed.data["data"]["hold"]["id"]
        self.close(self.bursar_role, REASON, read=True, write=False)
        refused = self.lift(self.bursar, hold_id, note="Paid.")
        self.assertEqual(refused.status_code, 403)
        self.assertIsNone(ResultHold.objects.get().lifted_at)
        self.assertEqual(self.lift(self.bursar, hold_id).status_code, 200)

    def test_the_lift_answer_loses_the_note_and_the_reason_when_unreadable(self):
        placed = self.hold(self.bursar, self.tunde)
        hold_id = placed.data["data"]["hold"]["id"]
        self.close(self.bursar_role, REASON)
        response = self.lift(self.bursar, hold_id)
        self.assertEqual(response.status_code, 200, response.data)
        hold = self.data(response)["hold"]
        self.assertNotIn("reason", hold)
        self.assertNotIn("lift_note", hold)
        self.assertIn("lifted_by", hold)

    def test_a_balance_hidden_row_still_shows_who_holds_and_whether_released(self):
        self.hold(self.bursar, self.tunde)
        row = next(r for r in self.data(self.holds(self.bursar)) if r["student"]["id"] == self.tunde.pk)
        self.assertIsNotNone(row["hold"])
        self.assertIn("released", row)

    def test_the_fields_are_declared_as_the_design_table_says(self):
        keys = set(FieldDefinition.objects.filter(key__startswith="gradebook.").values_list("key", flat=True))
        for key in (AVERAGE, POSITION, RATINGS, TEACHER_COMMENT, PRINCIPAL_COMMENT, REASON, BALANCE):
            self.assertIn(key, keys)
        names = {
            f.key: f.api_names for f in FieldDefinition.objects.filter(key__startswith="gradebook.")
        }
        self.assertEqual(
            names[AVERAGE],
            ["overall_average", "average", "annual_average", "from", "to", "change"],
        )
        self.assertEqual(names[RATINGS], ["ratings", "traits"])
        self.assertEqual(names[TEACHER_COMMENT], ["teacher_comment", "comment_done", "teacher_name"])
        self.assertEqual(
            names[PRINCIPAL_COMMENT],
            ["principal_comment", "comments", "override", "principal_name"],
        )
        self.assertEqual(names[BALANCE], ["balance", "oldest_unpaid_on", "days_owed"])
