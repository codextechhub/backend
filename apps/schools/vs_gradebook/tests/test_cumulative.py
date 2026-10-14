"""The cumulative block published with the last term of a school year.

Which term is cumulative, what the block holds (the earlier terms' figures
taken from the student's own published snapshots, the annual averages and the
annual position), what it does when nothing was published or the student was in
another class, a school that does not rank, and that the class's earlier terms
are read in one query however big the class is.

The marks are those of :class:`~.cumulative_base.CumulativeFixture`.
"""
from __future__ import annotations

from types import SimpleNamespace

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from schools.vs_gradebook.models import ClassTerm, StudentTermResult
from schools.vs_gradebook.services import cumulative
from schools.vs_academics.models import ElectiveTaker

from .cumulative_base import CumulativeFixture


def term(pk):
    return SimpleNamespace(pk=pk)


class WhichTermIsCumulativeTests(SimpleTestCase):
    def test_the_last_term_of_a_year_with_several_is_cumulative(self):
        terms = [term(1), term(2), term(3)]
        self.assertTrue(cumulative.is_cumulative(terms, terms[2]))

    def test_the_first_and_second_terms_of_a_three_term_year_are_not(self):
        terms = [term(1), term(2), term(3)]
        self.assertFalse(cumulative.is_cumulative(terms, terms[0]))
        self.assertFalse(cumulative.is_cumulative(terms, terms[1]))

    def test_the_second_term_of_a_two_term_year_is(self):
        terms = [term(1), term(2)]
        self.assertTrue(cumulative.is_cumulative(terms, terms[1]))
        self.assertFalse(cumulative.is_cumulative(terms, terms[0]))

    def test_the_only_term_of_a_year_is_not(self):
        terms = [term(1)]
        self.assertFalse(cumulative.is_cumulative(terms, terms[0]))


class MeanTests(SimpleTestCase):
    def test_the_mean_of_the_figures_that_exist_to_two_places(self):
        self.assertEqual(cumulative.mean(["75.00", None, "50.00"]), "62.50")
        self.assertEqual(cumulative.mean(["79.00", "100.00", "91.00"]), "90.00")
        self.assertEqual(cumulative.mean(["64.50", "95.00", "85.50"]), "81.67")

    def test_no_figure_is_no_mean(self):
        self.assertIsNone(cumulative.mean([None, None]))
        self.assertIsNone(cumulative.mean([]))


