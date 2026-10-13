"""The attendance report: one term, the whole school, counted the way the student's tab counts.

Monday 13 October 2025 is today and the term is shortened to begin on Monday 6
October, so it has two weeks in it: Wk 1 (6 to 10 October) and Wk 2 (the 13th).
Two classes carry the arithmetic, one in each branch:

* JSS1 A (Lekki): Chiamaka, Tunde and Kelechi on the roll, Amina suspended for
  the whole term, Obinna not placed until 20 October.
* JSS1 C (Ikeja, in its own ``JSS1`` level row): Ngozi and Ada.

The days:

====== ================================== ==================================
Day    JSS1 A (Lekki)                     JSS1 C (Ikeja)
====== ================================== ==================================
Mon 6  Chiamaka P, Tunde L, Kelechi P     Ngozi P, Ada P
Tue 7  closed (Lekki Open Day)            Ngozi A, Ada P
Wed 8  Chiamaka A, Tunde P, Kelechi E;    not taken
       Amina A (suspended, so uncounted)
Thu 9  closed (Founders' Day, everywhere) closed
Fri 10 not taken                          Ngozi P, Ada L
Mon 13 Chiamaka A, Tunde A, Kelechi P     not taken (today)
====== ================================== ==================================

JSS1 A: present 5 (1 late), absent 3, excused 1, rate 5/9 = 55.6, not taken 1.
JSS1 C: present 5 (1 late), absent 1, rate 5/6 = 83.3, not taken 2.
The level ``JSS1`` added up: present 10, absent 4, excused 1, 10/15 = 66.7, not
the average of 55.6 and 83.3.
"""
from __future__ import annotations

import datetime as dt

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_academics.models import AcademicTerm, Level, Program
from schools.vs_students.models import ClassEnrolment
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_branch,
    make_school,
    make_school_admin,
    set_field_access,
)

from ..constants import CFG_CHRONIC_THRESHOLD, PERM_REGISTER_VIEW, PERM_REPORT_VIEW
from ..models import ClassRegister, ReasonCode, StudentMark
from ..services.reasons import ensure_default_reason_codes
from ..services.student_tab import student_tab
from .base import (
    FOUNDERS_DAY,
    LAST_FRIDAY,
    LEKKI_OPEN_DAY,
    NOW,
    TODAY,
    AttendanceFixture,
    make_key_role,
    set_setting,
)

OCT_6 = dt.date(2025, 10, 6)
OCT_8 = dt.date(2025, 10, 8)


