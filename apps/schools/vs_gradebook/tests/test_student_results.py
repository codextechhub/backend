"""A student's Results tab: every term's published result, and the history of their holds."""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from schools.vs_gradebook.models import ClassTerm, ResultHold, StudentTermResult

from .base import school_clock
from .card_base import SEND, CardFixture

CALM = (
    "Nothing has been published for this term yet. Results appear here once the "
    "principal approves and publishes the class."
)
LATER = dt.datetime(2026, 2, 2, 12, 0, tzinfo=dt.timezone.utc)


def term_of(body, name):
    return next(entry for entry in body["terms"] if entry["term"]["name"] == name)


class PublishedTermTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_jss1a(held=[cls.amina])

    def test_a_published_term_carries_the_average_position_release_and_subjects(self):
        response = self.results_tab(self.result_reader, self.tunde)
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertEqual(body["student"], {
            "id": self.tunde.pk, "name": "Tunde Bello", "admission_number": "BFS/002",
            "class": {"id": self.jss1a.pk, "name": "JSS1 A"},
            "branch": {"id": self.lekki.pk, "name": "Lekki Branch"},
        })
        [entry] = body["terms"]
        self.assertTrue(entry["published"])
        self.assertEqual(entry["term"]["name"], "First Term")
        self.assertEqual(entry["session"]["name"], "2025/2026")
        self.assertEqual(entry["overall_average"], "75.00")
        self.assertEqual(entry["position"], "1st of 4")
        self.assertEqual(entry["release_label"], "Released Tue 16 Dec 2025")
        self.assertEqual(entry["class_id"], self.jss1a.pk)
        maths = next(s for s in entry["subjects"] if s["name"] == "Mathematics")
        self.assertEqual(maths, {"name": "Mathematics", "total": 75, "grade": "A", "remark": "Excellent"})

    def test_the_guardian_is_the_primary_guardian_by_name_and_address(self):
        body = self.data(self.results_tab(self.result_reader, self.chiamaka))
        self.assertEqual(
            body["guardian"], {"name": "Mrs. Ngozi Nwosu", "email": "ngozi.nwosu@example.ng"},
        )
        self.assertEqual(
            self.data(self.results_tab(self.result_reader, self.tunde))["guardian"],
            {"name": "Mrs. Aduke Bello", "email": None},
        )

    def test_a_held_students_term_reads_held_and_has_no_release_time(self):
        entry = self.data(self.results_tab(self.result_reader, self.amina))["terms"][0]
        self.assertEqual(entry["release_label"], "Held")
        self.assertIsNone(entry["released_at"])
        self.assertEqual(entry["position"], "3rd of 4")

    def test_a_published_but_unreleased_result_with_no_hold_reads_published_not_yet_released(self):
        StudentTermResult.objects.filter(student=self.amina).update(email_state=None)
        entry = self.data(self.results_tab(self.result_reader, self.amina))["terms"][0]
        self.assertEqual(entry["release_label"], "Published, not yet released")

    def test_the_snapshot_is_what_is_read_not_the_live_marks(self):
        from schools.vs_gradebook.models import Score

        Score.objects.update(mark=0)
        entry = self.data(self.results_tab(self.result_reader, self.tunde))["terms"][0]
        self.assertEqual(entry["overall_average"], "75.00")

    def test_the_history_lists_the_placed_hold_with_its_reason_and_who_placed_it(self):
        body = self.data(self.results_tab(self.result_reader, self.amina))
        [event] = body["history"]
        self.assertEqual(event["kind"], "HOLD_PLACED")
        self.assertEqual(event["text"], "Result held for First Term")
        self.assertEqual(event["reason"], "Fees outstanding since September.")
        self.assertEqual(event["by"], "Bimpe Bursar")
        self.assertIsNotNone(event["at"])

    def test_a_lifted_hold_shows_both_events_newest_first_with_the_lift_note(self):
        hold = ResultHold.objects.get(student=self.amina)
        with mock.patch(SEND, return_value=["sent"]):
            self.assertEqual(self.lift(self.bursar, hold.pk, note="Paid in full.").status_code, 200)
        body = self.data(self.results_tab(self.result_reader, self.amina))
        self.assertEqual([e["kind"] for e in body["history"]], ["HOLD_LIFTED", "HOLD_PLACED"])
        lifted = body["history"][0]
        self.assertEqual(lifted["text"], "Hold lifted for First Term")
        self.assertEqual(lifted["lift_note"], "Paid in full.")
        self.assertEqual(lifted["by"], "Bimpe Bursar")
        self.assertEqual(body["history"][1]["reason"], "Fees outstanding since September.")

    def test_a_student_with_no_hold_has_an_empty_history(self):
        self.assertEqual(self.data(self.results_tab(self.result_reader, self.tunde))["history"], [])


class TermsOfTheTabTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_jss1a()

    def test_a_term_that_has_begun_with_nothing_published_is_one_calm_line(self):
        with school_clock(LATER):
            body = self.data(self.results_tab(self.result_reader, self.tunde))
        self.assertEqual([e["term"]["name"] for e in body["terms"]], ["Second Term", "First Term"])
        second = term_of(body, "Second Term")
        self.assertEqual(second, {
            "term": {"id": self.second_term.pk, "name": "Second Term"},
            "session": {"id": self.year.pk, "name": "2025/2026"},
            "published": False, "message": CALM,
        })
        self.assertTrue(term_of(body, "First Term")["published"])

    def test_a_term_that_has_not_begun_is_not_listed(self):
        body = self.data(self.results_tab(self.result_reader, self.tunde))
        self.assertEqual([e["term"]["name"] for e in body["terms"]], ["First Term"])

    def test_a_student_with_nothing_published_anywhere_has_a_calm_term_and_no_history(self):
        body = self.data(self.results_tab(self.result_reader, self.zainab))
        self.assertEqual(len(body["terms"]), 1)
        [entry] = body["terms"]
        self.assertFalse(entry["published"])
        self.assertEqual(entry["message"], CALM)
        self.assertEqual(body["history"], [])
        for key in ("overall_average", "position", "subjects", "release_label", "released_at"):
            self.assertNotIn(key, entry)

    def test_a_student_with_no_placement_still_reads_the_current_term_calmly(self):
        from schools.vs_students.models import ClassEnrolment

        ClassEnrolment.all_objects.filter(student=self.zainab).delete()
        body = self.data(self.results_tab(self.result_reader, self.zainab))
        self.assertEqual([e["term"]["name"] for e in body["terms"]], ["First Term"])
        self.assertIsNone(body["student"]["class"])

    def test_the_cost_of_the_tab_does_not_grow_with_the_terms_published(self):
        def cost():
            with CaptureQueriesContext(connection) as captured, school_clock(LATER):
                response = self.results_tab(self.result_reader, self.tunde)
            self.assertEqual(response.status_code, 200)
            return len(captured)

        before = cost()
        class_term = ClassTerm.objects.get(school_class=self.jss1a)
        second = ClassTerm.objects.create(
            tenant=self.tenant, school_class=self.jss1a, term=self.second_term,
            branch=self.lekki, structure=class_term.structure, stage="PUBLISHED",
            published_at=LATER,
        )
        first = StudentTermResult.objects.get(student=self.tunde)
        StudentTermResult.objects.create(
            tenant=self.tenant, class_term=second, student=self.tunde, snapshot=first.snapshot,
            version=1, released_at=LATER, email_state="SENT",
        )
        self.assertEqual(cost(), before)


class RankingTests(CardFixture):
    """A school that does not rank prints no position on the tab, and the shape of school does not matter."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_sunrise()

    def test_sunrise_shows_the_average_and_no_position(self):
        body = self.data(self.results_tab(self.solo_result_reader, self.solo_pupil))
        [entry] = body["terms"]
        self.assertTrue(entry["published"])
        self.assertIn("overall_average", entry)
        self.assertNotIn("position", entry)
        self.assertIsNone(body["student"]["branch"])
        self.assertEqual(body["student"]["class"]["name"], "Basic 3")
        self.assertEqual(body["guardian"]["name"], "Mr. Okoro")


class ResultsTabSecurityTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_jss1a(held=[cls.amina])

    def test_only_the_result_key_may_read_the_tab_and_the_release_key_cannot(self):
        for user in (self.releaser, self.principal, self.eze, self.admin, self.bursar, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.results_tab(user, self.tunde).status_code, 403)
        self.assertEqual(self.results_tab(self.result_reader, self.tunde).status_code, 200)

    def test_the_result_key_opens_nothing_of_the_release_screens(self):
        self.assertEqual(self.release(self.result_reader).status_code, 403)
        self.assertEqual(self.report_card(self.result_reader, self.tunde).status_code, 403)
        self.assertEqual(self.class_cards(self.result_reader).status_code, 403)

    def test_a_student_of_another_branch_is_a_404_for_a_pinned_reader(self):
        self.assertEqual(self.results_tab(self.ikeja_result_reader, self.tunde).status_code, 404)
        self.assertEqual(self.results_tab(self.lekki_result_reader, self.sade).status_code, 404)
        self.assertEqual(self.results_tab(self.lekki_result_reader, self.tunde).status_code, 200)
        self.assertEqual(self.results_tab(self.ikeja_result_reader, self.sade).status_code, 200)

    def test_a_student_of_another_school_is_a_404(self):
        self.assertEqual(self.results_tab(self.solo_result_reader, self.tunde).status_code, 404)
        self.assertEqual(self.results_tab(self.result_reader, self.solo_pupil).status_code, 404)

    def test_an_unknown_student_is_a_404(self):
        response = self.call("get", self.result_reader, "gradebook-student-results", student_id=999999)
        self.assertEqual(response.status_code, 404)
