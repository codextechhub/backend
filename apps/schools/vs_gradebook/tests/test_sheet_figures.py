"""The class figures of a sheet, worked out without a database."""
from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from ..services.compute import Band, Component, round2, row_result, sheet_figures
from ..services.figures import excused_note, whole_percent

COMPONENTS = [Component(20), Component(20), Component(60, is_exam=True)]
BANDS = [Band(70, "A", "Excellent"), Band(40, "D", "Fair"), Band(0, "F", "Fail", True)]


def result(*cells):
    return row_result(COMPONENTS, list(cells), BANDS)


class SheetFiguresTests(SimpleTestCase):
    def test_the_mean_of_the_percentages_the_extreme_totals_and_the_pass_rate(self):
        found = sheet_figures([
            result((20, None), (20, None), (40, None)),   # 80
            result((10, None), (5, None), (10, None)),    # 25, a fail
            result((10, None), (10, None), (20, None)),   # 40, a pass
        ])
        self.assertEqual(round2(found.average), Decimal("48.33"))
        self.assertEqual((found.highest, found.lowest, found.pass_rate, found.graded), (80, 25, 67, 3))

    def test_an_incomplete_row_and_a_row_excused_from_everything_are_left_out(self):
        found = sheet_figures([
            result((20, None), (20, None), (40, None)),
            result((None, None), (5, None), (10, None)),
            result((None, "EXC"), (None, "EXC"), (None, "EXC")),
        ])
        self.assertEqual((found.average, found.highest, found.lowest, found.graded), (Decimal(80), 80, 80, 1))

    def test_nothing_graded_is_all_none(self):
        found = sheet_figures([result((None, None), (None, None), (None, None))])
        self.assertEqual(
            (found.average, found.highest, found.lowest, found.pass_rate, found.graded),
            (None, None, None, None, 0),
        )
        self.assertEqual(sheet_figures([]).pass_rate, None)

    def test_the_pass_rate_rounds_halves_up_to_a_whole_percent(self):
        rows = [result((20, None), (20, None), (40, None))] * 3 + [
            result((1, None), (1, None), (1, None)),
        ] * 5
        self.assertEqual(sheet_figures(rows).pass_rate, 38)  # 3 of 8 is 37.5

    def test_a_total_is_a_total_not_a_percentage(self):
        found = sheet_figures([result((None, "EXC"), (10, None), (30, None))])
        self.assertEqual((found.highest, found.average), (40, Decimal(50)))


class ExcusedNoteTests(SimpleTestCase):
    def test_the_note_says_total_obtainable_and_the_whole_percentage(self):
        self.assertEqual(
            excused_note(result((None, "EXC"), (15, None), (46, None))),
            "61 out of 80 obtainable, graded at 76%",
        )

    def test_there_is_no_note_without_an_excused_component_or_without_a_total(self):
        self.assertIsNone(excused_note(result((10, None), (15, None), (46, None))))
        self.assertIsNone(excused_note(result((None, "EXC"), (None, None), (46, None))))

    def test_a_row_excused_from_everything_is_not_graded(self):
        self.assertEqual(
            excused_note(result((None, "EXC"), (None, "EXC"), (None, "EXC"))),
            "Excused from every component, so not graded.",
        )

    def test_whole_percent_rounds_half_up(self):
        self.assertEqual(whole_percent(Decimal("76.5")), 77)
        self.assertEqual(whole_percent(Decimal("76.49")), 76)
