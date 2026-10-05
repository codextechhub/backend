"""The annual PAYE return lists each person once for the year.

Bright Star School pays some of its staff on hand-typed payroll lines that name
the person and nothing else: no user account, no salary record. Tunde Bakare, a
visiting music teacher, is paid that way in January and February, so the return
must show him once with two months, not twice. Two teachers called Ada Obi each
have their own user account; they share a name and must stay two people. A third
Ada Obi, paid on a hand-typed line, cannot be told apart from either of them by
anything but her name, so she is listed on her own rather than folded into one
of theirs. Where a hand-typed line carries a tax number, two namesakes with
different numbers stay apart, and a month typed without the number joins the
only one of that name that has it.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from vs_finance.constants import PayrollRunStatus
from vs_finance.models import EmployeeSalary, LedgerEntity, PayrollLine, PayrollRun

N = 100


class AnnualReturnPeopleTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        from vs_rbac.tests.helpers import make_branch, make_school

        def make_user(*, tenant, email):
            return get_user_model().objects.create_user(
                email=email, password="pw", tenant=tenant, status="ACTIVE",
                first_name="Bright", last_name="Star")

        school = make_school(slug="paye-people", name="Bright Star School", status="ACTIVE")
        cls.main = make_branch(school, name="Main Branch")
        cls.books = LedgerEntity.objects.create(
            name="Bright Star Books", code="PAYEPPL", kind=LedgerEntity.Kind.TENANT,
            tenant=school.tenant,
        )
        cls.ada_one = make_user(tenant=school.tenant, email="ada.one@brightstar.test")
        cls.ada_two = make_user(tenant=school.tenant, email="ada.two@brightstar.test")
        cls.ngozi = make_user(tenant=school.tenant, email="ngozi@brightstar.test")
        cls.ngozi_salary = EmployeeSalary.objects.create(
            entity=cls.books, name="Ngozi Eze", branch=cls.main, gross_amount=200_000 * N,
            employee=cls.ngozi,
        )
        cls.runs = [
            PayrollRun.objects.create(
                entity=cls.books, pay_date=datetime.date(2026, month, 25),
                run_status=PayrollRunStatus.POSTED,
            )
            for month in (1, 2)
        ]
        january, february = cls.runs
        cls.line(january, 1, "Tunde Bakare", 100_000, 5_000)
        cls.line(february, 1, "  tunde   BAKARE ", 100_000, 5_000)
        cls.line(january, 2, "Ada Obi", 300_000, 30_000, employee=cls.ada_one)
        cls.line(february, 2, "Ada Obi", 300_000, 30_000, employee=cls.ada_one)
        cls.line(january, 3, "Ada Obi", 250_000, 20_000, employee=cls.ada_two)
        cls.line(january, 4, "Ada Obi", 50_000, 1_000)
        cls.line(january, 6, "Musa Bello", 80_000, 2_000, tax_id="TIN-001")
        cls.line(february, 6, "Musa Bello", 80_000, 2_000)
        cls.line(january, 7, "Kemi Ade", 70_000, 1_500, tax_id="TIN-002")
        cls.line(february, 7, "Kemi Ade", 70_000, 1_500, tax_id="tin 002")
        cls.line(february, 8, "Kemi Ade", 60_000, 1_000, tax_id="TIN-003")
        cls.line(january, 5, "Ngozi Eze", 200_000, 15_000, employee=cls.ngozi)
        cls.line(february, 5, "Ngozi Eze", 200_000, 15_000, employee=cls.ngozi,
                 salary=cls.ngozi_salary)

    @classmethod
    def line(cls, run, number, name, gross, paye, **extra):
        return PayrollLine.objects.create(
            run=run, line_no=number, employee_name=name, gross_amount=gross * N,
            taxable_pay=gross * N, paye_amount=paye * N, **extra,
        )

    def rows(self):
        from vs_finance.payslips import annual_paye_return

        return annual_paye_return(self.books, year=2026)["rows"]

    def test_a_person_on_hand_typed_lines_is_listed_once_for_the_year(self):
        tunde = [row for row in self.rows() if row["employee_name"].strip().lower().startswith("tunde")]

        self.assertEqual(len(tunde), 1)
        self.assertEqual((tunde[0]["months"], tunde[0]["gross"], tunde[0]["paye"]),
                         (2, 200_000 * N, 10_000 * N))
        self.assertIsNone(tunde[0]["employee_id"])

    def test_two_people_sharing_a_name_stay_two_people(self):
        adas = sorted(
            ((row["employee_id"], row["months"], row["gross"])
             for row in self.rows() if row["employee_name"] == "Ada Obi"),
            key=lambda row: (row[0] is None, row[0] or 0),
        )

        self.assertEqual(adas, sorted([
            (self.ada_one.pk, 2, 600_000 * N),
            (self.ada_two.pk, 1, 250_000 * N),
            (None, 1, 50_000 * N),
        ], key=lambda row: (row[0] is None, row[0] or 0)))

    def test_a_person_paid_before_and_after_their_salary_record_is_listed_once(self):
        ngozi = [row for row in self.rows() if row["employee_name"] == "Ngozi Eze"]

        self.assertEqual(len(ngozi), 1)
        self.assertEqual((ngozi[0]["months"], ngozi[0]["salary_id"], ngozi[0]["employee_id"]),
                         (2, self.ngozi_salary.pk, self.ngozi.pk))

    def test_the_totals_are_the_sum_of_every_line(self):
        from vs_finance.payslips import annual_paye_return

        result = annual_paye_return(self.books, year=2026)

        self.assertEqual(len(result["rows"]), 8)
        self.assertEqual(result["totals"]["gross"], 1_860_000 * N)

    def test_a_tax_number_typed_on_one_line_only_still_gives_one_row(self):
        musa = [row for row in self.rows() if row["employee_name"] == "Musa Bello"]

        self.assertEqual([(row["tax_id"], row["months"], row["gross"]) for row in musa],
                         [("TIN-001", 2, 160_000 * N)])

    def test_hand_typed_namesakes_with_different_tax_numbers_stay_apart(self):
        kemis = sorted((row["months"], row["gross"])
                       for row in self.rows() if row["employee_name"] == "Kemi Ade")

        self.assertEqual(kemis, [(1, 60_000 * N), (2, 140_000 * N)])
