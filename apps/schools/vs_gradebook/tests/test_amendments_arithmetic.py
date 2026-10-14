"""How a corrected snapshot is worked out, with no database.

The rebuild is a pure function of the snapshot being corrected, the cells, the
classmates' snapshots and the school's remark templates, so every rule of an
amendment's arithmetic is proved here by calling it: the position against the
classmates' published averages (including the big class where a student moves
from 12th to 9th and shares the place with a classmate), the class figures of
the amended subject for one card only, the totals and average, and what the
rebuild carries over untouched.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from schools.vs_gradebook.services import amendments, compute, publishing
from schools.vs_gradebook.services.classbook import percentage_of

NOW = dt.datetime(2026, 1, 20, 9, 0, tzinfo=dt.timezone.utc)
COMPONENTS = (
    {"id": 1, "name": "1st CA", "short_name": "CA1", "max_score": 20, "is_exam": False},
    {"id": 2, "name": "2nd CA", "short_name": "CA2", "max_score": 20, "is_exam": False},
    {"id": 3, "name": "Examination", "short_name": "EXAM", "max_score": 60, "is_exam": True},
)
KEY = (
    {"min_score": 70, "max_score": 100, "grade": "A", "remark": "Excellent", "is_fail": False},
    {"min_score": 60, "max_score": 69, "grade": "B", "remark": "Very good", "is_fail": False},
    {"min_score": 50, "max_score": 59, "grade": "C", "remark": "Good", "is_fail": False},
    {"min_score": 40, "max_score": 49, "grade": "D", "remark": "Fair", "is_fail": False},
    {"min_score": 0, "max_score": 39, "grade": "F", "remark": "Fail", "is_fail": True},
)
TEMPLATES = [(70, "Excellent."), (55, "Good."), (40, "Fair."), (0, "Needs attention.")]
MATHS, ENGLISH = {"id": 10, "name": "Mathematics"}, {"id": 11, "name": "English"}


def components():
    return [SimpleNamespace(pk=c["id"], max_score=c["max_score"]) for c in COMPONENTS]


def bands():
    return [compute.Band(b["min_score"], b["grade"], b["remark"], b["is_fail"]) for b in KEY]


def subject_row(subject, cells, class_stats=None):
    """A subject's snapshot row for *cells* ``(CA1, CA2, exam)``, as publication builds it."""
    result = compute.row_result(components(), cells, bands())
    stats = class_stats or compute.class_stats([result.total])
    return publishing.subject_row(subject, components(), cells, result, stats)


def snapshot(maths, english, *, position=None, comment="Fair.", roll_size=20):
    """A student's published snapshot from their cells in the two subjects."""
    rows = [subject_row(MATHS, maths), subject_row(ENGLISH, english)]
    percentages = [
        compute.row_result(components(), cells, bands()).percentage for cells in (maths, english)
    ]
    average = compute.overall_average(percentages)
    obtained, obtainable = publishing.snapshot_totals(rows)
    out = {
        "version": 1, "published_at": NOW.isoformat(),
        "components": list(COMPONENTS), "grading_key": list(KEY), "subjects": rows,
        "total_obtained": obtained, "total_obtainable": obtainable,
        "overall_average": percentage_of(average), "principal_comment": comment,
        "teacher_comment": "Pleasant.", "attendance": {"opened": 60, "present": 55, "absent": 5},
    }
    if position is not None:
        out.update(position=position, roll_size=roll_size)
    return out


def cells(a, b, c):
    return [(a, None), (b, None), (c, None)]


def frozen(value):
    return json.dumps(value, sort_keys=True, default=str)


