"""The arithmetic of a result, with no database.

Every case is a student a teacher could point at: marks, ABS, an excused
component, a blank, a student excused from everything, and the senior scale's
boundaries.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from ..services.compute import (
    Band,
    Component,
    SCALE_OK_MESSAGE,
    STRUCTURE_OK_MESSAGE,
    band_for,
    class_stats,
    overall_average,
    positions,
    round2,
    row_result,
    scale_check,
    structure_check,
)

#: Two continuous assessments of 20 and an examination of 60.
STANDARD = [Component(20), Component(20), Component(60, is_exam=True)]

SENIOR = [
    Band(75, "A1", "Excellent"), Band(70, "B2", "Very good"), Band(65, "B3", "Good"),
    Band(60, "C4", "Credit"), Band(55, "C5", "Credit"), Band(50, "C6", "Credit"),
    Band(45, "D7", "Pass"), Band(40, "E8", "Pass"), Band(0, "F9", "Fail", is_fail=True),
]


class RowResultTests(SimpleTestCase):
    def test_marks_add_up_and_the_percentage_picks_the_band(self):
        result = row_result(STANDARD, [(18, None), (15, None), (46, None)], SENIOR)
        self.assertEqual((result.total, result.obtainable), (79, 100))
        self.assertEqual(result.percentage, Decimal(79))
        self.assertEqual((result.grade, result.remark, result.is_fail), ("A1", "Excellent", False))
        self.assertTrue(result.complete)
        self.assertFalse(result.excused_any)

    def test_absent_counts_zero_and_keeps_the_maximum(self):
        """Missing the 20-mark first test costs those 20 marks."""
        result = row_result(STANDARD, [(None, "ABS"), (15, None), (46, None)], SENIOR)
        self.assertEqual((result.total, result.obtainable), (61, 100))
        self.assertEqual(result.percentage, Decimal(61))
        self.assertEqual(result.grade, "C4")

    def test_excused_removes_the_component_from_what_could_be_obtained(self):
        """1st CA excused, 2nd CA 15 of 20, exam 46 of 60: 61 out of 80, graded at 76%."""
        result = row_result(STANDARD, [(None, "EXC"), (15, None), (46, None)], SENIOR)
        self.assertEqual((result.total, result.obtainable), (61, 80))
        self.assertEqual(result.percentage, Decimal("76.25"))
        self.assertEqual(round2(result.percentage), Decimal("76.25"))
        self.assertEqual(result.grade, "A1")
        self.assertTrue(result.excused_any)
        self.assertFalse(result.all_excused)

    def test_a_blank_makes_the_row_incomplete_with_nothing_worked_out(self):
        result = row_result(STANDARD, [(18, None), (None, None), (46, None)], SENIOR)
        self.assertFalse(result.complete)
        self.assertIsNone(result.total)
        self.assertIsNone(result.obtainable)
        self.assertIsNone(result.percentage)
        self.assertIsNone(result.grade)
        self.assertIsNone(result.remark)

    def test_a_blank_is_not_a_zero(self):
        blank = row_result(STANDARD, [(0, None), (0, None), (None, None)], SENIOR)
        zero = row_result(STANDARD, [(0, None), (0, None), (0, None)], SENIOR)
        self.assertFalse(blank.complete)
        self.assertTrue(zero.complete)
        self.assertEqual(zero.grade, "F9")
        self.assertTrue(zero.is_fail)

    def test_every_component_excused_has_no_grade(self):
        result = row_result(STANDARD, [(None, "EXC")] * 3, SENIOR)
        self.assertTrue(result.all_excused)
        self.assertTrue(result.complete)
        self.assertIsNone(result.percentage)
        self.assertIsNone(result.grade)
        self.assertEqual((result.total, result.obtainable), (0, 0))

    def test_the_grade_comes_from_the_unrounded_percentage(self):
        """74.5 shows as 74.50 and stays below 75; it is not rounded up into A1."""
        components = [Component(100)]
        below = row_result(components, [(74, None)], SENIOR)
        self.assertEqual(below.grade, "B2")
        halves = [Component(200)]
        result = row_result(halves, [(149, None)], SENIOR)
        self.assertEqual(result.percentage, Decimal("74.5"))
        self.assertEqual(result.grade, "B2")

    def test_the_senior_boundaries(self):
        one_hundred = [Component(100)]
        expected = {
            100: "A1", 75: "A1", 74: "B2", 70: "B2", 69: "B3", 65: "B3", 64: "C4",
            60: "C4", 59: "C5", 55: "C5", 54: "C6", 50: "C6", 49: "D7", 45: "D7",
            44: "E8", 40: "E8", 39: "F9", 0: "F9",
        }
        for mark, grade in expected.items():
            with self.subTest(mark=mark):
                self.assertEqual(row_result(one_hundred, [(mark, None)], SENIOR).grade, grade)

    def test_a_row_needs_one_cell_per_component(self):
        with self.assertRaises(ValueError):
            row_result(STANDARD, [(1, None)], SENIOR)

    def test_a_scale_that_does_not_reach_the_percentage_gives_no_grade(self):
        result = row_result([Component(100)], [(10, None)], [Band(50, "P", "Pass")])
        self.assertEqual(result.percentage, Decimal(10))
        self.assertIsNone(result.grade)
        self.assertIsNone(band_for(Decimal(10), [Band(50, "P")]))
        self.assertIsNone(band_for(None, SENIOR))


class OverallAverageTests(SimpleTestCase):
    def test_the_mean_of_the_percentages_that_exist(self):
        average = overall_average([Decimal(80), Decimal(60), None, Decimal(70)])
        self.assertEqual(average, Decimal(70))

    def test_a_subject_not_in_is_left_out_and_not_counted_as_zero(self):
        self.assertEqual(overall_average([Decimal(90), None]), Decimal(90))

    def test_nothing_to_average_is_none(self):
        self.assertIsNone(overall_average([]))
        self.assertIsNone(overall_average([None, None]))

    def test_the_average_is_kept_unrounded(self):
        average = overall_average([Decimal(70), Decimal(71), Decimal(71)])
        self.assertEqual(round2(average), Decimal("70.67"))
        self.assertNotEqual(average, round2(average))


class PositionTests(SimpleTestCase):
    def test_ties_share_a_position_and_the_next_is_skipped(self):
        averages = {
            "ada": Decimal("90.00"), "bayo": Decimal("85.50"),
            "chi": Decimal("80.00"), "dayo": Decimal("80.00"), "efe": Decimal("70.00"),
        }
        self.assertEqual(
            positions(averages),
            {"ada": 1, "bayo": 2, "chi": 3, "dayo": 3, "efe": 5},
        )

    def test_ties_are_compared_at_two_decimal_places(self):
        averages = {"a": Decimal("80.004"), "b": Decimal("79.996"), "c": Decimal("79.994")}
        self.assertEqual(positions(averages), {"a": 1, "b": 1, "c": 3})

    def test_a_student_with_no_average_has_no_position(self):
        result = positions({"a": Decimal(50), "b": None, "c": Decimal(60)})
        self.assertEqual(result, {"a": 2, "b": None, "c": 1})

    def test_everyone_equal_is_first(self):
        self.assertEqual(positions({"a": Decimal(50), "b": Decimal(50)}), {"a": 1, "b": 1})

    def test_an_empty_class(self):
        self.assertEqual(positions({}), {})


class ClassStatsTests(SimpleTestCase):
    def test_average_to_one_decimal_place_highest_and_lowest(self):
        stats = class_stats([61, 72, 80, None, 55])
        self.assertEqual(stats.average, Decimal("67.0"))
        self.assertEqual((stats.highest, stats.lowest), (80, 55))

    def test_halves_round_up(self):
        self.assertEqual(class_stats([10, 11]).average, Decimal("10.5"))
        self.assertEqual(class_stats([1, 1, 2]).average, Decimal("1.3"))

    def test_no_totals_gives_nothing(self):
        stats = class_stats([None, None])
        self.assertEqual((stats.average, stats.highest, stats.lowest), (None, None, None))
        self.assertEqual(class_stats([]).average, None)


class ScaleCheckTests(SimpleTestCase):
    def test_a_sound_scale(self):
        verdict = scale_check(SENIOR)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.message, SCALE_OK_MESSAGE)
        self.assertEqual(verdict.message, "No gap, no overlap, 0 to 100 covered.")

    def test_no_bands(self):
        self.assertEqual(scale_check([]).message, "Add at least one band.")

    def test_the_lowest_band_must_start_at_zero(self):
        verdict = scale_check([Band(10, "A"), Band(50, "B")])
        self.assertFalse(verdict.ok)
        self.assertEqual(
            verdict.message,
            "The lowest band starts at 10, so marks below it have no grade. "
            "Make the lowest band start at 0.",
        )

    def test_two_bands_with_the_same_lowest_mark_overlap(self):
        verdict = scale_check([Band(0, "F", is_fail=True), Band(50, "C"), Band(50, "D")])
        self.assertFalse(verdict.ok)
        self.assertIn("Two bands start at 50, so they overlap", verdict.message)

    def test_a_scale_of_only_fails_cannot_be_passed(self):
        verdict = scale_check([Band(0, "F", is_fail=True), Band(40, "E", is_fail=True)])
        self.assertEqual(
            verdict.message, "Every band is a fail. Mark at least one band as a pass.",
        )

    def test_a_band_needs_a_grade_and_a_lowest_mark_in_range(self):
        self.assertEqual(
            scale_check([Band(0, " ")]).message, "Every band needs a grade.",
        )
        self.assertEqual(
            scale_check([Band(101, "A"), Band(0, "F")]).message,
            "A band's lowest mark must be between 0 and 100.",
        )

    def test_the_order_the_bands_are_listed_in_does_not_matter(self):
        self.assertTrue(scale_check(list(reversed(SENIOR))).ok)


class StructureCheckTests(SimpleTestCase):
    def test_a_sound_structure(self):
        verdict = structure_check(STANDARD)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.message, STRUCTURE_OK_MESSAGE)

    def test_the_components_must_total_one_hundred(self):
        verdict = structure_check([Component(20), Component(20), Component(59)])
        self.assertFalse(verdict.ok)
        self.assertEqual(
            verdict.message,
            "The components add up to 99, not 100. Adjust the maximums until they total 100.",
        )

    def test_at_most_one_component_is_the_examination(self):
        verdict = structure_check([Component(40, True), Component(60, True)])
        self.assertFalse(verdict.ok)
        self.assertIn("Only one component can be the examination", verdict.message)

    def test_a_structure_without_components(self):
        self.assertEqual(structure_check([]).message, "Add at least one component.")
