"""The roster shows the pay in force today, and the next dated change beside it.

Corona Group has Ikeja, Lekki and Yaba; Single Site has one branch. Today is
5 October 2026.

* **Aisha Musa** is on N300,000 at Ikeja. Her raise to N320,000 from
  1 January 2027 was entered today. Until January the roster shows N300,000
  and carries the raise under ``next_terms``; from January it shows N320,000
  and nothing is next.
* **Musa Bello** is on N200,000 at Ikeja and moves to Lekki from 1 November:
  Ikeja is his branch today, and Lekki is the branch of his next terms.
* **Grace Eze** at Single Site is on N150,000 with a raise to N180,000 from
  January, read the same way at a school with one branch.

The edit form sends back what the roster showed. Mrs Okafor may read pay but
not change it: her form echoing Aisha's N300,000 saves, and the raise stays in
January; sending N320,000 now is a change and is refused. Mr Adeyemi may change
pay: moving Aisha's cost centre now neither brings the raise forward nor loses
the new cost centre in January, and N300,000 dated from January cancels the
raise. The state, PFA and structure follow the PAYE, pension and pay breakdown
write switches, and the ``/me`` map and the saved row say so.
"""
from __future__ import annotations

import datetime

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from core.test_utils import TenantAPIClient
from vs_finance.models import CostCenter, EmployeeSalary, SalaryComponent, SalaryStructure
from vs_finance.payroll_statutory import change_terms, record_creation
from vs_rbac.field_enforcement import field_access_payload

from .tests_payroll_roster_rules import _on, _PayRolesFixture
from .tests_payroll_statutory import N

TODAY = datetime.date(2026, 10, 5)
JANUARY = datetime.date(2027, 1, 1)
ALIASES = {"residence_state", "pfa", "structure"}


class _TermsFixture(_PayRolesFixture):
    """Aisha's raise from January, Musa's move from November, Grace at Single Site."""

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import set_field_access

        super().setUpTestData()
        cls.hidden_user = cls.grant(cls.user_for(cls.tenant, "terms-hidden@corona.test"),
                                    *cls.KEYS, tenant=cls.tenant, role_key="terms-hidden")
        set_field_access(TenantRoleTemplate.objects.get(tenant=cls.tenant, key="terms-hidden"),
                         *cls.pay_keys, read=False, write=False)
        set_field_access(TenantRoleTemplate.objects.get(tenant=cls.solo_tenant, key="stat-solo"),
                         *cls.pay_keys, read=True, write=True)
        cls.admin_cc = CostCenter.objects.create(entity=cls.books, code="ADMIN", name="Admin")
        cls.academic_cc = CostCenter.objects.create(entity=cls.books, code="ACAD", name="Academic")

        cls.aisha = cls.person(cls.books, "Aisha Musa", cls.ikeja, 300_000,
                               cost_center=cls.admin_cc)
        cls.musa = cls.person(cls.books, "Musa Bello", cls.ikeja, 200_000)
        cls.grace = cls.person(cls.solo_books, "Grace Eze", cls.solo_main, 150_000)
        for salary in (cls.aisha, cls.musa, cls.grace):
            record_creation(salary)
        with _on(TODAY):
            change_terms(cls.aisha, {"gross_amount": 320_000 * N}, effective_from=JANUARY)
            change_terms(cls.musa, {"branch_id": cls.lekki.pk},
                         effective_from=datetime.date(2026, 11, 1))
            change_terms(cls.grace, {"gross_amount": 180_000 * N}, effective_from=JANUARY)

    def setUp(self):
        super().setUp()
        self.clock = _on(TODAY)
        self.clock.__enter__()
        self.addCleanup(self.clock.__exit__, None, None, None)

    def roster(self, client, books=None):
        books = books or self.books
        response = client.get(f"/v1/finance/employee-salaries/?entity={books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"]: row for row in response.data["data"]}

    def form(self, salary, **changes):
        """The edit form as it saves: every value the roster showed, and *changes*."""
        row = self.roster(self.okafor)[salary.pk]
        body = {
            "name": row["name"], "gross_amount": row["gross_amount"],
            "structure": row["structure_id"], "residence_state": row["residence_state"],
            "pfa": row["pfa_id"], "cost_center": row["cost_center"],
            "branch": row["branch_id"], "is_active": row["is_active"],
        }
        body.update(changes)
        return body

    def patch(self, client, salary, body):
        return client.patch(
            f"/v1/finance/employee-salaries/{salary.pk}/?entity={self.books.code}",
            body, format="json")

    def terms(self, salary, day):
        return EmployeeSalary.objects.get(pk=salary.pk).terms_on(day)


