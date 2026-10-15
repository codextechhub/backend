"""Which release-list, report-card and Results-tab fields a role reads.

The switches are those of ``gradebook.result`` (the overall average, the
position, the ratings, both comments, the guardian, the email state),
``gradebook.sheet`` (the marks, the totals, grades and remarks, the class
figures), ``gradebook.hold`` (the reason and the lift note), the register's
counts and the Student Management guardian switches, proved on every route that
carries them: the release list, one card, a class's print and the Results tab.
A line a card builds from a hidden value disappears with it.
"""
from __future__ import annotations

from unittest import mock

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from schools.vs_gradebook.constants import PERM_RELEASE_VIEW
from schools.vs_gradebook.models import ResultHold

from .base import make_key_role
from .card_base import SEND, CardFixture

AVERAGE = "gradebook.result.overall_average"
POSITION = "gradebook.result.position"
RATINGS = "gradebook.result.ratings"
TEACHER_COMMENT = "gradebook.result.teacher_comment"
PRINCIPAL_COMMENT = "gradebook.result.principal_comment"
GUARDIAN = "gradebook.result.guardian_contact"
EMAIL_STATE = "gradebook.result.email_state"
MARK = "gradebook.sheet.mark"
ROW_RESULT = "gradebook.sheet.row_result"
CLASS_STATS = "gradebook.sheet.class_stats"
REASON = "gradebook.hold.reason"
COUNTS = "attendance.register.counts"
GUARDIAN_EMAIL = "school.guardians.email"
GUARDIAN_NAME_PARTS = (
    "school.guardians.first_name", "school.guardians.middle_name", "school.guardians.last_name",
)


class _Switches(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields(
            "gradebook.result", "gradebook.sheet", "gradebook.hold", "attendance.register",
            "school.guardians",
        )
        cls.publish_jss1a(held=[cls.amina])

    def close(self, role, *keys):
        set_field_access(role, *keys, read=False, write=False)

    def card(self, user=None, student=None):
        response = self.report_card(user or self.releaser, student or self.tunde)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    def release_rows(self, user=None):
        body = self.data(self.release(user or self.releaser))
        return {row["student"]["name"]: row for row in body["rows"]}

    def tab(self, student=None):
        response = self.results_tab(self.result_reader, student or self.tunde)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)


class DefaultAccessTests(_Switches):
    def test_with_no_switch_closed_every_figure_and_line_is_on_the_card(self):
        card = self.card()
        self.assertEqual(
            set(self.lines(card, "totals")),
            {"Total obtained", "Total obtainable", "Overall average", "Position in class"},
        )
        for key in ("ratings", "rating_key", "teacher_comment", "teacher_name",
                    "principal_comment", "principal_name", "attendance", "next_term_line"):
            self.assertIn(key, card)
        maths = next(s for s in card["subjects"] if s["name"] == "Mathematics")
        for key in ("cells", "total", "grade", "remark", "class_average", "highest", "lowest"):
            self.assertIn(key, maths)

    def test_with_no_switch_closed_the_list_and_the_tab_carry_everything(self):
        row = self.release_rows()["Chiamaka Nwosu"]
        for key in ("guardian", "email_state", "emailed_at"):
            self.assertIn(key, row)
        tab = self.tab(self.amina)
        for key in ("guardian", "terms", "history"):
            self.assertIn(key, tab)
        self.assertIn("reason", tab["history"][0])
        self.assertIn("overall_average", tab["terms"][0])
        self.assertIn("position", tab["terms"][0])