class _Report(AttendanceFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        AcademicTerm.all_objects.filter(pk=cls.first_term.pk).update(start_date=OCT_6)
        ensure_default_reason_codes(cls.tenant)
        cls.sick = ReasonCode.objects.get(tenant=cls.tenant, name="Sick")

        # Whole-school reader, and a reader pinned to each branch.
        reporter_role = make_key_role(
            cls.tenant, "Reporter", "reporter", (PERM_REGISTER_VIEW, PERM_REPORT_VIEW),
        )
        cls.reporter = make_school_admin(
            None, email="reports@brightfield.test", tenant=cls.tenant,
            first_name="Rita", last_name="Okon",
        )
        make_assignment(cls.tenant, cls.reporter, reporter_role, branch=None)
        cls.lekki_reporter = make_school_admin(
            cls.lekki, email="reports@lekki.test", first_name="Lola", last_name="Ade",
        )
        make_assignment(cls.tenant, cls.lekki_reporter, reporter_role, branch=cls.lekki)
        cls.ikeja_reporter = make_school_admin(
            cls.ikeja, email="reports@ikeja.test", first_name="Ike", last_name="Obi",
        )
        make_assignment(cls.tenant, cls.ikeja_reporter, reporter_role, branch=cls.ikeja)
        solo_role = make_key_role(
            cls.solo, "Reporter", "reporter", (PERM_REGISTER_VIEW, PERM_REPORT_VIEW),
        )
        cls.solo_reporter = make_school_admin(
            None, email="reports@sunrise.test", tenant=cls.solo,
            first_name="Soji", last_name="Eze",
        )
        make_assignment(cls.solo, cls.solo_reporter, solo_role, branch=None)

        # Ikeja's own JSS1, a different Level row with the same name.
        junior = Program.all_objects.create(tenant=cls.tenant, name="Ikeja Junior", code="IKJ")
        cls.ikeja_jss1 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.year, program=junior, branch=cls.ikeja,
            name="JSS1", code="JSS1I", order_index=1,
        )
        cls.jss1c = cls.make_class(cls.ikeja_jss1, "JSS1 C", "JSS1C", cls.ikeja, None)
        cls.ngozi = cls.make_student("Ngozi", "Ibe", cls.jss1c, "BFS/501", branch=cls.ikeja)
        cls.ada = cls.make_student("Ada", "Eze", cls.jss1c, "BFS/502", branch=cls.ikeja)

        a, c = cls.jss1a, cls.jss1c
        cls.register(a, cls.lekki, OCT_6, {cls.chiamaka: "P", cls.tunde: "L", cls.kelechi: "P"})
        cls.register(c, cls.ikeja, OCT_6, {cls.ngozi: "P", cls.ada: "P"})
        cls.register(c, cls.ikeja, LEKKI_OPEN_DAY, {cls.ngozi: "A", cls.ada: "P"})
        cls.register(
            a, cls.lekki, OCT_8,
            {cls.chiamaka: "A", cls.tunde: "P", cls.kelechi: "E", cls.amina: "A"},
        )
        cls.register(c, cls.ikeja, LAST_FRIDAY, {cls.ngozi: "P", cls.ada: "L"})
        cls.register(a, cls.lekki, TODAY, {cls.chiamaka: "A", cls.tunde: "A", cls.kelechi: "P"})

    @classmethod
    def register(cls, school_class, branch, on_date, marks, *, submitted=True):
        register = ClassRegister.objects.create(
            tenant=cls.tenant, school_class=school_class, date=on_date, branch=branch,
            session=cls.year, term=cls.first_term,
            submitted_at=NOW if submitted else None,
            submitted_by=cls.admin if submitted else None,
        )
        for student, mark in marks.items():
            StudentMark.objects.create(
                tenant=cls.tenant, register=register, student=student, mark=mark,
                arrived_at=dt.time(8, 30) if mark == "L" else None,
                reason_code=cls.sick if mark == "E" else None, marked_by=cls.admin,
            )
        return register

    def report(self, user=None, **params):
        response = TenantAPIClient(user or self.reporter).get(
            reverse("attendance-reports"), params,
        )
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    @staticmethod
    def by_name(rows, key="class_name"):
        return {row[key]: row for row in rows}


