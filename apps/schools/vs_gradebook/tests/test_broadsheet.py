"""The broadsheet: every student against every subject, and what stops the class being submitted."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from schools.vs_attendance.services.term_summary import class_term_summary
from schools.vs_gradebook.constants import SheetStatus
from schools.vs_gradebook.models import ClassTerm

from .class_base import ClassFixture

SUMMARY = "schools.vs_gradebook.services.classbook.class_term_summary"


class BroadsheetSecurityTests(ClassFixture):
    def test_the_class_teacher_reads_their_own_class(self):
        self.assertEqual(self.broadsheet(self.eze).status_code, 200)

    def test_a_teacher_who_is_not_the_class_teacher_is_refused(self):
        response = self.broadsheet(self.bello)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.code(response), "NOT_YOUR_CLASS")

    def test_a_principal_and_a_hub_reader_read_any_class_in_their_branches(self):
        for user in (self.principal, self.hub_reader, self.lekki_principal):
            with self.subTest(user=user.email):
                self.assertEqual(self.broadsheet(user).status_code, 200)

    def test_a_caller_with_no_gradebook_key_is_refused(self):
        self.assertEqual(self.broadsheet(self.nobody).status_code, 403)
        self.assertEqual(self.broadsheet(self.bursar).status_code, 403)

    def test_a_class_outside_a_branch_bound_callers_branch_is_a_404(self):
        self.assertEqual(self.broadsheet(self.ikeja_principal).status_code, 404)
        self.assertEqual(self.broadsheet(self.ikeja_admin).status_code, 404)

    def test_another_schools_class_is_a_404_whoever_asks(self):
        response = self.call(
            "get", self.solo_admin, "gradebook-broadsheet", class_id=self.jss1a.pk,
            term=self.solo_term,
        )
        self.assertEqual(response.status_code, 404)
        response = self.call(
            "get", self.admin, "gradebook-broadsheet", class_id=self.solo_class.pk,
            term=self.first_term,
        )
        self.assertEqual(response.status_code, 404)

    def test_a_term_of_another_school_is_a_404(self):
        response = self.broadsheet(self.principal, term=self.solo_term)
        self.assertEqual(response.status_code, 404)

    def test_a_term_of_another_school_year_is_refused(self):
        from schools.vs_academics.models import AcademicSession, AcademicTerm
        import datetime as dt

        other = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2026/2027", start_date=dt.date(2026, 9, 7),
            end_date=dt.date(2027, 7, 16), status="DRAFT",
        )
        term = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=other, name="First Term", order_index=1,
            start_date=dt.date(2026, 9, 7), end_date=dt.date(2026, 12, 11),
        )
        response = self.broadsheet(self.principal, term=term)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "INVALID_TERM")


class BroadsheetShapeTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sheets_in()

    def test_the_header_names_the_class_the_term_and_the_class_teacher(self):
        body = self.data(self.broadsheet(self.eze))
        self.assertEqual(body["class"], {"id": self.jss1a.pk, "name": "JSS1 A"})
        self.assertEqual(body["term"], {"id": self.first_term.pk, "name": "First Term"})
        self.assertEqual(body["class_teacher"]["name"], "Chidi Eze")
        self.assertEqual(body["stage"], "COMPILING")
        self.assertIsNone(body["returned"])
        self.assertTrue(body["ranks"])

    def test_the_columns_are_the_subjects_the_class_owes_with_their_teachers(self):
        body = self.data(self.broadsheet(self.eze))
        self.assertEqual([s["name"] for s in body["subjects"]], ["French", "Mathematics"])
        french, maths = body["subjects"]
        self.assertEqual(maths["short"], "MTH")
        self.assertTrue(maths["in"] and french["in"])
        self.assertEqual(maths["main_teacher"]["name"], "Chidi Eze")
        self.assertIsNone(french["main_teacher"])

    def test_every_student_on_the_roll_is_a_row_in_name_order_with_a_suspended_one_included(self):
        body = self.data(self.broadsheet(self.eze))
        self.assertEqual(
            [r["student"]["name"] for r in body["rows"]],
            ["Tunde Bello", "Kelechi Eze", "Chiamaka Nwosu", "Amina Yusuf"],
        )
        self.assertEqual(body["rows"][0]["student"]["admission_number"], "BFS/002")

    def test_a_row_holds_a_cell_for_each_subject_with_the_total_or_the_reason_there_is_none(self):
        body = self.data(self.broadsheet(self.eze))
        columns = [s["id"] for s in body["subjects"]]
        tunde = self.row_of(body, self.tunde)
        self.assertEqual(
            tunde["cells"],
            [
                {"subject": columns[0], "state": "TOTAL", "total": 75},
                {"subject": columns[1], "state": "TOTAL", "total": 75},
            ],
        )
        kelechi = self.row_of(body, self.kelechi)
        self.assertEqual(kelechi["cells"][0], {"subject": columns[0], "state": "NOT_TAKEN", "total": None})
        self.assertEqual(kelechi["cells"][1], {"subject": columns[1], "state": "TOTAL", "total": 40})

    def test_the_average_is_two_places_and_positions_rank_highest_first(self):
        body = self.data(self.broadsheet(self.eze))
        expected = {
            self.tunde: ("75.00", 1), self.chiamaka: ("64.50", 2),
            self.amina: ("60.00", 3), self.kelechi: ("40.00", 4),
        }
        for student, (average, position) in expected.items():
            row = self.row_of(body, student)
            self.assertEqual(row["average"], average)
            self.assertEqual(row["position"], position)
            self.assertEqual(row["position_of"], 4)

    def test_a_school_that_does_not_rank_has_no_position_key_at_all(self):
        self.make_sheet(
            self.solo_class, self.solo_maths, status=SheetStatus.SUBMITTED,
            rows={self.solo_pupil: (18, 15, 46), self.solo_pupil_two: (10, 10, 20)},
        )
        body = self.data(self.call(
            "get", self.solo_teacher, "gradebook-broadsheet", class_id=self.solo_class.pk,
            term=self.solo_term,
        ))
        self.assertFalse(body["ranks"])
        for row in body["rows"]:
            self.assertNotIn("position", row)
            self.assertNotIn("position_of", row)
            self.assertIn("average", row)

    def test_comment_done_follows_the_class_teachers_comment(self):
        self.assertEqual(self.compile_put(self.eze, self.tunde, {"teacher_comment": "Good."}).status_code, 200)
        body = self.data(self.broadsheet(self.eze))
        self.assertTrue(self.row_of(body, self.tunde)["comment_done"])
        self.assertFalse(self.row_of(body, self.kelechi)["comment_done"])


class BroadsheetTieTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sheets_in(maths=cls.tied_maths(), french=cls.tied_french())

    def test_ties_share_a_position_and_the_next_is_skipped(self):
        body = self.data(self.broadsheet(self.eze))
        positions = {r["student"]["name"]: r["position"] for r in body["rows"]}
        self.assertEqual(
            positions,
            {"Tunde Bello": 1, "Chiamaka Nwosu": 2, "Amina Yusuf": 2, "Kelechi Eze": 4},
        )
        averages = {r["student"]["name"]: r["average"] for r in body["rows"]}
        self.assertEqual(averages["Chiamaka Nwosu"], averages["Amina Yusuf"])


class BroadsheetGapTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.make_sheet(
            cls.jss1a, cls.maths, status=SheetStatus.SUBMITTED, rows=cls.default_maths(),
        )
        cls.make_sheet(cls.jss1a, cls.french, status=SheetStatus.IN_PROGRESS, rows={cls.tunde: (1, 2, 3)})

    def test_a_sheet_not_in_is_a_gap_with_its_teacher_and_its_cells_read_not_in(self):
        body = self.data(self.broadsheet(self.eze))
        self.assertEqual(body["stage"], "ENTERING")
        [gap] = body["gaps"]
        self.assertEqual(gap["subject"]["name"], "French")
        self.assertIsNone(gap["main_teacher"])
        french = next(s for s in body["subjects"] if s["name"] == "French")
        self.assertFalse(french["in"])
        tunde = self.row_of(body, self.tunde)
        cell = next(c for c in tunde["cells"] if c["subject"] == french["id"])
        self.assertEqual((cell["state"], cell["total"]), ("NOT_IN", None))

    def test_the_average_counts_only_the_subjects_that_are_in(self):
        body = self.data(self.broadsheet(self.eze))
        self.assertEqual(self.row_of(body, self.tunde)["average"], "75.00")
        self.assertEqual(self.row_of(body, self.chiamaka)["average"], "79.00")

    def test_the_class_cannot_be_submitted_and_the_blockers_say_why(self):
        body = self.data(self.broadsheet(self.eze))
        self.assertFalse(body["can_submit"])
        self.assertEqual(
            [s["name"] for s in body["submit_blockers"]["subjects_missing"]], ["French"],
        )
        self.assertEqual(body["submit_blockers"]["comments_missing"], 4)

    def test_a_returned_class_shows_who_returned_it_when_and_why(self):
        import datetime as dt

        ClassTerm.objects.filter(school_class=self.jss1a).update(
            returned_by=self.principal, returned_note="Recheck the comments.",
            returned_at=dt.datetime(2025, 10, 10, 9, 0, tzinfo=dt.timezone.utc),
        )
        body = self.data(self.broadsheet(self.eze))
        self.assertEqual(body["returned"]["by"], "Pauline Principal")
        self.assertEqual(body["returned"]["note"], "Recheck the comments.")


class BroadsheetSubmitGateTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sheets_in()
        cls.comment_all()

    def test_only_the_class_teacher_can_submit_and_only_when_nothing_is_missing(self):
        self.assertTrue(self.data(self.broadsheet(self.eze))["can_submit"])
        self.assertFalse(self.data(self.broadsheet(self.principal))["can_submit"])

    def test_an_awaiting_class_cannot_be_submitted_again(self):
        self.assertEqual(self.submit_class(self.eze).status_code, 200)
        body = self.data(self.broadsheet(self.eze))
        self.assertEqual(body["stage"], "AWAITING")
        self.assertFalse(body["can_submit"])


class AttendanceWarningTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sheets_in()

    def test_the_warning_says_how_many_school_days_had_no_register(self):
        expected = class_term_summary(self.tenant, self.jss1a, self.first_term).days_not_taken
        self.assertGreater(expected, 0)
        warning = self.data(self.broadsheet(self.eze))["attendance_warning"]
        self.assertEqual(warning, {"days_not_taken": expected})

    def test_it_is_null_when_every_day_had_a_register(self):
        with mock.patch(SUMMARY, return_value=SimpleNamespace(days_not_taken=0, students={})):
            self.assertIsNone(self.data(self.broadsheet(self.eze))["attendance_warning"])

    def test_the_average_is_a_two_place_string(self):
        row = self.row_of(self.data(self.broadsheet(self.eze)), self.chiamaka)
        self.assertEqual(Decimal(row["average"]), Decimal("64.50"))