class OverallFiguresTests(SimpleTestCase):
    def setUp(self):
        self.old = snapshot(cells(10, 10, 20), cells(15, 15, 45), position=2)
        self.classmates = [snapshot(cells(18, 15, 46), cells(10, 10, 30), position=1)]

    def corrected(self, new_maths, classmates=None, templates=TEMPLATES, overrides=None):
        return amendments.corrected_snapshot(
            self.old, MATHS["id"], new_maths, self.classmates if classmates is None else classmates,
            templates, overrides or {}, NOW,
        )

    def test_the_subject_totals_and_average_follow_the_cells(self):
        new = self.corrected(cells(10, 10, 50))
        maths = new["subjects"][0]
        self.assertEqual(
            (maths["total"], maths["percentage"], maths["grade"], maths["remark"]),
            (70, "70.00", "A", "Excellent"),
        )
        self.assertEqual((new["total_obtained"], new["total_obtainable"]), (145, 200))
        # (70 + 75) / 2
        self.assertEqual(new["overall_average"], "72.50")

    def test_the_average_uses_unrounded_percentages_as_publication_does(self):
        # 58 of 80 obtainable (the second assessment excused) is 72.5 percent exactly.
        new = self.corrected([(10, None), (None, "EXC"), (48, None)])
        maths = new["subjects"][0]
        self.assertEqual((maths["total"], maths["obtainable"], maths["percentage"]), (58, 80, "72.50"))
        self.assertEqual(new["overall_average"], "73.75")

    def test_a_subject_excused_from_everything_is_left_out_of_the_average(self):
        new = self.corrected([(None, "EXC"), (None, "EXC"), (None, "EXC")])
        self.assertIsNone(new["subjects"][0]["percentage"])
        self.assertEqual(new["overall_average"], "75.00")

    def test_the_version_is_one_more_and_the_original_publication_time_stands(self):
        new = self.corrected(cells(10, 10, 50))
        self.assertEqual(new["version"], 2)
        self.assertEqual(new["published_at"], self.old["published_at"])
        self.assertEqual(new["amended_at"], NOW.isoformat())

    def test_what_the_rebuild_does_not_work_out_is_carried_over(self):
        new = self.corrected(cells(10, 10, 50))
        for key in ("components", "grading_key", "teacher_comment", "attendance"):
            self.assertEqual(frozen(new[key]), frozen(self.old[key]))
        self.assertEqual(frozen(new["subjects"][1]), frozen(self.old["subjects"][1]))

    def test_the_snapshot_and_the_classmates_handed_in_are_not_changed(self):
        before_old = frozen(self.old)
        before_mates = frozen(self.classmates)
        self.corrected(cells(10, 10, 50))
        self.assertEqual(frozen(self.old), before_old)
        self.assertEqual(frozen(self.classmates), before_mates)


class PositionTests(SimpleTestCase):
    """A class of twenty: the student is 12th and a correction lifts them to 9th, tied with a classmate."""

    def setUp(self):
        # Classmates' published averages: eight above 80, two on exactly 70 (9th, 9th), one
        # on 69, then the rest below the student.
        above = [(18, 18, 54), (18, 17, 53), (17, 17, 53), (17, 17, 52), (17, 16, 52),
                 (16, 16, 52), (16, 16, 51), (16, 16, 50)]
        self.classmates = [
            snapshot(cells(*row), cells(*row), position=n + 1) for n, row in enumerate(above)
        ]
        # 14 + 14 + 42 = 70 in both subjects: an average of exactly 70.00.
        self.classmates += [snapshot(cells(14, 14, 42), cells(14, 14, 42), position=9)] * 2
        # 69 in both subjects.
        self.classmates += [snapshot(cells(14, 14, 41), cells(14, 14, 41), position=11)]
        self.classmates += [
            snapshot(cells(10, 10, 30), cells(10, 10, 30), position=12 + n) for n in range(8)
        ]
        # The student: mathematics 60 and English 77, an average of 68.50, and so 12th.
        self.old = snapshot(cells(12, 12, 36), cells(15, 15, 47), position=12)

    def corrected(self, new_maths):
        return amendments.corrected_snapshot(
            self.old, MATHS["id"], new_maths, self.classmates, TEMPLATES, {}, NOW,
        )

    def test_the_student_is_twelfth_before_the_correction(self):
        self.assertEqual(self.old["overall_average"], "68.50")
        higher = [c for c in self.classmates if Decimal(c["overall_average"]) > Decimal("68.50")]
        self.assertEqual(len(higher), 11)

    def test_a_correction_that_ties_a_classmate_shares_their_place_and_moves_nobody(self):
        # Mathematics 63 and English 77 average exactly 70.00.
        before = frozen(self.classmates)
        new = self.corrected(cells(13, 14, 36))
        self.assertEqual(new["overall_average"], "70.00")
        self.assertEqual(new["position"], 9)
        self.assertEqual(new["roll_size"], 20)
        self.assertEqual([c["position"] for c in self.classmates[8:10]], [9, 9])
        self.assertEqual(frozen(self.classmates), before)

    def test_a_correction_above_everybody_is_first(self):
        self.old = snapshot(cells(12, 12, 36), cells(20, 20, 60), position=2)
        new = self.corrected(cells(20, 20, 60))
        self.assertEqual(new["overall_average"], "100.00")
        self.assertEqual(new["position"], 1)

    def test_a_correction_below_everybody_is_last(self):
        self.old = snapshot(cells(18, 18, 54), cells(18, 18, 54), position=1)
        new = self.corrected(cells(0, 0, 0))
        # (0 + 90) / 2 = 45, below all nineteen classmates and level with none.
        self.assertEqual(new["overall_average"], "45.00")
        self.assertEqual(new["position"], 20)

    def test_a_school_that_does_not_rank_gets_no_position(self):
        self.old = snapshot(cells(12, 12, 36), cells(15, 15, 47))
        self.assertNotIn("position", self.corrected(cells(13, 14, 36)))


