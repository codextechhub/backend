"""What performance analytics says: the figures, their order and their empty shapes.

Every number asserted here is worked out by hand in :mod:`.analytics_base`'s
docstring. Access and branch scope are in :mod:`.test_analytics_access`.
"""
from __future__ import annotations

import datetime as dt
import json

from django.db import connection
from django.test.utils import CaptureQueriesContext

from schools.vs_academics.models import AcademicTerm
from schools.vs_gradebook.constants import CFG_RANK_IN_CLASS

from ..services import analytics
from .analytics_base import AnalyticsFixture, snapshot
from .base import set_setting


class SubjectRowTests(AnalyticsFixture):
    def setUp(self):
        super().setUp()
        self.body = self.data(self.analytics(self.reader))

    def test_the_rows_are_ordered_by_subject_then_class(self):
        self.assertEqual(
            [(r["subject"]["name"], r["class"]["name"]) for r in self.body["subjects"]],
            [
                ("French", "JSS1 A"), ("Mathematics", "JSS1 A"),
                ("Mathematics", "JSS1 B"), ("Mathematics", "JSS1 C"),
            ],
        )

    def test_a_subject_has_the_average_pass_rate_and_count_marked(self):
        row = self.subject_row(self.body, self.maths, self.jss1a)
        self.assertEqual((row["average"], row["pass_rate"], row["marked"]), ("60.00", 75, 4))
        row = self.subject_row(self.body, self.french, self.jss1a)
        self.assertEqual((row["average"], row["pass_rate"], row["marked"]), ("65.00", 100, 2))
        row = self.subject_row(self.body, self.maths, self.jss1b)
        self.assertEqual((row["average"], row["pass_rate"], row["marked"]), ("47.50", 100, 2))

    def test_the_spread_is_in_scale_order_and_names_a_grade_nobody_earned(self):
        row = self.subject_row(self.body, self.maths, self.jss1a)
        self.assertEqual(
            [(e["grade"], e["count"]) for e in row["spread"]],
            [("A", 2), ("B", 1), ("C", 0), ("D", 0), ("F", 1)],
        )
        row = self.subject_row(self.body, self.maths, self.jss1b)
        self.assertEqual(
            [(e["grade"], e["count"]) for e in row["spread"]],
            [("A", 0), ("B", 0), ("C", 1), ("D", 1), ("F", 0)],
        )

    def test_a_held_students_result_counts_because_published_is_published(self):
        self.assertEqual(self.subject_row(self.body, self.maths, self.jss1a)["marked"], 4)

    def test_the_pass_rate_rounds_to_a_whole_percentage_with_halves_up(self):
        pupils = [
            self.chiamaka, self.tunde, self.kelechi, self.amina, self.sade, self.ife,
            self.bisi, self.uche,
        ]
        self.put(self.sss1a, self.second_term, [
            (p, snapshot(p, self.sss1a, self.second_term, [(self.maths, 80 if n == 0 else 10)],
                         average=10))
            for n, p in enumerate(pupils)
        ])
        body = self.data(self.analytics(self.reader, term=self.second_term.pk))
        row = self.subject_row(body, self.maths, self.sss1a)
        self.assertEqual((row["marked"], row["pass_rate"]), (8, 13))

    def test_a_subject_nobody_was_graded_in_has_no_figures_and_a_marked_of_zero(self):
        self.put(self.sss1a, self.second_term, [
            (self.zainab, snapshot(self.zainab, self.sss1a, self.second_term, [(self.maths, None)], average=None)),
        ])
        body = self.data(self.analytics(self.reader, term=self.second_term.pk))
        row = self.subject_row(body, self.maths, self.sss1a)
        self.assertEqual((row["average"], row["pass_rate"], row["marked"]), (None, None, 0))
        self.assertEqual([e["count"] for e in row["spread"]], [0, 0, 0, 0, 0])


