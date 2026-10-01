"""The salary roster's rules: one row per person, dated moves, and who may change pay.

Corona Group has Ikeja, Lekki and Yaba. Tunde Bello is one person with one
account.

* **One active row per person.** Each member of staff is paid in full by one
  branch. Tunde's N288,888 is one row, and PAYE is worked out once on all of it;
  split as N234,567 at Ikeja and N54,321 at Lekki, the Lekki part would fall
  under the tax-free band and nothing would be withheld on it. Adding a second
  active row for him, at the same branch or another, is refused with a pointer
  to moving him instead, and moving him still works.
* **A move is dated, and the row changes hands on that date.** On 10 February
  the whole-school bursar moves Tunde from Ikeja to Lekki from 1 April. Until
  then Ikeja keeps him, pays his March, and opens his record; Lekki finds
  nothing. From 1 April it is the other way round, and his April payslip, from
  Lekki's run, counts Ikeja's N900,000 for January to March in his year to
  date, whoever opens it.
* **Changing pay needs the write switch.** Mrs Okafor's role may read every pay
  figure but change none: every payroll and salary write naming a pay field is
  refused, field by field, and nothing is written. Mr Adeyemi's role may change
  them, and the same writes succeed.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

from django.db import IntegrityError, transaction

from core.test_utils import TenantAPIClient
from vs_finance.models import (
    Account,
    EmployeeDeduction,
    EmployeeSalary,
    PayrollDeductionType,
    PayrollLine,
    PayrollRun,
    SalaryStructure,
)
from vs_finance.money import format_naira
from vs_finance.payroll import generate_run_from_roster, post_payroll, roster_for
from vs_finance.payroll_statutory import change_terms, record_creation
from vs_finance.payroll_tax import YearToDate, compute_paye

from .tests_payroll_statutory import N, _snapshot, _StatutoryFixture, _date


@contextmanager
def _on(day):
    """The tenant's and every branch's clock, fixed on ``day``."""
    with mock.patch("vs_config.clock.tenant_today", return_value=day), \
            mock.patch("vs_config.clock.branch_today", return_value=day):
        yield


def _january_paye(gross):
    """PAYE on ``gross`` for January from the 2026 table, with 8% pension and 2.5% NHF."""
    return compute_paye(
        _snapshot(), month=1, prior=YearToDate(), gross_now=gross, taxable_now=gross,
        pension_now=gross * 8 // 100, nhf_now=gross * 25 // 1000, annual_rent=0,
    ).amount


# --------------------------------------------------------------------------- #
# One active row per person                                                   #
# --------------------------------------------------------------------------- #

class OneActiveRowPerPersonTests(_StatutoryFixture):
    """Tunde is paid by one branch, on one row."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tunde_user = cls.user_for(cls.tenant, "tunde-one@corona.test")
        cls.tunde = cls.person(cls.books, "Tunde Bello", cls.ikeja, 288_888,
                               employee=cls.tunde_user)
        record_creation(cls.tunde)

    def setUp(self):
        super().setUp()
        self.hq = TenantAPIClient(user=self.hq_user)

    def path(self, suffix=""):
        return f"/v1/finance/employee-salaries/{suffix}?entity={self.books.code}"

    def add(self, branch, gross=54_321):
        return self.hq.post(self.path(), {
            "employee": self.tunde_user.pk, "branch": branch.pk, "gross_amount": gross * N,
        }, format="json")

    def test_a_second_active_row_is_refused_at_another_branch_and_at_the_same(self):
        for branch in (self.lekki, self.ikeja):
            with self.subTest(branch=branch.name):
                response = self.add(branch)

                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("Ikeja Branch", str(response.data))
                self.assertIn("move them", str(response.data))
        self.assertEqual(EmployeeSalary.objects.filter(employee=self.tunde_user).count(), 1)

    def test_putting_an_old_row_back_on_the_payroll_is_refused(self):
        old = self.person(self.books, "Tunde Bello", self.lekki, 54_321,
                          employee=self.tunde_user, is_active=False)

        response = self.hq.patch(self.path(f"{old.pk}/"), {"is_active": True}, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        old.refresh_from_db()
        self.assertFalse(old.is_active)

    def test_linking_an_active_row_to_somebody_already_paid_is_refused(self):
        unlinked = self.person(self.books, "T. Bello", self.lekki, 54_321)

        response = self.hq.patch(
            self.path(f"{unlinked.pk}/"), {"employee": self.tunde_user.pk}, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        unlinked.refresh_from_db()
        self.assertIsNone(unlinked.employee_id)

    def test_a_person_whose_old_row_is_off_the_payroll_may_be_added_again(self):
        self.hq.delete(self.path(f"{self.tunde.pk}/"))

        response = self.add(self.lekki)

        self.assertEqual(response.status_code, 201, response.data)

    def test_the_database_refuses_a_duplicate_the_service_did_not_see(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.person(self.books, "Tunde Bello", self.lekki, 54_321, employee=self.tunde_user)

    def test_moving_him_between_branches_still_works(self):
        response = self.hq.patch(self.path(f"{self.tunde.pk}/"), {"branch": self.lekki.pk},
                                 format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["branch_id"], self.lekki.pk)
        self.assertEqual(EmployeeSalary.objects.filter(employee=self.tunde_user).count(), 1)

    def test_paye_is_worked_out_once_on_his_whole_pay(self):
        run = generate_run_from_roster(self.books, pay_date=_date(1, 25))

        (line,) = PayrollLine.objects.filter(run=run, employee=self.tunde_user)
        self.assertEqual(line.gross_amount, 288_888 * N)
        self.assertEqual(line.paye_amount, _january_paye(288_888 * N))
        self.assertEqual(_january_paye(54_321 * N), 0)
        self.assertGreater(
            line.paye_amount, _january_paye(234_567 * N) + _january_paye(54_321 * N))


class DuplicatesAlreadyOnTheBooksTests(_StatutoryFixture):
    """A database that already holds two active rows for one person.

    The migration leaves the index out rather than fail or merge anybody's
    pay; the report lists the person, and installs the index once the rows are
    resolved. PostgreSQL's DDL is transactional, so dropping the index here is
    rolled back with the test.
    """

    INDEX = "uniq_active_roster_row_per_person"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tunde_user = cls.user_for(cls.tenant, "tunde-dup@corona.test")

    def setUp(self):
        super().setUp()
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(f'DROP INDEX "{self.INDEX}"')
        self.ikeja_row = self.person(self.books, "Tunde Bello", self.ikeja, 234_567,
                                     employee=self.tunde_user)
        self.lekki_row = self.person(self.books, "Tunde Bello", self.lekki, 54_321,
                                     employee=self.tunde_user)

    def installed(self):
        from vs_finance.management.commands.report_duplicate_roster_rows import index_installed

        return index_installed()

    def report(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("report_duplicate_roster_rows", *args, stdout=out)
        return out.getvalue()

    def test_the_migration_leaves_the_index_out_and_merges_nobody(self):
        import importlib

        from django.apps import apps
        from django.db import connection

        migration = importlib.import_module(
            "vs_finance.migrations.0050_one_active_roster_row_per_person")
        with connection.schema_editor() as editor:
            migration.install_if_clean(apps, editor)

        self.assertFalse(self.installed())
        self.assertEqual(
            EmployeeSalary.objects.filter(employee=self.tunde_user, is_active=True).count(), 2)

    def test_the_report_lists_him_and_installs_the_index_once_resolved(self):
        from django.core.management.base import CommandError

        listed = self.report()
        self.assertIn("tunde-dup@corona.test", listed)
        self.assertIn(f"row {self.lekki_row.pk}", listed)
        with self.assertRaises(CommandError):
            self.report("--install")
        self.assertFalse(self.installed())

        from django.db import connection

        EmployeeSalary.objects.filter(pk=self.lekki_row.pk).update(is_active=False)
        # The test's own inserts leave deferred checks pending, which CREATE INDEX refuses.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.report("--install")

        self.assertTrue(self.installed())


# --------------------------------------------------------------------------- #
# A dated move hands the row over on its date                                 #
# --------------------------------------------------------------------------- #

class DatedMoveTests(_StatutoryFixture):
    """Tunde moves from Ikeja to Lekki on 1 April, a move entered on 10 February."""

    @classmethod
    def setUpTestData(cls):
        from .tests_payroll_branch import _PayrollFixture

        super().setUpTestData()
        with _on(_date(1, 2)):
            _PayrollFixture.set_scope(cls.tenant, "PER_BRANCH")
        cls.tunde_user = cls.user_for(cls.tenant, "tunde-move@corona.test")
        cls.tunde = cls.person(cls.books, "Tunde Bello", cls.ikeja, 300_000,
                               employee=cls.tunde_user)
        record_creation(cls.tunde)
        cls.person(cls.books, "Bola Lawal", cls.lekki, 80_000)

        cls.runs = {}
        cls.runs[1] = cls.raise_run(1, cls.ikeja)
        with _on(_date(2, 10)):
            change_terms(cls.tunde, {"branch_id": cls.lekki.pk}, effective_from=_date(4, 1))
        for month in (2, 3):
            cls.runs[month] = cls.raise_run(month, cls.ikeja)
        cls.runs[4] = cls.raise_run(4, cls.lekki)

        cls.ikeja_user = cls.grant(cls.user_for(cls.tenant, "move-ikeja@corona.test"),
                                   *cls.KEYS, tenant=cls.tenant, role_key="move-ikeja",
                                   branch=cls.ikeja)
        cls.lekki_user = cls.grant(cls.user_for(cls.tenant, "move-lekki@corona.test"),
                                   *cls.KEYS, tenant=cls.tenant, role_key="move-lekki",
                                   branch=cls.lekki)

    @classmethod
    def raise_run(cls, month, branch):
        run = generate_run_from_roster(cls.books, pay_date=_date(month, 25), branch=branch)
        post_payroll(run)
        return run

    def setUp(self):
        super().setUp()
        self.ikeja_bursar = TenantAPIClient(user=self.ikeja_user)
        self.lekki_bursar = TenantAPIClient(user=self.lekki_user)
        self.hq = TenantAPIClient(user=self.hq_user)

    def get(self, client, suffix):
        joiner = "&" if "?" in suffix else "?"
        return client.get(f"/v1/finance/{suffix}{joiner}entity={self.books.code}")

    def tunde_line(self, month):
        return self.runs[month].lines.get(salary=self.tunde)

    def test_ikeja_pays_him_to_march_and_lekki_from_april(self):
        self.assertEqual(
            [self.tunde_line(m).branch_id for m in (1, 2, 3, 4)],
            [self.ikeja.pk, self.ikeja.pk, self.ikeja.pk, self.lekki.pk],
        )
        self.assertEqual(self.runs[4].branch_id, self.lekki.pk)

    def test_on_15_march_ikeja_still_has_him_and_lekki_does_not(self):
        with _on(_date(3, 15)):
            ikeja = self.get(self.ikeja_bursar, f"employee-salaries/{self.tunde.pk}/history/")
            lekki = self.get(self.lekki_bursar, f"employee-salaries/{self.tunde.pk}/history/")
            ikeja_roster = self.get(self.ikeja_bursar, "employee-salaries/")
            lekki_roster = self.get(self.lekki_bursar, "employee-salaries/")

        self.assertEqual(ikeja.status_code, 200, ikeja.data)
        self.assertEqual(lekki.status_code, 404)
        (row,) = [r for r in ikeja_roster.data["data"] if r["id"] == self.tunde.pk]
        self.assertEqual((row["branch_id"], row["branch_name"]), (self.ikeja.pk, "Ikeja Branch"))
        self.assertNotIn(self.tunde.pk, [r["id"] for r in lekki_roster.data["data"]])
        self.assertEqual(
            list(roster_for(self.books, self.lekki, on=_date(3, 15)).values_list("name", flat=True)),
            ["Bola Lawal"],
        )

    def test_on_2_april_lekki_has_his_whole_year_and_ikeja_does_not(self):
        with _on(_date(4, 2)):
            ikeja = self.get(self.ikeja_bursar, f"employee-salaries/{self.tunde.pk}/history/")
            lekki = self.get(self.lekki_bursar, f"employee-salaries/{self.tunde.pk}/history/")
            summary = self.get(
                self.lekki_bursar, f"employee-salaries/{self.tunde.pk}/tax-summary/?year=2026")

        self.assertEqual(ikeja.status_code, 404)
        self.assertEqual(lekki.status_code, 200, lekki.data)
        self.assertEqual([v["branch_id"] for v in lekki.data["data"]],
                         [self.ikeja.pk, self.lekki.pk])
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["data"]["totals"]["gross"], 4 * 300_000 * N)

    def test_his_april_payslip_counts_ikejas_months_whoever_opens_it(self):
        line = self.tunde_line(4)
        path = f"payroll-runs/{self.runs[4].pk}/lines/{line.pk}/payslip/?output=json"
        with _on(_date(4, 2)):
            lekki = self.get(self.lekki_bursar, path)
            whole = self.get(self.hq, path)
            ikeja = self.get(self.ikeja_bursar, path)

        self.assertEqual(lekki.status_code, 200, lekki.data)
        ytd = lekki.data["data"]["ytd"]
        self.assertEqual(ytd["gross"], format_naira((900_000 + 300_000) * N))
        self.assertEqual(whole.data["data"]["ytd"], ytd)
        self.assertEqual(ikeja.status_code, 404)
        self.assertEqual(line.tax_basis["inputs"]["gross_before"], 900_000 * N)


# --------------------------------------------------------------------------- #
# Changing pay needs the field's write switch                                 #
# --------------------------------------------------------------------------- #

class PayWriteSwitchTests(_StatutoryFixture):
    """Mrs Okafor reads pay and changes none of it; Mr Adeyemi changes it."""

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.models import FieldDefinition, TenantRoleTemplate
        from vs_rbac.tests.helpers import install_declared_fields, set_field_access

        super().setUpTestData()
        keys = install_declared_fields("finance.salary", "finance.payrollrun")
        writable = set(
            FieldDefinition.objects.filter(key__in=keys, writable=True).values_list("key", flat=True)
        )
        cls.okafor_user = cls.grant(cls.user_for(cls.tenant, "okafor@corona.test"), *cls.KEYS,
                                    tenant=cls.tenant, role_key="pay-read-only")
        cls.adeyemi_user = cls.grant(cls.user_for(cls.tenant, "adeyemi@corona.test"), *cls.KEYS,
                                     tenant=cls.tenant, role_key="pay-writer")
        read_only = TenantRoleTemplate.objects.get(tenant=cls.tenant, key="pay-read-only")
        writer = TenantRoleTemplate.objects.get(tenant=cls.tenant, key="pay-writer")
        set_field_access(read_only, *keys, read=True, write=False)
        set_field_access(writer, *writable, read=True, write=True)
        set_field_access(writer, *(set(keys) - writable), read=True, write=False)

        cls.tunde = cls.person(cls.books, "Tunde Bello", cls.ikeja, 200_000)
        record_creation(cls.tunde)
        liability = Account.objects.create(entity=cls.books, code="2451", name="Staff loans",
                                           account_type="LIABILITY", is_postable=True)
        cls.loan = PayrollDeductionType.objects.create(
            entity=cls.books, code="LOAN", name="Staff loan", liability_account=liability)
        cls.deduction = EmployeeDeduction.objects.create(
            salary=cls.tunde, deduction_type=cls.loan, amount=5_000 * N)
        cls.structure = SalaryStructure.objects.create(entity=cls.books, name="Teachers")

    def setUp(self):
        super().setUp()
        self.okafor = TenantAPIClient(user=self.okafor_user)
        self.adeyemi = TenantAPIClient(user=self.adeyemi_user)

    def call(self, client, method, suffix, body):
        return getattr(client, method)(
            f"/v1/finance/{suffix}?entity={self.books.code}", body, format="json")

    def writes(self):
        """``(group, method, path, body, field named in the refusal)`` for every pay write."""
        salary = f"employee-salaries/{self.tunde.pk}/"
        component = {"name": "Basic", "kind": "EARNING", "calc_method": "PERCENT_OF_GROSS",
                     "rate_bps": 10_000, "is_basic": True}
        return [
            ("gross", "post", "employee-salaries/",
             {"name": "Dele Ade", "branch": self.ikeja.pk, "gross_amount": 90_000 * N},
             "gross_amount"),
            ("gross", "patch", salary, {"gross_amount": 250_000 * N}, "gross_amount"),
            ("paye", "patch", salary, {"tax_id": "TIN-TUNDE"}, "tax_id"),
            ("paye", "patch", salary,
             {"paye_override": 1_000 * N, "paye_override_reason": "Second employer"},
             "paye_override"),
            ("paye", "patch", salary, {"annual_rent": 1_200_000 * N}, "annual_rent"),
            ("paye", "patch", salary, {"residence_state": "OG"}, "residence_state"),
            ("pension", "patch", salary, {"pension_pin": "PEN-TUNDE"}, "pension_pin"),
            ("pension", "patch", salary, {"pfa": self.stanbic.pk}, "pfa"),
            ("pension", "patch", salary, {"pension_amount": 9_000 * N}, "pension_amount"),
            ("breakdown", "post", f"{salary}deductions/",
             {"deduction_type": self.loan.pk, "amount": 2_000 * N}, "amount"),
            ("breakdown", "patch", f"employee-deductions/{self.deduction.pk}/",
             {"total_limit": 50_000 * N}, "total_limit"),
            ("breakdown", "patch", salary, {"structure": self.structure.pk}, "structure"),
            ("breakdown", "post", "salary-structures/",
             {"name": "Senior staff", "components": [component]}, "components"),
            ("breakdown", "patch", f"salary-structures/{self.structure.pk}/",
             {"components": [component]}, "components"),
            ("run lines", "post", "payroll-runs/",
             {"pay_date": "2026-01-25", "branch": self.ikeja.pk,
              "lines": [{"employee_name": "Casual", "gross_amount": 10_000 * N}]},
             "gross_amount"),
        ]

    def snapshot(self):
        tunde = EmployeeSalary.objects.get(pk=self.tunde.pk)
        return (
            tunde.gross_amount, tunde.pension_amount, tunde.tax_id, tunde.pension_pin,
            tunde.pfa_id, tunde.residence_state_id, tunde.annual_rent, tunde.paye_override,
            tunde.structure_id, tunde.versions.count(),
            EmployeeSalary.objects.filter(entity=self.books).count(),
            list(EmployeeDeduction.objects.values_list("amount", "total_limit")),
            SalaryStructure.objects.filter(entity=self.books).count(),
            self.structure.components.count(), PayrollRun.objects.filter(entity=self.books).count(),
        )

    def test_a_read_only_pay_role_is_refused_every_pay_write_and_nothing_is_written(self):
        before = self.snapshot()
        for group, method, path, body, field in self.writes():
            with self.subTest(group=group, path=path, field=field):
                response = self.call(self.okafor, method, path, body)

                self.assertEqual(response.status_code, 403, response.data)
                self.assertIn(field, str(response.data))
        self.assertEqual(self.snapshot(), before)

    def test_a_role_with_the_write_switch_makes_every_pay_write(self):
        for group, method, path, body, field in self.writes():
            with self.subTest(group=group, path=path, field=field):
                response = self.call(self.adeyemi, method, path, body)

                self.assertIn(response.status_code, (200, 201), response.data)

    def test_a_read_only_pay_role_still_changes_what_is_not_pay(self):
        response = self.call(self.okafor, "patch", f"employee-salaries/{self.tunde.pk}/",
                             {"name": "Tunde A. Bello"})

        self.assertEqual(response.status_code, 200, response.data)
        self.tunde.refresh_from_db()
        self.assertEqual(self.tunde.name, "Tunde A. Bello")