class RosterShowsTodaysTermsTests(_TermsFixture):

    def test_today_aisha_shows_her_current_pay_and_the_raise_is_next(self):
        row = self.roster(self.adeyemi)[self.aisha.pk]

        self.assertEqual((row["gross_amount"], row["net_amount"]), (300_000 * N, 300_000 * N))
        self.assertEqual(row["terms_effective_from"], "1900-01-01")
        self.assertEqual(row["cost_center"], "ADMIN")
        self.assertEqual(row["next_terms"], {
            "effective_from": "2027-01-01", "branch_id": self.ikeja.pk,
            "branch_name": "Ikeja Branch", "gross_amount": 320_000 * N,
            "paye_amount": 0, "pension_amount": 0,
        })

    def test_from_january_the_raise_is_in_force_and_nothing_is_next(self):
        with _on(datetime.date(2027, 1, 2)):
            row = self.roster(self.adeyemi)[self.aisha.pk]

        self.assertEqual(row["gross_amount"], 320_000 * N)
        self.assertEqual(row["terms_effective_from"], "2027-01-01")
        self.assertIsNone(row["next_terms"])

    def test_a_move_dated_next_month_is_next_with_its_branch(self):
        row = self.roster(self.adeyemi)[self.musa.pk]

        self.assertEqual((row["branch_id"], row["branch_name"]), (self.ikeja.pk, "Ikeja Branch"))
        self.assertEqual(
            {k: row["next_terms"][k] for k in ("effective_from", "branch_id", "branch_name",
                                                "gross_amount")},
            {"effective_from": "2026-11-01", "branch_id": self.lekki.pk,
             "branch_name": "Lekki Branch", "gross_amount": 200_000 * N},
        )

    def test_a_school_with_one_branch_reads_the_same_way(self):
        row = self.roster(TenantAPIClient(user=self.solo_user), self.solo_books)[self.grace.pk]

        self.assertEqual(row["gross_amount"], 150_000 * N)
        self.assertEqual((row["next_terms"]["effective_from"], row["next_terms"]["gross_amount"]),
                         ("2027-01-01", 180_000 * N))

    def test_a_caller_who_may_not_read_pay_learns_when_but_not_what(self):
        row = self.roster(TenantAPIClient(user=self.hidden_user))[self.aisha.pk]

        self.assertNotIn("gross_amount", row)
        self.assertEqual(row["next_terms"], {
            "effective_from": "2027-01-01", "branch_id": self.ikeja.pk,
            "branch_name": "Ikeja Branch",
        })

    def test_the_roster_costs_the_same_queries_for_one_row_and_three(self):
        from vs_finance.models import PayrollTaxJurisdiction, PensionFundAdministrator

        solo = TenantAPIClient(user=self.solo_user)
        structure = SalaryStructure.objects.create(entity=self.solo_books, name="Teachers")
        SalaryComponent.objects.create(
            structure=structure, name="Basic", kind="EARNING", calc_method="PERCENT_OF_GROSS",
            rate_bps=10_000, is_basic=True, sequence=0)
        lagos = PayrollTaxJurisdiction.objects.get(country="NG", code="LA")
        stanbic = PensionFundAdministrator.objects.get(code="STANBIC")
        EmployeeSalary.objects.filter(pk=self.grace.pk).update(
            structure=structure, residence_state=lagos, pfa=stanbic)
        self.grace.versions.update(structure=structure, residence_state=lagos)
        self.roster(solo, self.solo_books)
        with CaptureQueriesContext(connection) as one:
            self.assertEqual(len(self.roster(solo, self.solo_books)), 1)

        for name in ("Ade Ola", "Bisi Ojo"):
            salary = self.person(
                self.solo_books, name, self.solo_main, 120_000, structure=structure,
                residence_state=lagos, pfa=stanbic)
            record_creation(salary)
            change_terms(salary, {"gross_amount": 130_000 * N}, effective_from=JANUARY)
        with CaptureQueriesContext(connection) as three:
            self.assertEqual(len(self.roster(solo, self.solo_books)), 3)

        self.assertEqual(len(three), len(one), [q["sql"] for q in three.captured_queries])