class ReportCardSwitchTests(_Switches):
    def test_hiding_the_position_removes_its_line_and_keeps_the_others(self):
        self.close(self.release_role, POSITION)
        totals = self.lines(self.card(), "totals")
        self.assertNotIn("Position in class", totals)
        self.assertIn("Overall average", totals)
        self.assertIn("Total obtained", totals)

    def test_hiding_the_average_removes_its_line_and_keeps_the_position(self):
        self.close(self.release_role, AVERAGE)
        totals = self.lines(self.card(), "totals")
        self.assertNotIn("Overall average", totals)
        self.assertIn("Position in class", totals)

    def test_hiding_the_subject_figures_removes_totals_grades_remarks_and_the_sums(self):
        self.close(self.release_role, ROW_RESULT)
        card = self.card()
        for subject in card["subjects"]:
            for key in ("total", "grade", "remark"):
                self.assertNotIn(key, subject)
            self.assertIn("cells", subject)
        totals = self.lines(card, "totals")
        self.assertNotIn("Total obtained", totals)
        self.assertNotIn("Total obtainable", totals)
        self.assertIn("Overall average", totals)

    def test_hiding_the_marks_removes_the_component_cells_and_nothing_else(self):
        self.close(self.release_role, MARK)
        card = self.card()
        for subject in card["subjects"]:
            self.assertNotIn("cells", subject)
            self.assertIn("total", subject)
        self.assertEqual(len(card["components"]), 3)

    def test_hiding_the_class_figures_removes_average_highest_and_lowest(self):
        self.close(self.release_role, CLASS_STATS)
        for subject in self.card()["subjects"]:
            for key in ("class_average", "highest", "lowest"):
                self.assertNotIn(key, subject)
            self.assertIn("total", subject)

    def test_hiding_the_ratings_removes_them_and_the_key_that_explains_them(self):
        self.close(self.release_role, RATINGS)
        card = self.card()
        self.assertNotIn("ratings", card)
        self.assertNotIn("rating_key", card)
        self.assertIn("teacher_comment", card)

    def test_hiding_the_class_teachers_comment_removes_the_name_that_signs_it(self):
        self.close(self.release_role, TEACHER_COMMENT)
        card = self.card()
        self.assertNotIn("teacher_comment", card)
        self.assertNotIn("teacher_name", card)
        self.assertIn("principal_comment", card)
        self.assertIn("principal_name", card)

    def test_hiding_the_principals_comment_removes_the_name_that_signs_it(self):
        self.close(self.release_role, PRINCIPAL_COMMENT)
        card = self.card()
        self.assertNotIn("principal_comment", card)
        self.assertNotIn("principal_name", card)
        self.assertIn("teacher_comment", card)

    def test_hiding_the_registers_counts_removes_the_attendance_lines(self):
        self.close(self.release_role, COUNTS)
        card = self.card()
        self.assertNotIn("attendance", card)
        self.assertIn("facts", card)

    def test_the_grading_key_and_the_facts_have_no_switch(self):
        self.close(
            self.release_role, AVERAGE, POSITION, RATINGS, TEACHER_COMMENT, PRINCIPAL_COMMENT,
            MARK, ROW_RESULT, CLASS_STATS, COUNTS,
        )
        card = self.card()
        self.assertTrue(card["grading_key"])
        self.assertEqual(self.lines(card, "facts")["Student"], "Tunde Bello")
        self.assertTrue(card["next_term_line"])

    def test_a_role_that_hides_a_field_does_not_change_what_another_role_reads(self):
        other_role = make_key_role(self.tenant, "Other Reader", "other_reader", (PERM_RELEASE_VIEW,))
        other = self.make_user("other@brightfield.test", "Olu", "Other", other_role)
        self.close(self.release_role, POSITION)
        self.assertIn("Position in class", self.lines(self.card(other), "totals"))
        self.assertNotIn("Position in class", self.lines(self.card(self.releaser), "totals"))

    def test_the_class_print_hides_the_same_lines_on_every_card(self):
        self.close(self.release_role, POSITION, RATINGS, MARK)
        body = self.data(self.class_cards(self.releaser))
        self.assertTrue(body["cards"])
        for card in body["cards"]:
            self.assertNotIn("Position in class", self.lines(card, "totals"))
            self.assertNotIn("ratings", card)
            self.assertNotIn("cells", card["subjects"][0])


