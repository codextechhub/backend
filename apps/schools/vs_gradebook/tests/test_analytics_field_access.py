"""Which analytics figures a role reads, on the screen and in the files.

Averages, grade spread, pass rates and the gap follow the sheet's class figures
(``gradebook.sheet.class_stats``); a student's ``from``, ``to`` and ``change``
follow the overall average; the top of each class follows the position and the
overall average together, the stricter winning. A figure a closed switch governs
is absent, never blank, and a file leaves the column out.
"""
from __future__ import annotations

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from ..constants import PERM_ANALYTICS_EXPORT, PERM_ANALYTICS_VIEW
from .analytics_base import AnalyticsFixture
from .base import make_key_role
from .test_analytics_export import read_csv, read_xlsx

CLASS_STATS = "gradebook.sheet.class_stats"
AVERAGE = "gradebook.result.overall_average"
POSITION = "gradebook.result.position"


class _Switches(AnalyticsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("gradebook.sheet", "gradebook.result")

    _counter = 0

    def reader_closed(self, *keys):
        """A school-wide reader of analytics whose role has Read off on each of *keys*."""
        type(self)._counter += 1
        role = make_key_role(
            self.tenant, f"Closed {self._counter}", f"closed_{self._counter}",
            (PERM_ANALYTICS_VIEW, PERM_ANALYTICS_EXPORT),
        )
        user = self.make_user(
            f"closed{self._counter}@brightfield.test", "Clo", f"Sed{self._counter}", role,
        )
        set_field_access(role, *keys, read=False, write=False)
        return user


class DefaultAccessTests(_Switches):
    def test_with_every_switch_open_every_figure_is_there(self):
        body = self.data(self.analytics(self.reader))
        self.assertEqual(
            set(body["subjects"][0]), {"subject", "class", "average", "spread", "pass_rate", "marked"},
        )
        self.assertEqual(set(body["compare"][0]), {"subject", "arms", "gap"})
        self.assertEqual(set(body["compare"][0]["arms"][0]), {"class", "average"})
        self.assertEqual(
            set(body["movers"][0]),
            {"student", "class", "from_term", "from", "to", "change"},
        )
        self.assertEqual(set(body["tops"][0]), {"class", "students", "average"})
        self.assertEqual(
            set(body), {"term", "scope", "ranks", "subjects", "compare", "movers", "tops"},
        )


class ClassStatsTests(_Switches):
    def setUp(self):
        super().setUp()
        self.body = self.data(self.analytics(self.reader_closed(CLASS_STATS)))

    def test_the_average_spread_and_pass_rate_are_gone_and_the_count_marked_stays(self):
        for row in self.body["subjects"]:
            self.assertEqual(set(row), {"subject", "class", "marked"})
        self.assertEqual(self.body["subjects"][1]["marked"], 4)

    def test_the_compared_averages_and_the_gap_are_gone_and_the_classes_stay(self):
        [row] = self.body["compare"]
        self.assertNotIn("gap", row)
        self.assertEqual(
            [arm for arm in row["arms"]],
            [{"class": {"id": c.pk, "name": c.name}} for c in (self.jss1a, self.jss1b, self.jss1c)],
        )

    def test_the_students_averages_and_the_tops_are_untouched(self):
        self.assertEqual(self.body["movers"][0]["change"], "-9.50")
        self.assertEqual(self.body["tops"][0]["average"], "70.00")

    def test_a_role_that_still_reads_them_is_unaffected(self):
        self.reader_closed(CLASS_STATS)
        self.assertIn("average", self.data(self.analytics(self.reader))["subjects"][0])


class OverallAverageTests(_Switches):
    def setUp(self):
        super().setUp()
        self.body = self.data(self.analytics(self.reader_closed(AVERAGE)))

    def test_from_to_and_change_are_gone_and_the_names_stay(self):
        for row in self.body["movers"]:
            self.assertEqual(set(row), {"student", "class", "from_term"})
        self.assertEqual(len(self.body["movers"]), 3)

    def test_the_movers_are_listed_by_name_because_their_order_would_state_the_changes(self):
        self.assertEqual(
            [m["student"]["name"] for m in self.body["movers"]],
            ["Chiamaka Nwosu", "Sade Ojo", "Tunde Bello"],
        )

    def test_the_tops_are_gone_because_each_states_an_average(self):
        self.assertNotIn("tops", self.body)

    def test_the_subject_figures_are_untouched(self):
        self.assertEqual(self.body["subjects"][1]["average"], "60.00")


class PositionTests(_Switches):
    def setUp(self):
        super().setUp()
        self.body = self.data(self.analytics(self.reader_closed(POSITION)))

    def test_hiding_the_position_removes_the_tops_entirely_and_keeps_the_averages(self):
        self.assertNotIn("tops", self.body)
        self.assertEqual(self.body["movers"][0]["change"], "-9.50")
        self.assertEqual(self.body["subjects"][1]["average"], "60.00")

    def test_the_movers_keep_their_order_of_change(self):
        self.assertEqual(
            [m["student"]["name"] for m in self.body["movers"]],
            ["Sade Ojo", "Tunde Bello", "Chiamaka Nwosu"],
        )


class EveryFigureClosedTests(_Switches):
    def test_with_all_three_closed_only_names_and_counts_remain(self):
        body = self.data(self.analytics(self.reader_closed(CLASS_STATS, AVERAGE, POSITION)))
        self.assertNotIn("tops", body)
        self.assertEqual({tuple(sorted(r)) for r in body["subjects"]}, {("class", "marked", "subject")})
        self.assertEqual({tuple(sorted(r)) for r in body["movers"]}, {("class", "from_term", "student")})
        self.assertEqual(body["scope"]["published_count"], 3)


class ExportColumnTests(_Switches):
    def test_a_closed_class_figures_switch_leaves_the_average_pass_rate_and_spread_out(self):
        user = self.reader_closed(CLASS_STATS)
        _, _, headers, rows = read_csv(self.export(user, table="subjects"))
        self.assertEqual(headers, ["Subject", "Class", "Branch", "Marked"])
        self.assertEqual(rows[1], ["Mathematics", "JSS1 A", "Lekki Branch", "4"])
        _, _, headers, rows = read_xlsx(self.export(user, table="subjects", export="xlsx"))
        self.assertEqual(headers, ["Subject", "Class", "Branch", "Marked"])
        self.assertEqual(len(rows), 4)

    def test_a_closed_overall_average_leaves_the_movers_as_names_alone_in_name_order(self):
        user = self.reader_closed(AVERAGE)
        _, _, headers, rows = read_csv(self.export(user, table="movers"))
        self.assertEqual(
            headers, ["Student", "Admission number", "Class", "Branch", "Compared with"],
        )
        self.assertEqual(
            [row[0] for row in rows], ["Chiamaka Nwosu", "Sade Ojo", "Tunde Bello"],
        )
        text = "".join("".join(row) for row in rows)
        for figure in ("99.50", "-9.50", "75.00", "+10.00"):
            self.assertNotIn(figure, text)

    def test_the_other_table_is_untouched_by_a_switch_that_is_not_its_own(self):
        user = self.reader_closed(AVERAGE)
        _, _, headers, _ = read_csv(self.export(user, table="subjects"))
        self.assertIn("Average", headers)
        user = self.reader_closed(CLASS_STATS)
        _, _, headers, _ = read_csv(self.export(user, table="movers"))
        self.assertIn("Change", headers)

    def test_a_role_that_may_read_everything_has_every_column(self):
        _, _, headers, _ = read_csv(self.export(self.reader, table="movers"))
        self.assertEqual(headers[-3:], ["Previous average", "Average this term", "Change"])
