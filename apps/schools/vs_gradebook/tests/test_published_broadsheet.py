"""The broadsheet of a published class reads the snapshots the cards were issued from.

Before publication a broadsheet is worked out from the sheets. After it, the
sheets are locked but an approved correction changes one student's snapshot and
not the live figures, so a broadsheet worked out from the sheets would show
positions and averages that disagree with the cards the families hold. Once a
class is published every row is the student's latest snapshot.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import mock

from schools.vs_attendance.services.term_summary import class_term_summary
from schools.vs_gradebook.services.reportcards import position_label

from .amend_base import AmendmentFixture
from .cumulative_base import CumulativeFixture


class PublishedBroadsheetTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def body(self):
        response = self.broadsheet(self.principal)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    def assert_every_row_equals_its_card(self):
        body = self.body()
        names = {s["id"]: s["name"] for s in body["subjects"]}
        for student in (self.tunde, self.chiamaka, self.kelechi, self.amina):
            with self.subTest(student=student.first_name):
                card = self.card(student)
                row = self.row_of(body, student)
                self.assertEqual(
                    {names[c["subject"]]: c["total"] for c in row["cells"] if c["state"] == "TOTAL"},
                    {s["name"]: s["total"] for s in card["subjects"]},
                )
                lines = self.lines(card, "totals")
                self.assertEqual(f"{row['average']}%", lines["Overall average"])
                self.assertEqual(
                    position_label(row["position"], row["position_of"]),
                    lines["Position in class"],
                )

    def test_every_row_equals_the_students_card_as_published(self):
        self.assert_every_row_equals_its_card()

    def test_after_an_approved_amendment_every_row_still_equals_the_students_card(self):
        found = self.raised(self.kelechi)
        self.assertEqual(self.approve_amendment(self.approver, found).status_code, 200)
        self.assert_every_row_equals_its_card()

    def test_a_published_class_has_no_attendance_warning_though_days_had_no_register(self):
        live = class_term_summary(self.tenant, self.jss1a, self.first_term).days_not_taken
        self.assertGreater(live, 0)
        self.assertIsNone(self.body()["attendance_warning"])

    def test_the_warning_is_null_whatever_the_register_summary_says_once_published(self):
        summary = SimpleNamespace(days_not_taken=7, students={})
        with mock.patch(
            "schools.vs_gradebook.services.classbook.class_term_summary", return_value=summary,
        ):
            self.assertIsNone(self.body()["attendance_warning"])

    def test_the_amended_students_row_shows_the_corrected_figures_not_the_sheets(self):
        found = self.raised(self.kelechi)
        self.approve_amendment(self.approver, found)
        row = self.row_of(self.body(), self.kelechi)
        self.assertEqual(row["average"], "70.00")
        self.assertEqual(row["position"], 2)
        self.assertEqual(row["position_of"], 4)
        total = next(c for c in row["cells"] if c["state"] == "TOTAL")
        self.assertEqual(total["total"], 70)

    def test_a_classmate_keeps_the_position_published_with_their_card(self):
        found = self.raised(self.kelechi)
        self.approve_amendment(self.approver, found)
        row = self.row_of(self.body(), self.chiamaka)
        self.assertEqual((row["average"], row["position"]), ("64.50", 2))

    def test_a_held_student_is_a_row_with_their_snapshot_figures(self):
        row = self.row_of(self.body(), self.amina)
        self.assertEqual((row["average"], row["position"]), ("60.00", 3))

    def test_the_rows_are_in_name_order_and_the_class_cannot_be_submitted_again(self):
        body = self.body()
        self.assertEqual(
            [r["student"]["name"] for r in body["rows"]],
            ["Tunde Bello", "Kelechi Eze", "Chiamaka Nwosu", "Amina Yusuf"],
        )
        self.assertEqual(body["stage"], "PUBLISHED")
        self.assertFalse(body["can_submit"])
        self.assertTrue(body["ranks"])

    def test_an_optional_subject_a_student_does_not_take_is_not_taken(self):
        body = self.body()
        french = next(s for s in body["subjects"] if s["name"] == "French")
        cell = next(c for c in self.row_of(body, self.kelechi)["cells"] if c["subject"] == french["id"])
        self.assertEqual((cell["state"], cell["total"]), ("NOT_TAKEN", None))

    def test_the_comment_flag_is_the_comment_the_card_was_issued_with(self):
        for row in self.body()["rows"]:
            self.assertTrue(row["comment_done"])


class PublishedBroadsheetBlockersTests(AmendmentFixture):
    """Nothing is submittable after publication, so a late joiner blocks nothing."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()
        cls.late = cls.make_student(
            "Zainab", "Lawal", cls.jss1a, "LKI/2025/090", effective=dt.date(2025, 10, 13),
        )

    def test_a_pupil_who_joined_after_publication_is_not_a_missing_comment(self):
        body = self.data(self.broadsheet(self.principal))
        self.assertEqual(body["stage"], "PUBLISHED")
        self.assertEqual(body["submit_blockers"], {"subjects_missing": [], "comments_missing": 0})
        self.assertEqual(body["gaps"], [])
        self.assertFalse(body["can_submit"])
        self.assertNotIn(self.late.pk, [r["student"]["id"] for r in body["rows"]])

    def test_the_class_teacher_is_not_offered_a_submit_either(self):
        body = self.data(self.broadsheet(self.eze))
        self.assertFalse(body["can_submit"])
        self.assertEqual(body["submit_blockers"]["comments_missing"], 0)


class PublishedBroadsheetOfASchoolThatDoesNotRankTests(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_sunrise_term(cls.solo_term, ((18, 15, 46), (10, 10, 20)))

    def test_no_row_carries_a_position(self):
        body = self.data(self.call(
            "get", self.solo_teacher, "gradebook-broadsheet", class_id=self.solo_class.pk,
            term=self.solo_term,
        ))
        self.assertEqual(body["stage"], "PUBLISHED")
        self.assertFalse(body["ranks"])
        for row in body["rows"]:
            self.assertNotIn("position", row)
            self.assertNotIn("position_of", row)
        self.assertEqual({r["average"] for r in body["rows"]}, {"79.00", "40.00"})
