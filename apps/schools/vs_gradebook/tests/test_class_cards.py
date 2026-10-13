"""A class's cards for printing: every released student's card, in roll order."""
from __future__ import annotations

import copy

from django.db import connection
from django.test.utils import CaptureQueriesContext

from schools.vs_gradebook.models import ClassTerm, StudentTermResult

from .card_base import RELEASED_AT, CardFixture


class ClassCardsTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_jss1a(held=[cls.amina])

    def test_every_released_students_card_is_in_roll_order(self):
        body = self.data(self.class_cards(self.releaser))
        names = [self.lines(card, "facts")["Student"] for card in body["cards"]]
        self.assertEqual(names, ["Tunde Bello", "Kelechi Eze", "Chiamaka Nwosu"])
        self.assertEqual(body["class"]["name"], "JSS1 A")
        self.assertEqual(body["term"]["name"], "First Term")

    def test_a_card_in_the_print_is_the_card_of_the_single_route(self):
        body = self.data(self.class_cards(self.releaser))
        single = self.data(self.report_card(self.releaser, self.tunde))
        self.assertEqual(body["cards"][0], single)

    def test_a_held_student_is_left_out_and_counted(self):
        body = self.data(self.class_cards(self.releaser))
        self.assertEqual(body["left_out_count"], 1)
        [left] = body["left_out"]
        self.assertEqual(left["student"]["name"], "Amina Yusuf")
        self.assertEqual(left["reason"], "HELD")
        for card in body["cards"]:
            self.assertIs(card["held"], False)

    def test_a_student_the_class_never_published_is_left_out_as_not_released(self):
        from schools.vs_students.models import ClassEnrolment

        ClassEnrolment.all_objects.filter(student=self.obinna).update(
            effective_date=self.first_term.start_date,
        )
        body = self.data(self.class_cards(self.releaser))
        reasons = {row["student"]["name"]: row["reason"] for row in body["left_out"]}
        self.assertEqual(reasons, {"Amina Yusuf": "HELD", "Obinna Okafor": "NOT_RELEASED"})
        self.assertEqual(body["left_out_count"], 2)

    def test_the_letterhead_is_the_same_on_every_card(self):
        heads = {str(card["letterhead"]) for card in self.data(self.class_cards(self.releaser))["cards"]}
        self.assertEqual(len(heads), 1)

    def test_a_class_that_is_not_published_cannot_be_printed(self):
        ClassTerm.objects.filter(school_class=self.jss1a).update(stage="AWAITING")
        response = self.class_cards(self.releaser)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "NOT_PUBLISHED")

    def test_lifting_the_hold_puts_the_card_in_the_print(self):
        from unittest import mock

        from .card_base import SEND

        with mock.patch(SEND, return_value=["sent"]):
            from schools.vs_gradebook.models import ResultHold

            self.lift(self.bursar, ResultHold.objects.get(student=self.amina).pk)
        body = self.data(self.class_cards(self.releaser))
        self.assertEqual(len(body["cards"]), 4)
        self.assertEqual(body["left_out_count"], 0)
        self.assertEqual(body["left_out"], [])


class ClassCardsSecurityTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_jss1a(held=[cls.amina])

    def test_only_the_release_key_may_print(self):
        for user in (self.result_reader, self.principal, self.eze, self.admin, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.class_cards(user).status_code, 403)

    def test_another_branch_and_another_school_get_a_404(self):
        self.assertEqual(self.class_cards(self.ikeja_releaser).status_code, 404)
        response = self.call(
            "get", self.solo_releaser, "gradebook-report-cards", class_id=self.jss1a.pk,
            term=self.solo_term,
        )
        self.assertEqual(response.status_code, 404)

    def test_a_branch_pinned_reader_of_the_right_branch_may_print(self):
        self.assertEqual(self.class_cards(self.lekki_releaser).status_code, 200)


class ClassCardsQueryTests(CardFixture):
    """The cost of a print does not grow with the class."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.class_term = cls.publish_jss1a()

    def add_published(self, count):
        template = StudentTermResult.objects.get(student=self.tunde)
        for number in range(count):
            pupil = self.make_student("Extra", f"Pupil{number:02d}", self.jss1a, f"BFS/9{number:02d}")
            snapshot = copy.deepcopy(template.snapshot)
            snapshot["student"] = {
                "id": pupil.pk, "name": pupil.full_name, "admission_number": pupil.student_number,
            }
            StudentTermResult.objects.create(
                tenant=self.tenant, class_term=self.class_term, student=pupil,
                snapshot=snapshot, version=1, released_at=RELEASED_AT, email_state="SENT",
            )

    def queries(self):
        with CaptureQueriesContext(connection) as captured:
            response = self.class_cards(self.releaser)
        self.assertEqual(response.status_code, 200, response.data)
        return len(captured), len(self.data(response)["cards"])

    def test_the_number_of_queries_is_the_same_for_a_small_class_and_a_large_one(self):
        small_queries, small_cards = self.queries()
        self.add_published(12)
        large_queries, large_cards = self.queries()
        self.assertEqual((small_cards, large_cards), (4, 16))
        self.assertEqual(large_queries, small_queries)

    def test_the_release_list_costs_the_same_however_large_the_class(self):
        def cost():
            with CaptureQueriesContext(connection) as captured:
                response = self.release(self.releaser)
            self.assertEqual(response.status_code, 200)
            return len(captured)

        small = cost()
        self.add_published(12)
        self.assertEqual(cost(), small)