class ArithmeticTests(_Report):
    def test_a_class_is_counted_from_its_submitted_registers_only(self):
        rows = self.by_name(self.report()["by_class"])
        self.assertEqual(set(rows), {"JSS1 A", "JSS1 C"})
        self.assertEqual(rows["JSS1 A"]["counts"], {
            "present": 5, "late": 1, "absent": 3, "excused": 1, "rate": 55.6,
            "not_taken": 1, "roster_size": 3,
        })
        self.assertEqual(rows["JSS1 C"]["counts"], {
            "present": 5, "late": 1, "absent": 1, "excused": 0, "rate": 83.3,
            "not_taken": 2, "roster_size": 2,
        })

    def test_a_suspended_pupil_and_a_pupil_not_yet_placed_count_nowhere(self):
        row = self.by_name(self.report()["by_class"])["JSS1 A"]
        # Amina's absence on the 8th and Obinna's placement on the 20th change nothing.
        self.assertEqual(row["counts"]["absent"], 3)
        self.assertEqual(row["counts"]["roster_size"], 3)

    def test_a_closed_day_and_a_day_nobody_took_are_not_absences(self):
        body = self.report()
        rows = self.by_name(body["by_class"])
        # JSS1 A: closed on the 7th (Lekki only) and the 9th, not taken on the 10th.
        self.assertEqual(rows["JSS1 A"]["counts"]["not_taken"], 1)
        self.assertEqual(rows["JSS1 A"]["counts"]["absent"], 3)
        self.assertEqual(
            [(r["date"], r["class_name"]) for r in body["not_taken"]],
            [
                (TODAY.isoformat(), "JSS1 C"), (LAST_FRIDAY.isoformat(), "JSS1 A"),
                (OCT_8.isoformat(), "JSS1 C"),
            ],
        )

    def test_a_mark_on_a_day_the_school_was_closed_counts_nowhere(self):
        self.register(self.jss1a, self.lekki, FOUNDERS_DAY, {self.chiamaka: "A", self.tunde: "A"})
        row = self.by_name(self.report()["by_class"])["JSS1 A"]
        self.assertEqual(row["counts"]["absent"], 3)
        self.assertEqual(row["counts"]["rate"], 55.6)

    def test_a_register_never_submitted_counts_as_not_taken_whatever_it_holds(self):
        # A draft on the 10th: marked but never submitted.
        self.register(self.jss1a, self.lekki, LAST_FRIDAY, {self.chiamaka: "A"}, submitted=False)
        row = self.by_name(self.report()["by_class"])["JSS1 A"]
        self.assertEqual(row["counts"]["absent"], 3)
        self.assertEqual(row["counts"]["not_taken"], 1)

    def test_the_range_counts_the_days_the_school_was_open_to_somebody(self):
        body = self.report()
        self.assertEqual(body["range"], {
            "start": "2025-10-06", "end": "2025-10-13", "school_days": 5,
        })
        # Lekki alone loses the 7th as well as the 9th.
        self.assertEqual(self.report(branch=self.lekki.pk)["range"]["school_days"], 4)
        self.assertEqual(self.report(branch=self.ikeja.pk)["range"]["school_days"], 5)

    def test_the_trend_has_a_row_per_week_and_the_rate_of_what_was_taken(self):
        self.assertEqual(self.report()["trend"], [
            {"week_start": "2025-10-06", "label": "Wk 1", "rate": 75.0},
            {"week_start": "2025-10-13", "label": "Wk 2", "rate": 33.3},
        ])

    def test_a_week_with_no_register_has_no_rate_rather_than_zero(self):
        AcademicTerm.all_objects.filter(pk=self.first_term.pk).update(
            start_date=dt.date(2025, 9, 29),
        )
        trend = self.report()["trend"]
        self.assertEqual([r["label"] for r in trend], ["Wk 1", "Wk 2", "Wk 3"])
        self.assertEqual([r["rate"] for r in trend], [None, 75.0, 33.3])

    def test_weeks_start_on_the_day_the_school_says(self):
        from schools.vs_calendar.constants import CFG_WEEK_STARTS_ON

        set_setting(CFG_WEEK_STARTS_ON, 7, self.tenant, self.admin)
        trend = self.report()["trend"]
        self.assertEqual(
            [r["week_start"] for r in trend], ["2025-10-05", "2025-10-12"],
        )
        self.assertEqual([r["rate"] for r in trend], [75.0, 33.3])

    def test_a_level_is_added_up_across_branches_not_averaged(self):
        [level] = self.report()["by_level"]
        self.assertEqual(level, {
            "level_name": "JSS1", "present": 10, "late": 2, "absent": 4, "excused": 1,
            "not_taken": 3, "rate": 66.7,
        })

    def test_a_class_nobody_is_placed_in_is_not_listed(self):
        names = {row["class_name"] for row in self.report()["by_class"]}
        self.assertNotIn("JSS1 B", names)
        self.assertNotIn("Primary 4 A", names)
        self.assertNotIn("JSS1 Shared", names)

    def test_a_pupil_who_changes_class_is_judged_in_each_class_for_its_own_stretch(self):
        ife = self.make_student("Ifeanyi", "Ude", self.jss1a, "BFS/601")
        # The move supersedes the first placement, which stays as history.
        ClassEnrolment.all_objects.filter(student=ife).update(is_active=False)
        ClassEnrolment.all_objects.create(
            tenant=self.tenant, student=ife, school_class=self.jss1b, session=self.year,
            is_active=True, effective_date=OCT_8,
        )
        StudentMark.objects.create(
            tenant=self.tenant, register=ClassRegister.objects.get(
                school_class=self.jss1a, date=OCT_6,
            ), student=ife, mark="P", marked_by=self.admin,
        )
        # A stale mark in the old class after the move, and the real one in the new.
        StudentMark.objects.create(
            tenant=self.tenant, register=ClassRegister.objects.get(
                school_class=self.jss1a, date=OCT_8,
            ), student=ife, mark="A", marked_by=self.admin,
        )
        self.register(self.jss1b, self.lekki, OCT_8, {ife: "A"})

        rows = self.by_name(self.report()["by_class"])
        self.assertEqual(rows["JSS1 A"]["counts"]["present"], 6)
        self.assertEqual(rows["JSS1 A"]["counts"]["absent"], 3)
        self.assertEqual(rows["JSS1 B"]["counts"], {
            "present": 0, "late": 0, "absent": 1, "excused": 0, "rate": 0.0,
            "not_taken": 2, "roster_size": 1,
        })
        chronic = {r["student"]["id"]: r for r in self.report()["chronic"]}
        self.assertEqual(chronic[ife.pk]["rate"], 50.0)
        self.assertEqual(chronic[ife.pk]["class_name"], "JSS1 B")
        tab = student_tab(self.tenant, self.reporter, ife.pk)["counts"]
        self.assertEqual(
            (tab["rate"], tab["absent"], tab["not_taken"]),
            (50.0, 1, chronic[ife.pk]["not_taken"]),
        )