class ClassFiguresTests(SimpleTestCase):
    def setUp(self):
        self.old = snapshot(cells(10, 10, 20), cells(15, 15, 45), position=4)
        self.mates = [
            snapshot(cells(18, 15, 46), cells(10, 10, 30), position=1),
            snapshot(cells(15, 15, 45), cells(15, 15, 45), position=2),
            snapshot(cells(15, 15, 30), cells(10, 10, 30), position=3),
        ]

    def maths(self, new_maths):
        found = amendments.corrected_snapshot(
            self.old, MATHS["id"], new_maths, self.mates, TEMPLATES, {}, NOW,
        )["subjects"][0]
        return found["class_average"], found["highest"], found["lowest"], found["total"]

    def test_the_figures_are_the_classmates_published_totals_and_the_new_total(self):
        # 79, 75 and 60 with the new 70.
        self.assertEqual(self.maths(cells(10, 10, 50)), ("71.0", 79, 60, 70))

    def test_a_new_total_above_everyone_is_the_highest(self):
        average, highest, lowest, total = self.maths(cells(20, 20, 60))
        self.assertEqual((highest, total), (100, 100))
        self.assertEqual(lowest, 60)

    def test_a_new_total_below_everyone_is_the_lowest(self):
        average, highest, lowest, total = self.maths(cells(0, 0, 5))
        self.assertEqual((highest, lowest, total), (79, 5, 5))

    def test_only_this_subjects_figures_change(self):
        found = amendments.corrected_snapshot(
            self.old, MATHS["id"], cells(10, 10, 50), self.mates, TEMPLATES, {}, NOW,
        )
        self.assertEqual(frozen(found["subjects"][1]), frozen(self.old["subjects"][1]))

    def test_a_classmate_who_does_not_take_the_subject_adds_nothing(self):
        solo = snapshot(cells(10, 10, 20), cells(15, 15, 45))
        solo["subjects"] = [solo["subjects"][1]]
        self.mates.append(solo)
        self.assertEqual(self.maths(cells(10, 10, 50)), ("71.0", 79, 60, 70))


class PrincipalCommentTests(SimpleTestCase):
    def setUp(self):
        self.old = snapshot(cells(10, 10, 20), cells(10, 10, 20), position=1, comment="Fair.")

    def corrected(self, new_maths, overrides=None):
        return amendments.corrected_snapshot(
            self.old, MATHS["id"], new_maths, [], TEMPLATES, overrides or {}, NOW,
        )

    def test_the_published_comment_stands_while_the_band_does(self):
        self.assertEqual(self.corrected(cells(10, 10, 25))["principal_comment"], "Fair.")

    def test_the_comment_of_the_new_band_is_used_when_the_average_moves_into_one(self):
        # 40 and 80 average 60, the 55 band.
        self.assertEqual(self.corrected(cells(20, 20, 40))["principal_comment"], "Good.")

    def test_a_comment_the_principal_wrote_for_the_class_wins_over_the_template(self):
        found = self.corrected(cells(20, 20, 40), overrides={55: "Splendid effort."})
        self.assertEqual(found["principal_comment"], "Splendid effort.")
