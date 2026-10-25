"""The report as a file: the same figures as the screen, cut to the caller.

Built on the report's own fixture (see ``test_report``): JSS1 A at Lekki and
JSS1 C at Ikeja, a term that began on Monday 6 October 2025, and the school
clock fixed at Monday 13 October. The figures asserted here are the ones that
fixture's docstring works out by hand, and every file is also compared with the
JSON the report endpoint serves for the same caller.
"""
from __future__ import annotations

import csv
import io

from django.urls import reverse

from core.test_utils import TenantAPIClient
from vs_audit.models import AuditEvent
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_school_admin,
    set_field_access,
)

from ..constants import PERM_REGISTER_VIEW, PERM_REPORT_EXPORT, PERM_REPORT_VIEW
from .base import make_key_role
from .test_report import _Report


class _Export(_Report):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        role = make_key_role(
            cls.tenant, "Exporter", "exporter",
            (PERM_REGISTER_VIEW, PERM_REPORT_VIEW, PERM_REPORT_EXPORT),
        )
        cls.exporter = make_school_admin(
            None, email="export@brightfield.test", tenant=cls.tenant,
            first_name="Efe", last_name="Okon",
        )
        make_assignment(cls.tenant, cls.exporter, role, branch=None)
        cls.lekki_exporter = make_school_admin(
            cls.lekki, email="export@lekki.test", first_name="Lami", last_name="Ade",
        )
        make_assignment(cls.tenant, cls.lekki_exporter, role, branch=cls.lekki)
        solo_role = make_key_role(
            cls.solo, "Exporter", "exporter",
            (PERM_REGISTER_VIEW, PERM_REPORT_VIEW, PERM_REPORT_EXPORT),
        )
        cls.solo_exporter = make_school_admin(
            None, email="export@sunrise.test", tenant=cls.solo,
            first_name="Sola", last_name="Eze",
        )
        make_assignment(cls.solo, cls.solo_exporter, solo_role, branch=None)

    def fetch(self, user=None, **params):
        return TenantAPIClient(user or self.exporter).get(
            reverse("attendance-report-export"), params,
        )

    def sheet(self, response):
        """``(title, subtitle, headers, rows)`` of a CSV response."""
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        lines = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        title, subtitle, blank, headers, *rows = lines
        self.assertEqual(blank, [])
        return title[0], subtitle[0], headers, rows

    def export(self, table, user=None, **params):
        return self.sheet(self.fetch(user, table=table, **params))

    def as_role(self, *hidden):
        """A whole-school exporter with the given register fields switched off."""
        install_declared_fields("attendance.register")
        role = make_key_role(
            self.tenant, "Narrow", f"narrow_{'_'.join(hidden)}",
            (PERM_REGISTER_VIEW, PERM_REPORT_EXPORT),
        )
        user = make_school_admin(
            None, email=f"narrow_{'_'.join(hidden)}@brightfield.test",
            tenant=self.tenant,
        )
        make_assignment(self.tenant, user, role, branch=None)
        for key in hidden:
            set_field_access(role, f"attendance.register.{key}", read=False, write=False)
        return user


class PermissionTests(_Export):
    def test_the_export_needs_its_own_key_even_with_the_report(self):
        for user in (self.reporter, self.nobody, self.eze, self.admin):
            with self.subTest(user=user.email):
                response = self.fetch(user, table="by_class")
                self.assertEqual(response.status_code, 403)

    def test_the_key_to_export_is_enough_without_the_one_to_read(self):
        install_declared_fields("attendance.register")
        role = make_key_role(
            self.tenant, "Export only", "export_only", (PERM_REPORT_EXPORT,),
        )
        user = make_school_admin(None, email="eo@brightfield.test", tenant=self.tenant)
        make_assignment(self.tenant, user, role, branch=None)
        self.assertEqual(self.fetch(user, table="by_class").status_code, 200)

    def test_a_branch_of_another_school_is_a_404(self):
        response = self.fetch(table="by_class", branch=self.solo_branch.pk)
        self.assertEqual(response.status_code, 404)

    def test_a_branch_reader_cannot_export_another_branch(self):
        response = self.fetch(self.lekki_exporter, table="by_class", branch=self.ikeja.pk)
        self.assertEqual(response.status_code, 404)