class EditFormAgainstTodaysTermsTests(_TermsFixture):

    def test_a_read_only_form_echoing_todays_pay_saves_and_the_raise_stays_in_january(self):
        versions = self.aisha.versions.count()

        response = self.patch(self.okafor, self.aisha, self.form(self.aisha, name="Aisha Musa-Bello"))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.aisha.versions.count(), versions)
        self.assertEqual(self.terms(self.aisha, datetime.date(2026, 12, 31)).gross_amount,
                         300_000 * N)
        self.assertEqual(self.terms(self.aisha, JANUARY).gross_amount, 320_000 * N)
        self.assertEqual(response.data["data"]["gross_amount"], 300_000 * N)

    def test_a_read_only_form_sending_the_raise_now_is_refused(self):
        response = self.patch(self.okafor, self.aisha,
                              self.form(self.aisha, gross_amount=320_000 * N))

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(sorted(response.data["error"]["detail"]), ["gross_amount"])

    def test_a_cost_centre_change_neither_brings_the_raise_forward_nor_loses_it(self):
        response = self.patch(self.adeyemi, self.aisha,
                              self.form(self.aisha, cost_center="ACAD"))

        self.assertEqual(response.status_code, 200, response.data)
        november = self.terms(self.aisha, datetime.date(2026, 11, 30))
        january = self.terms(self.aisha, JANUARY)
        self.assertEqual((november.gross_amount, november.cost_center_id),
                         (300_000 * N, self.academic_cc.pk))
        self.assertEqual((january.gross_amount, january.cost_center_id),
                         (320_000 * N, self.academic_cc.pk))
        self.assertEqual(response.data["data"]["next_terms"]["gross_amount"], 320_000 * N)

    def test_todays_pay_dated_from_january_cancels_the_raise(self):
        body = {"gross_amount": 300_000 * N, "effective_from": "2027-01-01"}
        refused = self.patch(self.okafor, self.aisha, body)
        self.assertEqual(refused.status_code, 403, refused.data)

        response = self.patch(self.adeyemi, self.aisha, body)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.terms(self.aisha, JANUARY).gross_amount, 300_000 * N)

    def test_a_pay_correction_now_keeps_a_raise_that_sets_its_own_figure(self):
        response = self.patch(self.adeyemi, self.aisha, {"gross_amount": 310_000 * N})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.terms(self.aisha, datetime.date(2026, 12, 31)).gross_amount,
                         310_000 * N)
        self.assertEqual(self.terms(self.aisha, JANUARY).gross_amount, 320_000 * N)


class AliasSwitchesVisibleTests(_TermsFixture):
    """State, PFA and structure are read-only exactly where the switch they follow is."""

    def test_the_me_map_lists_them_for_a_read_only_role_and_not_for_a_writer(self):
        read_only = field_access_payload(self.okafor_user, self.tenant)["finance.salary"]
        writer = field_access_payload(self.adeyemi_user, self.tenant).get(
            "finance.salary", {"read_only": [], "hidden": []})

        self.assertTrue(ALIASES <= set(read_only["read_only"]), read_only)
        self.assertFalse(ALIASES & set(read_only["hidden"]))
        self.assertFalse(ALIASES & set(writer["read_only"]), writer)

    def test_a_role_that_may_not_read_pay_still_reads_them_and_may_not_change_them(self):
        hidden = field_access_payload(self.hidden_user, self.tenant)["finance.salary"]

        self.assertTrue(ALIASES <= set(hidden["read_only"]), hidden)
        self.assertFalse(ALIASES & set(hidden["hidden"]))

    def test_the_saved_row_lists_them_for_a_read_only_caller_only(self):
        okafor = self.patch(self.okafor, self.aisha, {"name": "Aisha Musa"})
        adeyemi = self.patch(self.adeyemi, self.aisha, {"name": "Aisha Musa"})

        self.assertEqual(okafor.status_code, 200, okafor.data)
        self.assertTrue(ALIASES <= set(okafor.data["data"]["_read_only_fields"]))
        self.assertFalse(ALIASES & set(adeyemi.data["data"]["_read_only_fields"]))


class WriteAliasDeclarationTests(SimpleTestCase):
    """A write alias follows a writable field and is not a field's own client name."""

    def declaration(self, aliases):
        from vs_rbac.field_registry import FieldDeclaration, FieldSpec

        return FieldDeclaration(
            module="demo", resource="pay", surfaces=(),
            fields=(FieldSpec("gross", "Gross", scope="TENANT"),
                    FieldSpec("net", "Net", scope="TENANT", writable=False)),
            write_aliases=tuple(aliases.items()),
        )

    def test_an_alias_of_a_writable_field_is_accepted(self):
        from vs_rbac.field_registry import validate_declaration

        validate_declaration(self.declaration({"grade": "gross"}))

    def test_an_alias_of_a_field_nothing_writes_or_of_a_client_name_is_refused(self):
        from vs_rbac.field_registry import validate_declaration

        for aliases in ({"grade": "net"}, {"grade": "missing"}, {"gross": "gross"}):
            with self.assertRaises(ValueError, msg=aliases):
                validate_declaration(self.declaration(aliases))