class CompareTests(AnalyticsFixture):
    def test_a_subject_in_several_published_classes_has_each_average_and_the_gap(self):
        [row] = self.data(self.analytics(self.reader))["compare"]
        self.assertEqual(row["subject"]["name"], "Mathematics")
        self.assertEqual(
            [(arm["class"]["name"], arm["average"]) for arm in row["arms"]],
            [("JSS1 A", "60.00"), ("JSS1 B", "47.50"), ("JSS1 C", "80.00")],
        )
        self.assertEqual(row["gap"], "32.50")

    def test_a_subject_in_one_class_is_not_compared(self):
        names = [row["subject"]["name"] for row in self.data(self.analytics(self.reader))["compare"]]
        self.assertNotIn("French", names)


class MoverTests(AnalyticsFixture):
    def setUp(self):
        super().setUp()
        self.movers = self.data(self.analytics(self.reader))["movers"]

    def test_the_biggest_fall_comes_first(self):
        self.assertEqual(
            [(m["student"]["name"], m["change"]) for m in self.movers],
            [("Sade Ojo", "-9.50"), ("Tunde Bello", "-5.00"), ("Chiamaka Nwosu", "+10.00")],
        )

    def test_a_row_names_the_term_compared_with_and_both_averages(self):
        tunde = next(m for m in self.movers if m["student"]["name"] == "Tunde Bello")
        self.assertEqual(
            (tunde["from"], tunde["to"], tunde["from_term"]["name"], tunde["class"]["name"]),
            ("75.00", "70.00", "Third Term", "JSS1 A"),
        )
        self.assertEqual(tunde["student"]["admission_number"], "BFS/002")

    def test_the_comparison_crosses_a_school_year_and_takes_the_most_recent_term(self):
        tunde = next(m for m in self.movers if m["student"]["name"] == "Tunde Bello")
        self.assertEqual(tunde["from_term"]["id"], self.old_third.pk)
        self.assertNotEqual(tunde["from_term"]["id"], self.old_second.pk)

    def test_a_term_with_no_average_is_passed_over_for_the_one_before_it(self):
        sade = next(m for m in self.movers if m["student"]["name"] == "Sade Ojo")
        self.assertEqual((sade["from_term"]["id"], sade["from"]), (self.old_second.pk, "99.50"))

    def test_a_student_with_no_earlier_published_term_is_left_out(self):
        names = {m["student"]["name"] for m in self.movers}
        self.assertFalse(names & {"Kelechi Eze", "Amina Yusuf", "Bisi Ade", "Uche Obi", "Ife Adeola"})

    def test_a_later_term_is_never_compared_with(self):
        self.assertNotIn(self.second_term.pk, {m["from_term"]["id"] for m in self.movers})
        second = self.data(self.analytics(self.reader, term=self.second_term.pk))["movers"]
        [tunde] = second
        self.assertEqual((tunde["from_term"]["id"], tunde["from"], tunde["to"]), (
            self.first_term.pk, "70.00", "10.00",
        ))
        self.assertEqual(tunde["change"], "-60.00")

    def test_an_unchanged_average_has_no_sign(self):
        self.put(self.sss1a, self.second_term, [
            (self.chiamaka, snapshot(self.chiamaka, self.sss1a, self.second_term, [(self.maths, 70)], average=70)),
        ])
        movers = self.data(self.analytics(self.reader, term=self.second_term.pk))["movers"]
        chiamaka = next(m for m in movers if m["student"]["name"] == "Chiamaka Nwosu")
        self.assertEqual(chiamaka["change"], "0.00")


