"""Which parts of the year block on the last term's card a role reads.

Every figure in the block follows a switch that already exists: the total of
each term follows the sheet's figures switch, the annual averages follow the
overall average switch and the annual position the position switch, with the
promotion line on a switch of its own. A line a card prints from a closed switch
is gone, label and value, and so on the Results tab.
"""
from __future__ import annotations

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from schools.vs_students.constants import EnrolmentOutcome

from .cumulative_base import CumulativeFixture

AVERAGE = "gradebook.result.overall_average"
POSITION = "gradebook.result.position"
PROMOTION = "gradebook.result.promotion_line"
ROW_RESULT = "gradebook.sheet.row_result"


class _Switches(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("gradebook.result", "gradebook.sheet")
        cls.publish_year()
        cls.record_outcome(cls.tunde, EnrolmentOutcome.PROMOTED, cls.next_jss2a)

    def close(self, role, *keys):
        set_field_access(role, *keys, read=False, write=False)

    def card_block(self):
        return self.last_card(self.tunde)["cumulative"]

    def tab_block(self):
        tab = self.data(self.results_tab(self.result_reader, self.tunde))
        return next(t for t in tab["terms"] if t["term"]["id"] == self.last_term.pk)["cumulative"]

    def both(self):
        """The block as the card and the Results tab send it, for one test to hold to both."""
        return {"card": self.card_block(), "tab": self.tab_block()}

    def close_for_both(self, key):
        self.close(self.release_role, key)
        self.close(self.result_role, key)


class DefaultAccessTests(_Switches):
    def test_with_no_switch_closed_the_whole_block_is_there(self):
        for where, block in self.both().items():
            with self.subTest(where=where):
                self.assertEqual(
                    set(block),
                    {"terms", "subjects", "annual_average", "annual_position", "promotion_line"},
                )
                for subject in block["subjects"]:
                    self.assertEqual(set(subject), {"name", "totals", "annual_average"})


class PositionSwitchTests(_Switches):
    def test_hiding_the_position_removes_the_annual_position_line_and_keeps_the_rest(self):
        self.close_for_both(POSITION)
        for where, block in self.both().items():
            with self.subTest(where=where):
                self.assertNotIn("annual_position", block)
                self.assertEqual(block["annual_average"], "60.00")
                self.assertIn("promotion_line", block)
                self.assertEqual(block["subjects"][0]["totals"], ["75", "30", "60"])


class OverallAverageSwitchTests(_Switches):
    def test_hiding_the_overall_average_removes_every_annual_average(self):
        self.close_for_both(AVERAGE)
        for where, block in self.both().items():
            with self.subTest(where=where):
                self.assertNotIn("annual_average", block)
                for subject in block["subjects"]:
                    self.assertNotIn("annual_average", subject)
                    self.assertIn("totals", subject)
                self.assertIn("annual_position", block)

    def test_hiding_it_leaves_the_terms_own_overall_average_to_its_own_switch(self):
        self.close(self.result_role, AVERAGE)
        tab = self.data(self.results_tab(self.result_reader, self.tunde))
        entry = next(t for t in tab["terms"] if t["term"]["id"] == self.last_term.pk)
        self.assertNotIn("overall_average", entry)
        self.assertNotIn("annual_average", entry["cumulative"])


class RowResultSwitchTests(_Switches):
    def test_hiding_the_subject_figures_removes_the_term_totals_and_keeps_the_averages(self):
        self.close_for_both(ROW_RESULT)
        for where, block in self.both().items():
            with self.subTest(where=where):
                for subject in block["subjects"]:
                    self.assertNotIn("totals", subject)
                    self.assertIn("annual_average", subject)
                self.assertEqual(block["terms"], ["First Term", "Second Term", "Third Term"])
                self.assertEqual(block["annual_average"], "60.00")


class PromotionLineSwitchTests(_Switches):
    def test_hiding_the_promotion_line_removes_it_alone(self):
        self.close_for_both(PROMOTION)
        for where, block in self.both().items():
            with self.subTest(where=where):
                self.assertNotIn("promotion_line", block)
                self.assertIn("annual_position", block)
                self.assertIn("annual_average", block)


class OtherRolesAreUnaffectedTests(_Switches):
    def test_a_switch_closed_for_one_role_leaves_another_role_its_figures(self):
        self.close(self.release_role, POSITION, AVERAGE, ROW_RESULT, PROMOTION)
        block = self.tab_block()
        self.assertIn("annual_position", block)
        self.assertIn("promotion_line", block)
        self.assertEqual(self.card_block()["terms"], ["First Term", "Second Term", "Third Term"])
        self.assertEqual(
            set(self.card_block()), {"terms", "subjects"},
        )