class ChronicTests(_Report):
    def chronic(self, **params):
        return self.report(**params)["chronic"]

    def test_the_students_below_the_threshold_are_listed_lowest_first(self):
        rows = self.chronic()
        self.assertEqual(
            [(r["student"]["name"], r["rate"]) for r in rows],
            [
                ("Chiamaka Nwosu", 33.3), ("Kelechi Eze", 66.7),
                ("Ngozi Ibe", 66.7), ("Tunde Bello", 66.7),
            ],
        )
        chiamaka = rows[0]
        self.assertEqual(chiamaka["student"]["student_number"], "BFS/001")
        self.assertEqual(chiamaka["class_name"], "JSS1 A")
        self.assertEqual((chiamaka["absent"], chiamaka["not_taken"]), (2, 1))
        self.assertEqual(rows[2]["not_taken"], 2)

    def test_a_pupil_exactly_at_the_threshold_is_not_listed(self):
        set_setting(CFG_CHRONIC_THRESHOLD, 100, self.tenant, self.admin)
        names = {r["student"]["name"] for r in self.chronic()}
        # Ada is at 100.0 exactly: at the threshold, not below it.
        self.assertNotIn("Ada Eze", names)
        self.assertIn("Ngozi Ibe", names)
        set_setting(CFG_CHRONIC_THRESHOLD, 67, self.tenant, self.admin)
        self.assertEqual(
            {r["student"]["name"] for r in self.chronic()},
            {"Chiamaka Nwosu", "Kelechi Eze", "Ngozi Ibe", "Tunde Bello"},
        )
        set_setting(CFG_CHRONIC_THRESHOLD, 66, self.tenant, self.admin)
        self.assertEqual([r["student"]["name"] for r in self.chronic()], ["Chiamaka Nwosu"])
        self.assertEqual(self.report()["threshold"], 66)

    def test_a_pupil_with_no_counted_day_is_not_listed(self):
        set_setting(CFG_CHRONIC_THRESHOLD, 100, self.tenant, self.admin)
        names = {r["student"]["name"] for r in self.chronic()}
        self.assertNotIn("Amina Yusuf", names)
        self.assertNotIn("Obinna Okafor", names)

    def test_every_figure_agrees_with_the_students_own_tab(self):
        listed = {r["student"]["id"]: r for r in self.chronic()}
        self.assertEqual(len(listed), 4)
        for student_id, row in listed.items():
            with self.subTest(student=row["student"]["name"]):
                counts = student_tab(self.tenant, self.reporter, student_id)["counts"]
                self.assertEqual(
                    (row["rate"], row["absent"], row["not_taken"]),
                    (counts["rate"], counts["absent"], counts["not_taken"]),
                )

    def test_a_class_total_is_the_sum_of_its_pupils_tabs(self):
        total = {"present": 0, "absent": 0, "excused": 0}
        for pupil in (self.chiamaka, self.tunde, self.kelechi, self.amina, self.obinna):
            counts = student_tab(self.tenant, self.reporter, pupil.pk)["counts"]
            for key in total:
                total[key] += counts[key]
        row = self.by_name(self.report()["by_class"])["JSS1 A"]["counts"]
        self.assertEqual(
            total, {k: row[k] for k in ("present", "absent", "excused")},
        )