class TopTests(AnalyticsFixture):
    def test_the_top_of_each_class_names_every_student_tied_on_it(self):
        tops = self.data(self.analytics(self.reader))["tops"]
        self.assertEqual(
            [(t["class"]["name"], [s["name"] for s in t["students"]], t["average"]) for t in tops],
            [
                ("JSS1 A", ["Chiamaka Nwosu", "Tunde Bello"], "70.00"),
                ("JSS1 B", ["Bisi Ade"], "50.00"),
                ("JSS1 C", ["Sade Ojo"], "90.00"),
            ],
        )

    def test_a_student_is_named_by_id_and_name_alone(self):
        [first, *_] = self.data(self.analytics(self.reader))["tops"]
        self.assertEqual(set(first["students"][0]), {"id", "name"})

    def test_a_school_that_does_not_rank_has_no_tops(self):
        set_setting(CFG_RANK_IN_CLASS, False, self.tenant, self.admin)
        body = self.data(self.analytics(self.reader))
        self.assertFalse(body["ranks"])
        self.assertEqual(body["tops"], [])
        self.assertTrue(body["subjects"])

    def test_a_class_published_without_positions_has_no_top(self):
        self.put(self.sss1a, self.second_term, [
            (self.zainab, snapshot(self.zainab, self.sss1a, self.second_term, [(self.maths, 88)], average=88)),
        ])
        body = self.data(self.analytics(self.reader, term=self.second_term.pk))
        self.assertTrue(body["ranks"])
        self.assertEqual(body["tops"], [])

    def test_the_school_ranks_by_default(self):
        self.assertTrue(self.data(self.analytics(self.reader))["ranks"])


class EmptyTests(AnalyticsFixture):
    def test_a_term_with_nothing_published_has_every_list_empty(self):
        empty = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=self.year, name="Third Term", order_index=3,
            start_date=dt.date(2026, 4, 20), end_date=dt.date(2026, 7, 17),
        )
        body = self.data(self.analytics(self.reader, term=empty.pk))
        for name in ("subjects", "compare", "movers", "tops"):
            with self.subTest(list=name):
                self.assertEqual(body[name], [])
        self.assertEqual(body["scope"]["classes"], [])
        self.assertEqual(body["scope"]["published_count"], 0)
        self.assertEqual(body["term"]["name"], "Third Term")

    def test_a_published_class_with_no_results_has_no_figures(self):
        self.put(self.jss1b, self.second_term, [])
        body = self.data(self.analytics(self.reader, term=self.second_term.pk))
        self.assertEqual(body["scope"]["published_count"], 2)
        self.assertEqual(
            {row["class"]["name"] for row in body["subjects"]}, {"JSS1 A"},
        )

    def test_the_term_defaults_to_the_current_one(self):
        self.assertEqual(self.data(self.analytics(self.reader))["term"]["name"], "First Term")


class NoTeacherTests(AnalyticsFixture):
    def test_no_teacher_is_named_anywhere_in_the_payload(self):
        raw = json.dumps(self.data(self.analytics(self.reader)))
        for name in ("Zephyr", "Quill", "Chidi Eze", "Sade Bello", "teacher"):
            with self.subTest(name=name):
                self.assertNotIn(name, raw)


class QueryCountTests(AnalyticsFixture):
    def count(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.analytics(self.reader).status_code, 200)
        return len(queries)

    def test_the_number_of_queries_does_not_grow_with_the_number_of_classes(self):
        self.count()
        few = self.count()
        for index in range(3):
            school_class = self.make_class(self.jss1, f"JSS1 X{index}", f"JSS1X{index}", self.lekki, None)
            pupils = [
                self.make_student("Pat", f"Extra{index}{n}", school_class, f"BFS/9{index}{n}")
                for n in range(2)
            ]
            self.put(school_class, self.first_term, [
                (p, snapshot(p, school_class, self.first_term, [(self.maths, 55), (self.french, 65)],
                             average=60, position=1))
                for p in pupils
            ])
        many = self.count()
        self.assertEqual(many, few)
        self.assertGreater(
            len(self.data(self.analytics(self.reader))["scope"]["classes"]), 3,
        )

    def test_the_report_itself_reads_in_eight_queries_or_fewer(self):
        placed_count = len(self.data(self.analytics(self.reader))["scope"]["classes"])
        self.assertEqual(placed_count, 3)
        with CaptureQueriesContext(connection) as queries:
            analytics.report(self.tenant, self.reader, self.first_term, None)
        self.assertLessEqual(len(queries), 8)