class RequestShapeTests(_Export):
    def test_an_unknown_table_is_refused_with_a_sentence(self):
        response = self.fetch(table="payroll")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose which table to export", str(response.data))

    def test_no_table_is_refused_the_same_way(self):
        self.assertEqual(self.fetch().status_code, 400)

    def test_an_unknown_file_type_is_refused(self):
        response = self.fetch(table="by_class", export="docx")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose a file type", str(response.data))

    def test_the_default_is_a_csv_named_for_the_table_and_the_term(self):
        response = self.fetch(table="by_class")
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="attendance_by_class_first_term.csv"',
        )

    def test_xlsx_and_pdf_are_accepted(self):
        for kind, content_type, magic in (
            ("xlsx", "spreadsheetml", b"PK"), ("pdf", "application/pdf", b"%PDF"),
        ):
            with self.subTest(kind=kind):
                response = self.fetch(table="chronic", export=kind)
                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response["Content-Type"])
                self.assertTrue(response.content.startswith(magic))
                self.assertIn(f"attendance_chronic_first_term.{kind}", response["Content-Disposition"])

    def test_a_term_that_is_not_a_number_is_refused(self):
        self.assertEqual(self.fetch(table="by_class", term="last").status_code, 400)

    def test_a_term_of_another_school_is_a_404(self):
        from schools.vs_academics.models import AcademicTerm

        solo_term = AcademicTerm.all_objects.get(tenant=self.solo)
        self.assertEqual(self.fetch(table="by_class", term=solo_term.pk).status_code, 404)


class ByClassTests(_Export):
    def test_each_class_is_a_row_with_its_figures_written_for_people(self):
        title, subtitle, headers, rows = self.export("by_class")
        self.assertEqual(title, "Attendance by class")
        self.assertEqual(subtitle, "First Term, 2025/2026 · All branches")
        self.assertEqual(headers, [
            "Class", "Branch", "Rate", "Absences", "Lateness", "Days not taken", "Students",
        ])
        # Classes are listed by branch name, then level and class name.
        self.assertEqual(rows, [
            ["JSS1 C", "Ikeja Branch", "83.3%", "1", "1", "2", "2"],
            ["JSS1 A", "Lekki Branch", "55.6%", "3", "1", "1", "3"],
        ])

    def test_the_file_agrees_with_the_report_for_the_same_caller(self):
        report = self.report(self.exporter)
        _, _, _, rows = self.export("by_class")
        self.assertEqual(len(rows), len(report["by_class"]))
        for row, item in zip(rows, report["by_class"], strict=True):
            counts = item["counts"]
            self.assertEqual(row[0], item["class_name"])
            self.assertEqual(row[2], f"{counts['rate']:g}%")
            self.assertEqual(
                row[3:], [str(counts[k]) for k in ("absent", "late", "not_taken", "roster_size")],
            )

    def test_a_branch_can_be_named(self):
        _, subtitle, _, rows = self.export("by_class", branch=self.ikeja.pk)
        self.assertEqual([r[0] for r in rows], ["JSS1 C"])
        self.assertEqual(subtitle, "First Term, 2025/2026 · Ikeja Branch")

    def test_a_branch_bound_reader_gets_only_their_own_branch_and_the_file_says_so(self):
        _, subtitle, _, rows = self.export("by_class", self.lekki_exporter)
        self.assertEqual([r[0] for r in rows], ["JSS1 A"])
        self.assertEqual(rows[0][1], "Lekki Branch")
        self.assertEqual(
            subtitle, "First Term, 2025/2026 · the reader's branches only",
        )

    def test_a_school_with_one_branch_shows_the_level_and_names_no_branch(self):
        _, subtitle, headers, rows = self.export("by_class", self.solo_exporter)
        self.assertEqual(headers[:2], ["Class", "Level"])
        self.assertEqual(rows[0][:2], ["Basic 3", "Basic 3"])
        self.assertNotIn("Branch", subtitle)
        self.assertNotIn("branch", subtitle)

    def test_a_class_with_no_register_taken_has_an_empty_rate_not_a_zero(self):
        _, _, _, rows = self.export("by_class", self.solo_exporter)
        self.assertEqual(rows[0][2], "")
        self.assertEqual(rows[0][5], "26")

    def test_a_cell_that_would_run_as_a_formula_is_written_as_text(self):
        self.jss1a.name = "=SUM(A1)"
        self.jss1a.save(update_fields=["name"])
        _, _, _, rows = self.export("by_class")
        self.assertIn("'=SUM(A1)", [row[0] for row in rows])


class ChronicTests(_Export):
    def test_the_students_below_the_line_are_listed_lowest_first(self):
        title, subtitle, headers, rows = self.export("chronic")
        self.assertEqual(title, "Students below the attendance threshold")
        self.assertEqual(subtitle, "First Term, 2025/2026 · below 90% · All branches")
        self.assertEqual(
            headers,
            ["Student", "Student number", "Class", "Rate", "Days absent", "Days excused", "Days not taken"],
        )
        self.assertEqual(rows[0], ["Chiamaka Nwosu", "BFS/001", "JSS1 A", "33.3%", "2", "0", "1"])
        self.assertEqual(
            [r[0] for r in rows],
            ["Chiamaka Nwosu", "Kelechi Eze", "Ngozi Ibe", "Tunde Bello"],
        )

    def test_the_file_lists_exactly_the_reports_students(self):
        report = self.report(self.exporter)["chronic"]
        _, _, _, rows = self.export("chronic")
        self.assertEqual([r[1] for r in rows], [c["student"]["student_number"] for c in report])
        self.assertEqual([r[3] for r in rows], [f"{c['rate']:g}%" for c in report])

    def test_a_branch_reader_sees_only_their_own_branchs_students(self):
        _, _, _, rows = self.export("chronic", self.lekki_exporter)
        self.assertEqual(
            {r[0] for r in rows}, {"Chiamaka Nwosu", "Kelechi Eze", "Tunde Bello"},
        )


