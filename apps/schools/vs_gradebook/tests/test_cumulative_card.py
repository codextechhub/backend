"""The cumulative report card and Results-tab entry, and the promotion line on them.

The card of a school year's last term prints each term's total, the annual
averages and the annual position from the snapshot, and the promotion line is
read live from the placement outcome Student Management recorded. The year's
earlier terms print no block and no line. The marks are those of
:class:`~.cumulative_base.CumulativeFixture`.
"""
from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext

from schools.vs_gradebook.services import promotion_line
from schools.vs_students.constants import EnrolmentOutcome

from .cumulative_base import CumulativeFixture


class _PublishedYear(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_year()
        cls.record_outcome(cls.tunde, EnrolmentOutcome.PROMOTED, cls.next_jss2a)
        cls.record_outcome(cls.chiamaka, EnrolmentOutcome.REPEATED, cls.next_jss1a)
        cls.record_outcome(cls.kelechi, EnrolmentOutcome.GRADUATED)

    def entry(self, student, term=None):
        """The Results tab's entry for *term* (the last by default) of *student*."""
        tab = self.data(self.results_tab(self.result_reader, student))
        wanted = (term or self.last_term).pk
        return next(t for t in tab["terms"] if t["term"]["id"] == wanted)


class CumulativeCardTests(_PublishedYear):
    def test_the_last_terms_card_prints_the_year(self):
        card = self.last_card(self.tunde)
        self.assertEqual(card["cumulative"], {
            "terms": ["First Term", "Second Term", "Third Term"],
            "subjects": [
                {"name": "French", "totals": ["75", "30", "60"], "annual_average": "55.00"},
                {"name": "Mathematics", "totals": ["75", "50", "70"], "annual_average": "65.00"},
            ],
            "annual_average": "60.00",
            "annual_position": "2nd of 4",
            "promotion_line": "Promoted to JSS2 A",
        })

    def test_a_subject_the_student_does_not_take_is_not_on_the_year(self):
        card = self.last_card(self.amina)
        self.assertEqual([s["name"] for s in card["cumulative"]["subjects"]], ["Mathematics"])

    def test_the_first_and_second_terms_print_no_year_and_no_promotion_line(self):
        for term in (self.first_term, self.second_term):
            response = self.report_card(self.releaser, self.tunde, term=term)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertNotIn("cumulative", self.data(response))

    def test_the_class_print_carries_each_cards_year_and_line(self):
        body = self.data(self.class_cards(self.releaser, term=self.third_term))
        lines = {
            card["facts"][0]["value"]: card["cumulative"].get("promotion_line")
            for card in body["cards"]
        }
        self.assertEqual(lines, {
            "Tunde Bello": "Promoted to JSS2 A", "Chiamaka Nwosu": "To repeat JSS1 A",
            "Kelechi Eze": "Graduated", "Amina Yusuf": None,
        })

    def test_the_annual_position_line_is_the_students_place_in_the_class(self):
        places = {
            card["facts"][0]["value"]: card["cumulative"]["annual_position"]
            for card in self.data(self.class_cards(self.releaser, term=self.third_term))["cards"]
        }
        self.assertEqual(places, {
            "Chiamaka Nwosu": "1st of 4", "Tunde Bello": "2nd of 4",
            "Amina Yusuf": "2nd of 4", "Kelechi Eze": "4th of 4",
        })


class PromotionLineTests(_PublishedYear):
    def line(self, student):
        return self.last_card(student)["cumulative"].get("promotion_line")

    def test_a_promoted_student_is_told_the_class_they_move_to(self):
        self.assertEqual(self.line(self.tunde), "Promoted to JSS2 A")

    def test_a_student_to_repeat_is_told_the_class_they_repeat(self):
        self.assertEqual(self.line(self.chiamaka), "To repeat JSS1 A")

    def test_a_graduate_is_told_they_graduated(self):
        self.assertEqual(self.line(self.kelechi), "Graduated")

    def test_a_student_with_no_outcome_recorded_has_no_line(self):
        self.assertIsNone(self.line(self.amina))
        self.assertNotIn("promotion_line", self.last_card(self.amina)["cumulative"])

    def test_the_line_is_read_live_when_the_outcome_is_recorded_after_publication(self):
        self.assertIsNone(self.line(self.amina))
        self.record_outcome(self.amina, EnrolmentOutcome.PROMOTED, self.next_jss2a)
        self.assertEqual(self.line(self.amina), "Promoted to JSS2 A")

    def test_an_outcome_that_is_not_a_promotion_decision_says_nothing(self):
        from schools.vs_students.models import ClassEnrolment

        for outcome in (
            EnrolmentOutcome.CURRENT, EnrolmentOutcome.TRANSFERRED, EnrolmentOutcome.ENDED,
        ):
            with self.subTest(outcome=outcome):
                ClassEnrolment.all_objects.filter(
                    student=self.amina, session=self.year,
                ).update(outcome=outcome)
                self.assertIsNone(self.line(self.amina))

    def test_a_later_years_outcome_does_not_change_this_years_line(self):
        from schools.vs_students.models import ClassEnrolment

        ClassEnrolment.all_objects.filter(
            student=self.tunde, session=self.next_year,
        ).update(outcome=EnrolmentOutcome.REPEATED)
        self.assertEqual(self.line(self.tunde), "Promoted to JSS2 A")

    def test_the_class_named_is_the_first_placement_of_the_next_year(self):
        from schools.vs_students.models import ClassEnrolment

        ClassEnrolment.all_objects.filter(student=self.tunde, session=self.next_year).update(
            is_active=False,
        )
        ClassEnrolment.all_objects.create(
            tenant=self.tenant, student=self.tunde, school_class=self.next_jss1a,
            session=self.next_year, is_active=True, effective_date=self.next_year.start_date,
        )
        self.assertEqual(self.line(self.tunde), "Promoted to JSS2 A")

    def test_the_lookup_costs_the_same_for_one_student_as_for_a_class(self):
        def cost(students):
            with CaptureQueriesContext(connection) as queries:
                found = promotion_line.promotion_lines(
                    self.tenant, self.year.pk, [s.pk for s in students],
                )
            return len(queries), found

        one, found_one = cost([self.tunde])
        many, found_many = cost([self.tunde, self.chiamaka, self.kelechi, self.amina])
        self.assertEqual(one, many)
        self.assertEqual(len(found_one), 1)
        self.assertEqual(len(found_many), 3)

    def test_no_students_is_no_query(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(promotion_line.promotion_lines(self.tenant, self.year.pk, []), {})
        self.assertEqual(len(queries), 0)


class CumulativeResultsTabTests(_PublishedYear):
    def test_the_last_terms_entry_carries_the_year_in_the_same_shape_as_the_card(self):
        self.assertEqual(
            self.entry(self.tunde)["cumulative"], self.last_card(self.tunde)["cumulative"],
        )

    def test_the_entry_carries_the_promotion_line(self):
        self.assertEqual(self.entry(self.kelechi)["cumulative"]["promotion_line"], "Graduated")
        self.assertNotIn("promotion_line", self.entry(self.amina)["cumulative"])

    def test_the_earlier_terms_entries_carry_no_year(self):
        for term in (self.first_term, self.second_term):
            self.assertNotIn("cumulative", self.entry(self.tunde, term))

    def test_the_entry_still_carries_the_terms_own_figures(self):
        entry = self.entry(self.tunde)
        self.assertEqual(entry["overall_average"], "65.00")
        self.assertEqual(entry["position"], "2nd of 4")
        self.assertEqual(entry["version"], 1)