class ReleaseListSwitchTests(_Switches):
    def test_hiding_the_guardian_contact_removes_the_guardian_entry(self):
        self.close(self.release_role, GUARDIAN)
        for row in self.release_rows().values():
            self.assertNotIn("guardian", row)
            self.assertIn("result_state", row)

    def test_hiding_the_email_state_removes_it_and_when_it_was_sent(self):
        self.close(self.release_role, EMAIL_STATE)
        row = self.release_rows()["Chiamaka Nwosu"]
        self.assertNotIn("email_state", row)
        self.assertNotIn("emailed_at", row)
        self.assertIn("guardian", row)
        self.assertEqual(row["result_state"], "RELEASED")

    def test_the_student_management_email_switch_removes_the_email_alone(self):
        self.close(self.release_role, GUARDIAN_EMAIL)
        guardian = self.release_rows()["Chiamaka Nwosu"]["guardian"]
        self.assertEqual(guardian, {"name": "Mrs. Ngozi Nwosu"})

    def test_the_student_management_name_switches_remove_the_name_alone(self):
        self.close(self.release_role, *GUARDIAN_NAME_PARTS)
        guardian = self.release_rows()["Chiamaka Nwosu"]["guardian"]
        self.assertEqual(guardian, {"email": "ngozi.nwosu@example.ng"})

    def test_with_both_student_management_switches_closed_the_guardian_disappears(self):
        self.close(self.release_role, GUARDIAN_EMAIL, *GUARDIAN_NAME_PARTS)
        row = self.release_rows()["Chiamaka Nwosu"]
        self.assertNotIn("guardian", row)

    def test_the_stricter_of_the_two_rules_wins_when_the_gradebook_switch_is_closed_too(self):
        self.close(self.release_role, GUARDIAN)
        set_field_access(self.release_role, GUARDIAN_EMAIL, read=True, write=True)
        self.assertNotIn("guardian", self.release_rows()["Chiamaka Nwosu"])

    def test_a_student_without_a_primary_guardian_stays_null_when_nothing_is_hidden(self):
        from schools.vs_students.models import StudentGuardian

        StudentGuardian.all_objects.filter(student=self.chiamaka).delete()
        self.assertIsNone(self.release_rows()["Chiamaka Nwosu"]["guardian"])


class ResultsTabSwitchTests(_Switches):
    def test_hiding_the_position_and_the_average_removes_them_from_every_term(self):
        self.close(self.result_role, POSITION, AVERAGE)
        for entry in self.tab()["terms"]:
            self.assertNotIn("position", entry)
            self.assertNotIn("overall_average", entry)
            self.assertIn("release_label", entry)

    def test_hiding_the_subject_figures_removes_totals_grades_and_remarks(self):
        self.close(self.result_role, ROW_RESULT)
        for subject in self.tab()["terms"][0]["subjects"]:
            self.assertEqual(set(subject), {"id", "name"})

    def test_hiding_the_guardian_contact_removes_the_guardian_from_the_tab(self):
        self.close(self.result_role, GUARDIAN)
        self.assertNotIn("guardian", self.tab(self.chiamaka))

    def test_the_student_management_switches_cut_the_guardian_down_on_the_tab(self):
        self.close(self.result_role, GUARDIAN_EMAIL)
        self.assertEqual(self.tab(self.chiamaka)["guardian"], {"name": "Mrs. Ngozi Nwosu"})
        self.close(self.result_role, *GUARDIAN_NAME_PARTS)
        self.assertNotIn("guardian", self.tab(self.chiamaka))

    def test_hiding_the_hold_reason_removes_the_reason_and_the_lift_note_from_the_history(self):
        hold = ResultHold.objects.get(student=self.amina)
        with mock.patch(SEND, return_value=["sent"]):
            self.lift(self.bursar, hold.pk, note="Paid in full.")
        self.close(self.result_role, REASON)
        history = self.tab(self.amina)["history"]
        self.assertEqual([event["kind"] for event in history], ["HOLD_LIFTED", "HOLD_PLACED"])
        for event in history:
            self.assertNotIn("reason", event)
            self.assertNotIn("lift_note", event)
            self.assertTrue(event["text"])
            self.assertTrue(event["by"])

    def test_a_role_that_may_read_the_reason_still_does(self):
        self.close(self.release_role, REASON)
        self.assertIn("reason", self.tab(self.amina)["history"][0])
