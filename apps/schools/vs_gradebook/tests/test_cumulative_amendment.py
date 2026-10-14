"""An approved correction keeps the cumulative block right.

The year block of the last term's card is a function of that card's own term
figures and the earlier terms' figures it already holds, so a correction to the
last term recomputes the amended student's subject and overall annual averages
and their annual position against their classmates' published annual averages.
No classmate's block moves, the version it replaces is kept, and a correction to
a term that is not cumulative adds no block.
"""
from __future__ import annotations

import copy
import json

from django.test import SimpleTestCase

from schools.vs_gradebook.models import ResultVersion, StudentTermResult
from schools.vs_gradebook.services import cumulative

from .cumulative_base import CumulativeFixture


def frozen(value):
    return json.dumps(value, sort_keys=True)


class CorrectionToTheLastTermTests(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_year()

    def amend(self, student, exam):
        """Correct *student*'s third-term Mathematics examination to *exam* and approve it."""
        found = self.raised(
            student, cells=self.cells(None, None, exam), term=self.third_term.pk,
        )
        response = self.approve_amendment(self.approver, found)
        self.assertEqual(response.status_code, 200, response.data)

    def maths_of(self, block):
        return next(r for r in block["subjects"] if r["subject"]["name"] == "Mathematics")

    def test_a_lower_mark_recomputes_the_subjects_annual_average_the_overall_and_the_position(self):
        self.amend(self.tunde, 0)
        block = self.stored(self.tunde)["cumulative"]
        maths = self.maths_of(block)
        self.assertEqual([t["total"] for t in maths["terms"]], [75, 50, 30])
        self.assertEqual([t["percentage"] for t in maths["terms"]], ["75.00", "50.00", "30.00"])
        self.assertEqual(maths["annual_average"], "51.67")
        self.assertEqual(block["overall"]["terms"][2]["average"], "45.00")
        self.assertEqual(block["overall"]["annual_average"], "53.33")
        self.assertEqual(block["annual_position"], 4)
        self.assertEqual(block["annual_position_of"], 4)

    def test_the_block_agrees_with_the_term_figures_of_the_same_snapshot(self):
        self.amend(self.tunde, 0)
        snapshot = self.stored(self.tunde)
        self.assertEqual(snapshot["version"], 2)
        self.assertEqual(
            snapshot["cumulative"]["overall"]["terms"][2]["average"], snapshot["overall_average"],
        )

    def test_the_earlier_terms_and_the_other_subjects_are_carried_over(self):
        before = self.stored(self.tunde)["cumulative"]
        self.amend(self.tunde, 0)
        after = self.stored(self.tunde)["cumulative"]
        self.assertEqual(after["terms"], before["terms"])
        french = lambda block: next(  # noqa: E731
            r for r in block["subjects"] if r["subject"]["name"] == "French"
        )
        self.assertEqual(french(after), french(before))
        self.assertEqual(
            self.maths_of(after)["terms"][:2], self.maths_of(before)["terms"][:2],
        )
        self.assertEqual(after["overall"]["terms"][:2], before["overall"]["terms"][:2])

    def test_the_card_prints_the_corrected_year(self):
        self.amend(self.tunde, 0)
        card = self.last_card(self.tunde)
        maths = next(s for s in card["cumulative"]["subjects"] if s["name"] == "Mathematics")
        self.assertEqual(maths["totals"], ["75", "50", "30"])
        self.assertEqual(maths["annual_average"], "51.67")
        self.assertEqual(card["cumulative"]["annual_average"], "53.33")
        self.assertEqual(card["cumulative"]["annual_position"], "4th of 4")
        self.assertEqual(card["version"], 2)

    def test_the_position_shares_a_place_with_a_classmate_published_on_the_same_average(self):
        self.amend(self.kelechi, 45)
        block = self.stored(self.kelechi)["cumulative"]
        self.assertEqual(block["overall"]["annual_average"], "60.00")
        self.assertEqual(block["annual_position"], 2)

    def test_no_classmates_block_moves(self):
        before = {s: frozen(self.stored(s)) for s in (self.chiamaka, self.kelechi, self.amina)}
        self.amend(self.tunde, 0)
        after = {s: frozen(self.stored(s)) for s in (self.chiamaka, self.kelechi, self.amina)}
        self.assertEqual(before, after)
        self.assertEqual(self.stored(self.amina)["cumulative"]["annual_position"], 2)

    def test_the_version_it_replaces_keeps_its_block(self):
        before = frozen(self.stored(self.tunde))
        self.amend(self.tunde, 0)
        result = StudentTermResult.objects.get(
            student=self.tunde, class_term__term=self.third_term,
        )
        first = ResultVersion.objects.get(result=result, version=1)
        self.assertEqual(frozen(first.snapshot), before)
        self.assertEqual(first.snapshot["cumulative"]["annual_position"], 2)


class CorrectionToAnEarlierTermTests(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_year()

    def test_a_term_that_is_not_cumulative_gains_no_block(self):
        found = self.raised(self.tunde, cells=self.cells(None, None, 50))
        self.assertEqual(self.approve_amendment(self.approver, found).status_code, 200)
        self.assertNotIn("cumulative", self.stored(self.tunde, self.first_term))
        self.assertEqual(self.stored(self.tunde, self.first_term)["version"], 2)


class CorrectionToAnEarlierTermReachesTheCumulativeCardTests(CumulativeFixture):
    """Amending term 1 after term 3 is published reissues the same student's term 3 card."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_year()

    def updated(self):
        return [
            c for c in self.send.call_args_list
            if c.kwargs["event_key"] == "gradebook.report_card_updated"
        ]

    def amend_first_term(self, student, exam=60):
        found = self.raised(student, cells=self.cells(None, None, exam))
        response = self.approve_amendment(self.approver, found)
        self.assertEqual(response.status_code, 200, response.data)
        return found

    def third(self, student):
        return StudentTermResult.objects.get(
            student=student, class_term__school_class=self.jss1a,
            class_term__term=self.third_term,
        )

    def maths_of(self, block):
        return next(r for r in block["subjects"] if r["subject"]["name"] == "Mathematics")

    def test_the_third_term_card_carries_the_corrected_first_term_and_annual_figures(self):
        self.amend_first_term(self.tunde, 60)
        snapshot = self.stored(self.tunde)
        self.assertEqual(snapshot["version"], 2)
        block = snapshot["cumulative"]
        maths = self.maths_of(block)
        self.assertEqual([t["total"] for t in maths["terms"]], [90, 50, 70])
        self.assertEqual(maths["annual_average"], "70.00")
        self.assertEqual(block["overall"]["terms"][0]["average"], "82.50")
        self.assertEqual(block["overall"]["annual_average"], "62.50")
        self.assertEqual(block["annual_position"], 2)

    def test_only_the_cumulative_block_and_the_version_move_on_that_card(self):
        before = copy.deepcopy(self.stored(self.tunde))
        self.amend_first_term(self.tunde, 60)
        after = self.stored(self.tunde)
        for key in set(before) | set(after):
            if key not in ("cumulative", "version", "amended_at"):
                self.assertEqual(before.get(key), after.get(key), key)

    def test_the_old_version_is_kept_and_both_versions_are_linked_to_the_amendment(self):
        before = frozen(self.stored(self.tunde))
        found = self.amend_first_term(self.tunde, 60)
        result = self.third(self.tunde)
        first = ResultVersion.objects.get(result=result, version=1)
        second = ResultVersion.objects.get(result=result, version=2)
        self.assertEqual(frozen(first.snapshot), before)
        self.assertIsNone(first.amendment_id)
        self.assertEqual(second.amendment_id, found)
        self.assertEqual(frozen(second.snapshot), frozen(result.snapshot))
        own = ResultVersion.objects.get(
            result__student=self.tunde, result__class_term__term=self.first_term, version=2,
        )
        self.assertEqual(own.amendment_id, found)

    def test_no_classmates_third_term_card_changes(self):
        before = {
            s: frozen(self.stored(s)) for s in (self.chiamaka, self.kelechi, self.amina)
        }
        self.amend_first_term(self.tunde, 60)
        after = {s: frozen(self.stored(s)) for s in (self.chiamaka, self.kelechi, self.amina)}
        self.assertEqual(before, after)
        for student in (self.chiamaka, self.kelechi, self.amina):
            self.assertEqual(self.third(student).version, 1)
            self.assertEqual(self.third(student).versions.count(), 1)

    def test_the_position_is_worked_out_against_the_classmates_published_annual_averages(self):
        self.amend_first_term(self.kelechi, 60)
        block = self.stored(self.kelechi)["cumulative"]
        self.assertEqual(block["overall"]["annual_average"], "68.33")
        self.assertEqual(block["annual_position"], 2)

    def test_the_card_prints_the_corrected_year_at_the_new_version(self):
        self.amend_first_term(self.tunde, 60)
        card = self.last_card(self.tunde)
        self.assertEqual(card["version"], 2)
        maths = next(s for s in card["cumulative"]["subjects"] if s["name"] == "Mathematics")
        self.assertEqual(maths["totals"], ["90", "50", "70"])

    def test_both_cards_released_sends_the_guardian_two_updated_emails(self):
        self.amend_first_term(self.chiamaka, 60)
        terms = sorted(c.kwargs["context"]["term_name"] for c in self.updated())
        self.assertEqual(terms, ["First Term", "Third Term"])

    def test_a_held_third_term_card_is_reissued_but_only_the_first_term_is_emailed(self):
        StudentTermResult.objects.filter(
            student=self.chiamaka, class_term__term=self.third_term,
        ).update(released_at=None)
        self.amend_first_term(self.chiamaka, 60)
        self.assertEqual(self.third(self.chiamaka).version, 2)
        self.assertEqual([c.kwargs["context"]["term_name"] for c in self.updated()], ["First Term"])

    def test_a_correction_that_leaves_the_year_figures_unchanged_reissues_nothing_later(self):
        found = self.raised(self.tunde, cells=self.cells(20, 10, None))
        self.assertEqual(self.approve_amendment(self.approver, found).status_code, 200)
        self.assertEqual(self.third(self.tunde).version, 1)
        self.assertEqual(self.third(self.tunde).versions.count(), 1)
        self.assertEqual(self.stored(self.tunde, self.first_term)["version"], 2)

    def test_the_amended_terms_own_card_reads_its_own_version_in_the_listing(self):
        self.amend_first_term(self.tunde, 60)
        rows = self.data(self.amendments_list(self.approver))
        self.assertEqual([row["version"] for row in rows], [2])


class RefreshedBlockTests(SimpleTestCase):
    """The arithmetic on its own, with no database."""

    @staticmethod
    def block(*, ranked=True):
        block = {
            "terms": [{"id": 1, "name": "First"}, {"id": 2, "name": "Second"}],
            "subjects": [{
                "subject": {"id": 9, "name": "Maths"},
                "terms": [
                    {"term_id": 1, "total": 80, "percentage": "80.00"},
                    {"term_id": 2, "total": 60, "percentage": "60.00"},
                ],
                "annual_average": "70.00",
            }],
            "overall": {
                "terms": [{"term_id": 1, "average": "80.00"}, {"term_id": 2, "average": "60.00"}],
                "annual_average": "70.00",
            },
        }
        if ranked:
            block["annual_position"], block["annual_position_of"] = 2, 3
        return block

    @staticmethod
    def classmate(annual):
        return {"overall": {"annual_average": annual}}

    def refresh(self, block, total, average, classmates):
        subjects = [{
            "subject": {"id": 9, "name": "Maths"}, "total": total,
            "percentage": f"{total}.00" if total is not None else None,
        }]
        return cumulative.refreshed(block, 2, subjects, average, classmates)

    def test_the_term_entry_and_both_annual_averages_are_worked_out_again(self):
        new = self.refresh(self.block(), 90, "90.00", [])
        self.assertEqual(new["subjects"][0]["terms"][1], {
            "term_id": 2, "total": 90, "percentage": "90.00",
        })
        self.assertEqual(new["subjects"][0]["annual_average"], "85.00")
        self.assertEqual(new["overall"]["annual_average"], "85.00")

    def test_the_block_handed_in_is_not_changed(self):
        block = self.block()
        before = copy.deepcopy(block)
        self.refresh(block, 90, "90.00", [])
        self.assertEqual(block, before)

    def test_the_position_is_against_the_classmates_published_annual_averages(self):
        classmates = [self.classmate("90.00"), self.classmate("85.00"), self.classmate("50.00")]
        self.assertEqual(self.refresh(self.block(), 90, "90.00", classmates)["annual_position"], 2)
        self.assertEqual(self.refresh(self.block(), 100, "100.00", classmates)["annual_position"], 1)
        self.assertEqual(self.refresh(self.block(), 10, "10.00", classmates)["annual_position"], 4)

    def test_a_tie_shares_a_place(self):
        tied = [self.classmate("85.00"), self.classmate("85.00")]
        self.assertEqual(self.refresh(self.block(), 90, "90.00", tied)["annual_position"], 1)
        ahead = [*tied, self.classmate("99.00")]
        self.assertEqual(self.refresh(self.block(), 90, "90.00", ahead)["annual_position"], 2)

    def test_a_classmate_with_no_annual_average_is_not_ranked_against(self):
        classmates = [self.classmate(None), self.classmate("50.00")]
        self.assertEqual(self.refresh(self.block(), 90, "90.00", classmates)["annual_position"], 1)

    def test_a_school_that_does_not_rank_gains_no_position(self):
        new = self.refresh(self.block(ranked=False), 90, "90.00", [self.classmate("99.00")])
        self.assertNotIn("annual_position", new)
        self.assertNotIn("annual_position_of", new)

    def test_a_student_left_with_no_average_has_no_position(self):
        block = self.block()
        block["subjects"][0]["terms"][0] = {"term_id": 1, "total": None, "percentage": None}
        block["overall"]["terms"][0]["average"] = None
        new = self.refresh(block, None, None, [self.classmate("50.00")])
        self.assertIsNone(new["subjects"][0]["annual_average"])
        self.assertIsNone(new["overall"]["annual_average"])
        self.assertIsNone(new["annual_position"])