class NotTakenTests(_Export):
    def test_the_days_nobody_took_are_listed_newest_first_in_the_schools_date_format(self):
        title, _, headers, rows = self.export("not_taken")
        self.assertEqual(title, "Registers not taken")
        self.assertEqual(headers, ["Date", "Class", "Class teacher", "Branch"])
        self.assertEqual(rows, [
            ["13 Oct 2025", "JSS1 C", "", "Ikeja Branch"],
            ["10 Oct 2025", "JSS1 A", "Chidi Eze", "Lekki Branch"],
            ["8 Oct 2025", "JSS1 C", "", "Ikeja Branch"],
        ])

    def test_at_a_school_with_one_branch_the_last_column_is_the_level(self):
        _, _, headers, rows = self.export("not_taken", self.solo_exporter)
        self.assertEqual(headers[-1], "Level")
        self.assertEqual(rows[0][-1], "Basic 3")


class FieldAccessTests(_Export):
    def test_hidden_counts_drop_the_figure_columns_rather_than_blank_them(self):
        user = self.as_role("counts")
        _, _, headers, rows = self.export("by_class", user)
        self.assertEqual(headers, ["Class", "Branch"])
        self.assertEqual(rows, [["JSS1 C", "Ikeja Branch"], ["JSS1 A", "Lekki Branch"]])

    def test_hidden_counts_leave_the_chronic_list_as_headings_alone(self):
        user = self.as_role("counts")
        _, _, headers, rows = self.export("chronic", user)
        self.assertEqual(headers, ["Student", "Student number", "Class"])
        self.assertEqual(rows, [])

    def test_hidden_counts_leave_the_days_not_taken_untouched(self):
        user = self.as_role("counts")
        _, _, headers, rows = self.export("not_taken", user)
        self.assertEqual(len(headers), 4)
        self.assertEqual(len(rows), 3)

    def test_a_hidden_status_leaves_the_not_taken_list_as_headings_alone(self):
        user = self.as_role("status")
        _, _, headers, rows = self.export("not_taken", user)
        self.assertEqual(headers, ["Date", "Class", "Class teacher", "Branch"])
        self.assertEqual(rows, [])
        # The figures stay.
        self.assertEqual(len(self.export("by_class", user)[2]), 7)

    def test_a_hidden_class_teacher_drops_the_column_and_the_names(self):
        install_declared_fields("academics.classes")
        role = make_key_role(
            self.tenant, "No class teacher", "no_class_teacher",
            (PERM_REGISTER_VIEW, PERM_REPORT_VIEW, PERM_REPORT_EXPORT),
        )
        user = make_school_admin(None, email="noct@brightfield.test", tenant=self.tenant)
        make_assignment(self.tenant, user, role, branch=None)
        set_field_access(role, "academics.classes.class_teacher", read=False, write=False)

        _, _, headers, rows = self.export("not_taken", user)
        self.assertEqual(headers, ["Date", "Class", "Branch"])
        self.assertEqual(rows[1], ["10 Oct 2025", "JSS1 A", "Lekki Branch"])

        report = TenantAPIClient(user).get(reverse("attendance-reports"))
        self.assertEqual(report.status_code, 200, report.data)
        for row in report.data["data"]["not_taken"]:
            self.assertNotIn("class_teacher_name", row)

    def test_the_figures_come_back_with_the_switch(self):
        install_declared_fields("attendance.register")
        _, _, headers, _ = self.export("by_class")
        self.assertIn("Rate", headers)


class AuditTests(_Export):
    def test_each_export_is_recorded_with_who_which_table_and_which_term(self):
        self.fetch(table="chronic")
        event = AuditEvent.objects.get(entity_type="AttendanceReport")
        self.assertEqual(event.action_type, "EXPORT_COMPLETED")
        self.assertEqual(event.tenant_id, self.tenant.pk)
        self.assertEqual(event.actor_user_id, self.exporter.pk)
        self.assertEqual(event.metadata["table"], "chronic")
        self.assertEqual(event.metadata["term_id"], self.first_term.pk)
        self.assertEqual(event.metadata["file_type"], "csv")
        self.assertIn("First Term", event.summary)

    def test_a_refused_export_is_not_recorded(self):
        self.fetch(self.reporter, table="chronic")
        self.fetch(table="bogus")
        self.assertFalse(AuditEvent.objects.filter(entity_type="AttendanceReport").exists())