class ScopeTests(_Report):
    def test_a_branch_reader_sees_their_branchs_classes_and_nobody_elses(self):
        ikeja = self.report(self.ikeja_reporter)
        self.assertEqual([r["class_name"] for r in ikeja["by_class"]], ["JSS1 C"])
        self.assertEqual({r["class_name"] for r in ikeja["not_taken"]}, {"JSS1 C"})
        self.assertEqual({r["student"]["name"] for r in ikeja["chronic"]}, {"Ngozi Ibe"})
        self.assertEqual(ikeja["by_level"][0]["present"], 5)
        lekki = self.report(self.lekki_reporter)
        self.assertEqual([r["class_name"] for r in lekki["by_class"]], ["JSS1 A"])
        self.assertEqual({r["class_name"] for r in lekki["not_taken"]}, {"JSS1 A"})

    def test_a_branch_reader_cannot_ask_for_another_branch(self):
        response = TenantAPIClient(self.ikeja_reporter).get(
            reverse("attendance-reports"), {"branch": self.lekki.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_a_whole_school_reader_narrows_by_branch(self):
        rows = self.report(branch=self.ikeja.pk)["by_class"]
        self.assertEqual([r["class_name"] for r in rows], ["JSS1 C"])
        self.assertEqual(rows[0]["branch"], {"id": self.ikeja.pk, "name": "Ikeja Branch"})

    def test_a_school_with_several_branches_carries_the_branch_dimension(self):
        body = self.report()
        self.assertTrue(body["branch_dimension"])
        rows = self.by_name(body["by_class"])
        self.assertEqual(rows["JSS1 A"]["branch"]["name"], "Lekki Branch")
        self.assertEqual(body["not_taken"][0]["branch"]["name"], "Ikeja Branch")

    def test_a_school_with_one_branch_drops_the_dimension_and_keeps_the_level(self):
        body = self.report(self.solo_reporter)
        self.assertFalse(body["branch_dimension"])
        [row] = body["by_class"]
        self.assertEqual(row["class_name"], "Basic 3")
        self.assertIsNone(row["branch"])
        self.assertEqual(row["level_name"], "Basic 3")
        self.assertIsNone(body["not_taken"][0]["branch"])
        self.assertEqual(body["not_taken"][0]["level_name"], "Basic 3")

    def test_another_schools_classes_and_pupils_never_appear(self):
        names = {r["class_name"] for r in self.report()["by_class"]}
        self.assertNotIn("Basic 3", names)
        solo_names = {r["class_name"] for r in self.report(self.solo_reporter)["by_class"]}
        self.assertEqual(solo_names, {"Basic 3"})

    def test_a_term_of_another_school_is_a_404(self):
        solo_term = AcademicTerm.all_objects.get(tenant=self.solo)
        response = TenantAPIClient(self.reporter).get(
            reverse("attendance-reports"), {"term": solo_term.pk},
        )
        self.assertEqual(response.status_code, 404)
        response = TenantAPIClient(self.solo_reporter).get(
            reverse("attendance-reports"), {"term": self.first_term.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_a_branch_of_another_school_is_a_404(self):
        response = TenantAPIClient(self.reporter).get(
            reverse("attendance-reports"), {"branch": self.solo_branch.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_a_term_that_is_not_a_number_is_refused(self):
        response = TenantAPIClient(self.reporter).get(
            reverse("attendance-reports"), {"term": "last"},
        )
        self.assertEqual(response.status_code, 400)


class PermissionTests(_Report):
    def test_the_report_needs_its_own_key(self):
        for user in (self.nobody, self.eze, self.admin):
            with self.subTest(user=user.email):
                response = TenantAPIClient(user).get(reverse("attendance-reports"))
                self.assertEqual(response.status_code, 403)

    def test_the_reader_with_the_key_is_let_in(self):
        self.assertEqual(
            TenantAPIClient(self.reporter).get(reverse("attendance-reports")).status_code, 200,
        )


class TermAndEmptyShapeTests(_Report):
    def test_the_terms_for_the_pills_travel_with_the_current_one(self):
        body = self.report()
        self.assertEqual(body["term"]["name"], "First Term")
        self.assertTrue(body["term"]["is_current"])
        self.assertEqual([t["id"] for t in body["terms"]], [self.first_term.pk])
        self.assertEqual(body["term"]["start_date"], OCT_6)

    def test_a_past_term_is_reported_to_its_last_day(self):
        past = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=self.year, name="Zero Term", order_index=0,
            start_date=dt.date(2025, 9, 1), end_date=dt.date(2025, 10, 3),
        )
        body = self.report(term=past.pk)
        self.assertEqual(body["term"]["id"], past.pk)
        self.assertEqual(body["range"]["end"], "2025-10-03")
        for row in body["by_class"]:
            self.assertEqual(row["counts"]["present"], 0)
            self.assertIsNone(row["counts"]["rate"])
        self.assertEqual(body["chronic"], [])

    def test_a_school_with_nothing_taken_has_no_rate_and_a_list_of_missed_days(self):
        body = self.report(self.solo_reporter)
        [row] = body["by_class"]
        self.assertEqual(row["counts"], {
            "present": 0, "late": 0, "absent": 0, "excused": 0, "rate": None,
            "not_taken": 26, "roster_size": 1,
        })
        self.assertEqual(body["chronic"], [])
        self.assertEqual(body["range"]["school_days"], 26)
        self.assertTrue(all(week["rate"] is None for week in body["trend"]))
        self.assertEqual(len(body["trend"]), 6)
        [level] = body["by_level"]
        self.assertIsNone(level["rate"])

    def test_a_term_that_has_not_begun_has_an_empty_report(self):
        future = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=self.year, name="Third Term", order_index=3,
            start_date=dt.date(2026, 4, 20), end_date=dt.date(2026, 7, 17),
        )
        body = self.report(term=future.pk)
        self.assertEqual(body["range"], {"start": None, "end": None, "school_days": 0})
        for block in ("trend", "by_class", "by_level", "chronic", "not_taken"):
            self.assertEqual(body[block], [])

    def test_a_school_with_no_term_at_all_has_an_empty_report(self):
        school = make_school(slug="barren", name="Barren Academy")
        make_branch(school, name="Main", is_main=True)
        role = make_key_role(school.tenant, "Reporter", "reporter", (PERM_REPORT_VIEW,))
        user = make_school_admin(None, email="r@barren.test", tenant=school.tenant)
        make_assignment(school.tenant, user, role, branch=None)
        body = self.report(user)
        self.assertIsNone(body["term"])
        self.assertEqual(body["terms"], [])
        self.assertEqual(body["by_class"], [])
        self.assertEqual(body["threshold"], 90)


class FieldAccessTests(_Report):
    def test_hidden_counts_leave_the_classes_without_their_figures(self):
        install_declared_fields("attendance.register")
        role = make_key_role(
            self.tenant, "No totals", "no_totals", (PERM_REGISTER_VIEW, PERM_REPORT_VIEW),
        )
        user = make_school_admin(None, email="nt@brightfield.test", tenant=self.tenant)
        make_assignment(self.tenant, user, role, branch=None)
        set_field_access(role, "attendance.register.counts", read=False, write=False)
        body = self.report(user)
        for gone in ("trend", "by_level", "chronic"):
            self.assertNotIn(gone, body)
        self.assertNotIn("school_days", body["range"])
        self.assertEqual(body["range"]["start"], "2025-10-06")
        self.assertEqual(
            sorted(row["class_name"] for row in body["by_class"]), ["JSS1 A", "JSS1 C"],
        )
        for row in body["by_class"]:
            self.assertNotIn("counts", row)
        self.assertEqual(len(body["not_taken"]), 3)
        set_field_access(role, "attendance.register.status", read=False, write=False)
        self.assertNotIn("not_taken", self.report(user))

    def test_the_figures_come_back_with_the_switch(self):
        install_declared_fields("attendance.register")
        body = self.report()
        self.assertIn("trend", body)
        self.assertIn("school_days", body["range"])
        self.assertTrue(all("counts" in row for row in body["by_class"]))


class QueryCountTests(_Report):
    def count(self):
        with CaptureQueriesContext(connection) as queries:
            response = TenantAPIClient(self.reporter).get(reverse("attendance-reports"))
        self.assertEqual(response.status_code, 200, response.data)
        return len(queries), self.data(response)

    def add_class(self, number):
        school_class = self.make_class(
            self.ikeja_jss1, f"Extra {number}", f"EX{number}", self.ikeja, self.bello_staff,
        )
        pupils = [
            self.make_student(
                f"Pupil{number}{n}", "Test", school_class, f"EX/{number}{n}", branch=self.ikeja,
            )
            for n in range(3)
        ]
        for day in (OCT_6, OCT_8, LAST_FRIDAY):
            self.register(school_class, self.ikeja, day, dict(zip(pupils, "PAL")))
        # One pupil with a suspension, so the extra classes exercise both paths.
        self.make_suspension(pupils[0], dt.date(2025, 10, 10))

    def test_the_queries_do_not_grow_with_the_classes_the_pupils_or_the_days(self):
        self.count()
        few, body = self.count()
        self.assertEqual(len(body["by_class"]), 2)
        for number in range(6):
            self.add_class(number)
        many, body = self.count()
        self.assertEqual(len(body["by_class"]), 8)
        self.assertEqual(many, few)
        self.assertLess(many, 50)

    def test_a_suspension_mid_term_changes_the_arithmetic_but_not_the_queries(self):
        self.add_class(0)
        body = self.report()
        row = self.by_name(body["by_class"])["Extra 0"]["counts"]
        # Three days of P, A, L per pupil; Pupil00 is suspended from the 10th,
        # so the 10th (L-or-P-or-A of that pupil) is not counted.
        self.assertEqual(row["roster_size"], 2)
        self.assertEqual(row["absent"] + row["present"] + row["excused"], 8)


class ClassChronicTests(_Report):
    """One class's chronic list: the report's, narrowed to the class."""

    def class_chronic(self, user, school_class, **params):
        return TenantAPIClient(user).get(
            reverse("attendance-class-chronic", kwargs={"class_id": school_class.pk}), params,
        )

    def test_the_class_teacher_sees_their_own_pupils_below_the_line(self):
        response = self.class_chronic(self.eze, self.jss1a)
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertEqual(body["school_class"]["name"], "JSS1 A")
        self.assertEqual(body["threshold"], 90)
        self.assertEqual(
            [(r["student"]["name"], r["rate"]) for r in body["chronic"]],
            [("Chiamaka Nwosu", 33.3), ("Kelechi Eze", 66.7), ("Tunde Bello", 66.7)],
        )

    def test_a_teacher_cannot_ask_about_a_class_they_do_not_lead(self):
        response = self.class_chronic(self.bello, self.jss1a)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "NOT_YOUR_CLASS")

    def test_a_report_reader_may_ask_about_any_class_in_their_branches(self):
        body = self.data(self.class_chronic(self.reporter, self.jss1c))
        self.assertEqual([r["student"]["name"] for r in body["chronic"]], ["Ngozi Ibe"])
        lekki_only = self.data(self.class_chronic(self.lekki_reporter, self.jss1c))
        self.assertEqual(lekki_only["chronic"], [])

    def test_another_schools_class_is_not_found(self):
        self.assertEqual(self.class_chronic(self.solo_reporter, self.jss1a).status_code, 404)

    def test_hidden_counts_leave_out_the_list(self):
        install_declared_fields("attendance.register")
        set_field_access(self.teacher_role, "attendance.register.counts", read=False, write=False)
        body = self.data(self.class_chronic(self.eze, self.jss1a))
        self.assertNotIn("chronic", body)
        self.assertEqual(body["school_class"]["name"], "JSS1 A")