class ThreeTermYearTests(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_year()

    def test_the_first_and_second_terms_carry_no_cumulative_block(self):
        for student in (self.tunde, self.chiamaka):
            for each in (self.first_term, self.second_term):
                self.assertNotIn("cumulative", self.stored(student, each))

    def test_the_third_term_carries_the_year(self):
        block = self.stored(self.tunde)["cumulative"]
        self.assertEqual(
            block["terms"],
            [
                {"id": self.first_term.pk, "name": "First Term"},
                {"id": self.second_term.pk, "name": "Second Term"},
                {"id": self.third_term.pk, "name": "Third Term"},
            ],
        )

    def test_a_subject_row_has_each_terms_total_and_percentage_and_the_annual_average(self):
        block = self.stored(self.tunde)["cumulative"]
        maths = next(r for r in block["subjects"] if r["subject"]["name"] == "Mathematics")
        self.assertEqual(maths["subject"], {"id": self.maths.pk, "name": "Mathematics"})
        self.assertEqual(
            maths["terms"],
            [
                {"term_id": self.first_term.pk, "total": 75, "percentage": "75.00"},
                {"term_id": self.second_term.pk, "total": 50, "percentage": "50.00"},
                {"term_id": self.third_term.pk, "total": 70, "percentage": "70.00"},
            ],
        )
        self.assertEqual(maths["annual_average"], "65.00")

    def test_every_subject_the_student_has_in_the_term_is_there_and_no_other(self):
        tunde = self.stored(self.tunde)["cumulative"]
        kelechi = self.stored(self.kelechi)["cumulative"]
        self.assertEqual(
            [r["subject"]["name"] for r in tunde["subjects"]], ["French", "Mathematics"],
        )
        self.assertEqual([r["subject"]["name"] for r in kelechi["subjects"]], ["Mathematics"])

    def test_an_annual_average_is_the_mean_of_the_term_percentages(self):
        block = self.stored(self.chiamaka)["cumulative"]
        french = next(r for r in block["subjects"] if r["subject"]["name"] == "French")
        self.assertEqual([t["percentage"] for t in french["terms"]], ["50.00", "90.00", "80.00"])
        self.assertEqual(french["annual_average"], "73.33")

    def test_the_overall_figures_are_the_term_averages_and_their_mean(self):
        block = self.stored(self.chiamaka)["cumulative"]
        self.assertEqual(
            block["overall"],
            {
                "terms": [
                    {"term_id": self.first_term.pk, "average": "64.50"},
                    {"term_id": self.second_term.pk, "average": "95.00"},
                    {"term_id": self.third_term.pk, "average": "85.50"},
                ],
                "annual_average": "81.67",
            },
        )

    def test_the_annual_position_shares_a_place_on_a_tie_and_skips_the_next(self):
        positions = {
            student: (
                self.stored(student)["cumulative"]["annual_position"],
                self.stored(student)["cumulative"]["annual_position_of"],
            )
            for student in (self.chiamaka, self.tunde, self.amina, self.kelechi)
        }
        self.assertEqual(positions, {
            self.chiamaka: (1, 4), self.tunde: (2, 4), self.amina: (2, 4), self.kelechi: (4, 4),
        })

    def test_the_term_figures_of_the_snapshot_are_the_blocks_third_entry(self):
        for student in (self.tunde, self.chiamaka, self.kelechi, self.amina):
            snapshot = self.stored(student)
            block = snapshot["cumulative"]
            self.assertEqual(block["overall"]["terms"][2]["average"], snapshot["overall_average"])
            for row, line in zip(block["subjects"], snapshot["subjects"]):
                self.assertEqual(row["terms"][2]["total"], line["total"])

    def test_the_block_is_frozen_with_the_card_whatever_happens_to_the_earlier_terms_later(self):
        before = self.stored(self.tunde)["cumulative"]["subjects"][0]["terms"][0]
        StudentTermResult.objects.filter(
            student=self.tunde, class_term__term=self.first_term,
        ).update(snapshot=None)
        self.assertEqual(self.stored(self.tunde)["cumulative"]["subjects"][0]["terms"][0], before)


class StudentWhoWasInAnotherClassTests(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_term(cls.first_term, 1)
        other = ClassTerm.objects.create(
            tenant=cls.tenant, school_class=cls.jss1b, term=cls.first_term,
            branch=cls.lekki, stage="PUBLISHED",
        )
        StudentTermResult.objects.filter(
            student=cls.tunde, class_term__term=cls.first_term,
        ).update(class_term=other)
        cls.publish_term(cls.second_term, 2)
        cls.publish_term(cls.third_term, 3)

    def test_the_first_terms_figures_still_appear_for_a_student_who_was_in_another_class(self):
        block = self.stored(self.tunde)["cumulative"]
        maths = next(r for r in block["subjects"] if r["subject"]["name"] == "Mathematics")
        self.assertEqual(maths["terms"][0]["total"], 75)
        self.assertEqual(maths["annual_average"], "65.00")
        self.assertEqual(block["overall"]["annual_average"], "60.00")

    def test_the_classmates_who_stayed_are_unaffected(self):
        self.assertEqual(
            self.stored(self.chiamaka)["cumulative"]["overall"]["annual_average"], "81.67",
        )


class NothingPublishedTests(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_term(cls.second_term, 2)
        cls.publish_term(cls.third_term, 3)

    def test_a_term_nothing_was_published_for_shows_nulls_and_is_left_out_of_the_mean(self):
        block = self.stored(self.tunde)["cumulative"]
        maths = next(r for r in block["subjects"] if r["subject"]["name"] == "Mathematics")
        self.assertEqual(
            maths["terms"][0],
            {"term_id": self.first_term.pk, "total": None, "percentage": None},
        )
        self.assertEqual(maths["annual_average"], "60.00")
        self.assertIsNone(block["overall"]["terms"][0]["average"])
        self.assertEqual(block["overall"]["annual_average"], "52.50")

    def test_the_card_prints_a_blank_for_that_terms_total(self):
        card = self.last_card(self.tunde)
        maths = next(r for r in card["cumulative"]["subjects"] if r["name"] == "Mathematics")
        self.assertEqual(maths["totals"], ["", "50", "70"])


class SubjectAbsentFromAnEarlierTermTests(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        ElectiveTaker.objects.create(
            tenant=cls.tenant, school_class=cls.jss1a, term=cls.third_term,
            subject=cls.french, student=cls.amina,
        )
        cls.publish_term(cls.first_term, 1)
        cls.publish_term(cls.second_term, 2)
        maths, french = cls.term_marks(3)
        cls.publish_term(cls.third_term, 3, french={**french, cls.amina: (10, 10, 20)})

    def test_a_subject_the_student_did_not_have_in_an_earlier_term_is_null_there(self):
        block = self.stored(self.amina)["cumulative"]
        french = next(r for r in block["subjects"] if r["subject"]["name"] == "French")
        self.assertEqual([t["total"] for t in french["terms"]], [None, None, 40])
        self.assertEqual(french["annual_average"], "40.00")


class TwoTermYearTests(CumulativeFixture):
    three_terms = False

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_year()

    def test_the_second_term_is_cumulative_and_the_first_is_not(self):
        self.assertNotIn("cumulative", self.stored(self.tunde, self.first_term))
        block = self.stored(self.tunde)["cumulative"]
        self.assertEqual([t["name"] for t in block["terms"]], ["First Term", "Second Term"])
        self.assertEqual(block["overall"]["annual_average"], "57.50")
        self.assertEqual(block["annual_position"], 3)


class OneTermYearTests(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_sunrise_term(cls.solo_term, ((18, 15, 46), (10, 10, 20)))

    def test_a_year_with_one_term_has_no_cumulative_block(self):
        for result in StudentTermResult.objects.filter(tenant=self.solo):
            self.assertNotIn("cumulative", result.snapshot)


class SchoolThatDoesNotRankTests(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        import datetime as dt

        from schools.vs_academics.models import AcademicTerm

        cls.solo_second = AcademicTerm.all_objects.create(
            tenant=cls.solo, session=cls.solo_year, name="Second Term", order_index=2,
            start_date=dt.date(2026, 1, 12), end_date=dt.date(2026, 4, 3),
        )
        cls.publish_sunrise_term(cls.solo_term, ((18, 15, 46), (10, 10, 20)))
        cls.publish_sunrise_term(cls.solo_second, ((20, 20, 50), (15, 15, 30)))

    def snapshot(self, pupil):
        return StudentTermResult.objects.get(
            student=pupil, class_term__term=self.solo_second,
        ).snapshot

    def test_the_block_has_annual_averages_and_no_annual_position(self):
        for pupil in (self.solo_pupil, self.solo_pupil_two):
            block = self.snapshot(pupil)["cumulative"]
            self.assertIsNotNone(block["overall"]["annual_average"])
            self.assertNotIn("annual_position", block)
            self.assertNotIn("annual_position_of", block)

    def test_the_card_and_the_results_tab_print_no_annual_position(self):
        card = self.data(self.call(
            "get", self.solo_releaser, "gradebook-report-card",
            class_id=self.solo_class.pk, student_id=self.solo_pupil.pk, term=self.solo_second,
        ))
        self.assertNotIn("annual_position", card["cumulative"])
        self.assertIn("annual_average", card["cumulative"])
        tab = self.data(self.results_tab(self.solo_result_reader, self.solo_pupil))
        entry = next(t for t in tab["terms"] if t["term"]["id"] == self.solo_second.pk)
        self.assertNotIn("annual_position", entry["cumulative"])
        self.assertNotIn("position", entry)


class EarlierTermsAreReadInBulkTests(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_term(cls.first_term, 1)
        cls.publish_term(cls.second_term, 2)

    def reads(self, students):
        terms = [self.first_term, self.second_term]
        with CaptureQueriesContext(connection) as queries:
            found = cumulative.earlier_figures(self.tenant, terms, [s.pk for s in students])
        return found, len(queries)

    def test_one_query_whether_the_class_has_two_students_or_four(self):
        small, small_queries = self.reads([self.tunde, self.chiamaka])
        large, large_queries = self.reads(
            [self.tunde, self.chiamaka, self.kelechi, self.amina],
        )
        self.assertEqual(small_queries, 1)
        self.assertEqual(large_queries, 1)
        self.assertEqual(len(small), 2)
        self.assertEqual(len(large), 4)

    def test_attaching_the_block_costs_the_same_for_a_bigger_class(self):
        def snapshots(students):
            return {
                s.pk: {
                    "subjects": [], "overall_average": None,
                }
                for s in students
            }

        terms = cumulative.session_terms(self.tenant, self.third_term)

        def cost(students):
            data = snapshots(students)
            with CaptureQueriesContext(connection) as queries:
                cumulative.attach(
                    self.tenant, terms, self.third_term, data, ranks=True, roll_size=len(data),
                )
            return len(queries)

        self.assertEqual(cost([self.tunde, self.chiamaka]), cost(
            [self.tunde, self.chiamaka, self.kelechi, self.amina],
        ))

    def test_only_the_latest_published_snapshot_of_a_term_is_read(self):
        found, _ = self.reads([self.tunde])
        first = found[self.tunde.pk][self.first_term.pk]
        self.assertEqual(first.overall, "75.00")
        self.assertEqual(first.subjects[self.maths.pk], (75, "75.00"))

    def test_a_student_with_nothing_published_is_absent(self):
        found, _ = self.reads([self.obinna])
        self.assertEqual(found, {})
