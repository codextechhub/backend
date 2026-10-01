"""Who reads the statutory payroll records: a branch's bursar, the whole school, the person paid.

Corona Group pays everyone in one central January run, booked one journal per
branch. Ada Obi works at Ikeja for N123,456 a month, with tax ID TIN-ADA,
pension PIN PEN-ADA and a cooperative deduction. Chidi Eze also works at Ikeja
but lives in Ogun State, so Ogun's PAYE return is Ikeja's alone. Bola Lawal
works at Lekki for N80,000. Tunde Bello teaches at both branches and has a
roster row at each, N234,567 at Ikeja and N54,321 at Lekki, for one account.

Ngozi keeps Lekki's books, pinned to Lekki. Wherever a salary, a deduction, a
statutory figure or a person's tax details can be read, they see Lekki's staff
and Lekki's amounts and nothing of Ikeja's: no name, no figure, and no total
with an Ikeja figure inside it. Tunde's Lekki payslip counts only what Lekki
paid him, the same year to date his PAYE was worked out on. Mr Bello, the
whole-school bursar, reads everything. Staff read their own payslips and no
colleague's, whatever role they hold. Rival Group's bursar reads none of it.
"""
from __future__ import annotations

from unittest import mock

from core.test_utils import TenantAPIClient
from vs_finance.models import (
    Account,
    EmployeeDeduction,
    PayrollDeductionType,
    PayrollLine,
    Payslip,
    TaxObligation,
)
from vs_finance.money import format_naira
from vs_finance.payroll import generate_run_from_roster, pay_payroll, post_payroll
from vs_finance.payroll_statutory import record_creation

from .tests_payroll_statutory import N, _StatutoryFixture, _date

#: Text that names, or is a figure of, Ikeja's staff and nothing of Lekki's.
IKEJA_ONLY = (
    "Ada Obi", "Chidi Eze", "TIN-ADA", "PEN-ADA", "Ikeja",
    str(123_456 * N), str(234_567 * N), "123,456", "234,567",
)


class _ReachFixture(_StatutoryFixture):
    """Corona's January run, posted and paid, with Ikeja's and Lekki's staff on it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ada_user = cls.user_for(cls.tenant, "ada@corona.test")
        cls.bola_user = cls.user_for(cls.tenant, "bola@corona.test")
        cls.tunde_user = cls.user_for(cls.tenant, "tunde@corona.test")
        cls.ada = cls.person(cls.books, "Ada Obi", cls.ikeja, 123_456, employee=cls.ada_user,
                             tax_id="TIN-ADA", pension_pin="PEN-ADA", pfa=cls.stanbic)
        cls.chidi = cls.person(cls.books, "Chidi Eze", cls.ikeja, 90_000, residence_state=cls.ogun)
        cls.bola = cls.person(cls.books, "Bola Lawal", cls.lekki, 80_000, employee=cls.bola_user,
                              tax_id="TIN-BOLA")
        cls.tunde_ikeja = cls.person(cls.books, "Tunde Bello", cls.ikeja, 234_567,
                                     employee=cls.tunde_user)
        cls.tunde_lekki = cls.person(cls.books, "Tunde Bello", cls.lekki, 54_321,
                                     employee=cls.tunde_user)
        for salary in (cls.ada, cls.chidi, cls.bola, cls.tunde_ikeja, cls.tunde_lekki):
            record_creation(salary)
        coop = Account.objects.create(entity=cls.books, code="2450", name="Cooperative savings",
                                      account_type="LIABILITY", is_postable=True)
        cls.coop = PayrollDeductionType.objects.create(
            entity=cls.books, code="COOP", name="Cooperative savings", liability_account=coop)
        cls.ada_coop = EmployeeDeduction.objects.create(
            salary=cls.ada, deduction_type=cls.coop, amount=7_777 * N)

        cls.payroll_run = generate_run_from_roster(cls.books, pay_date=_date(1, 25))
        post_payroll(cls.payroll_run)
        pay_payroll(cls.payroll_run, bank_accounts=[
            cls.bank(cls.books, cls.ikeja, "1191"), cls.bank(cls.books, cls.lekki, "1192"),
        ])
        cls.lines = {
            line.salary_id: line for line in PayrollLine.objects.filter(run=cls.payroll_run)
        }

        cls.ngozi_user = cls.grant(cls.user_for(cls.tenant, "ngozi@corona.test"),
                                   *cls.KEYS, "finance.audit.view",
                                   tenant=cls.tenant, role_key="reach-lekki", branch=cls.lekki)
        cls.bello_user = cls.grant(cls.user_for(cls.tenant, "bello@corona.test"),
                                   *cls.KEYS, "finance.audit.view",
                                   tenant=cls.tenant, role_key="reach-hq")

    def setUp(self):
        super().setUp()
        self.ngozi = TenantAPIClient(user=self.ngozi_user)
        self.bello = TenantAPIClient(user=self.bello_user)

    def line(self, salary):
        return self.lines[salary.pk]

    def get(self, client, path, books=None):
        joiner = "&" if "?" in path else "?"
        return client.get(f"/v1/finance/{path}{joiner}entity={(books or self.books).code}")

    def assert_nothing_of_ikeja(self, response):
        text = response.content.decode(errors="replace") if not hasattr(response, "data") \
            else str(response.data)
        for leaked in IKEJA_ONLY:
            self.assertNotIn(leaked, text)


class PayslipYearToDateTests(_ReachFixture):
    """A payslip's year to date is its own roster row's, as the PAYE working is."""

    def payslip_path(self, salary):
        return f"payroll-runs/{self.payroll_run.pk}/lines/{self.line(salary).pk}/payslip/"

    def test_tundes_lekki_payslip_counts_only_what_lekki_paid_him(self):
        response = self.get(self.ngozi, self.payslip_path(self.tunde_lekki) + "?output=json")

        self.assertEqual(response.status_code, 200, response.data)
        ytd = response.data["data"]["ytd"]
        self.assertEqual(ytd["gross"], format_naira(54_321 * N))
        self.assertEqual(ytd["paye"], format_naira(self.line(self.tunde_lekki).paye_amount))
        self.assert_nothing_of_ikeja(response)

    def test_the_pdf_prints_the_same_year_to_date(self):
        with mock.patch("vs_finance.pdf.payslip_pdf", return_value=b"%PDF-1.4") as render:
            response = self.get(self.ngozi, self.payslip_path(self.tunde_lekki))

        self.assertEqual(response.status_code, 200)
        (context,), _ = render.call_args
        self.assertEqual(context["ytd"]["gross"], format_naira(54_321 * N))
        self.assertNotIn(format_naira((234_567 + 54_321) * N), str(context))

    def test_tundes_own_payslip_agrees_with_the_paye_it_was_worked_out_on(self):
        line = self.line(self.tunde_lekki)
        payslip = Payslip.objects.get(line=line)

        response = TenantAPIClient(user=self.tunde_user).get(f"/v1/finance/my-payslips/{payslip.pk}/")

        self.assertEqual(response.status_code, 200, response.data)
        ytd = response.data["data"]["ytd"]
        self.assertEqual(line.tax_basis["inputs"]["gross_before"], 0)
        self.assertEqual((ytd["gross"], ytd["paye"]),
                         (format_naira(54_321 * N), format_naira(line.paye_amount)))

    def test_the_whole_school_reader_sees_the_same_payslip(self):
        response = self.get(self.bello, self.payslip_path(self.tunde_lekki) + "?output=json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["ytd"]["gross"], format_naira(54_321 * N))


def _prepare(entity, code):
    """January's draft return for the obligation ``code``."""
    from vs_finance.tax_filing import prepare_filing

    obligation = TaxObligation.objects.get(entity=entity, code=code)
    return prepare_filing(obligation, period_start=_date(1, 1), period_end=_date(1, 31))


class LekkiBursarSeesNothingOfIkejaTests(_ReachFixture):
    """Ngozi, pinned to Lekki, in every place a salary or a statutory figure is read."""

    def ok(self, path):
        response = self.get(self.ngozi, path)
        self.assertEqual(response.status_code, 200, getattr(response, "data", response))
        self.assert_nothing_of_ikeja(response)
        return response.data["data"]

    def lekki_lines(self):
        return [self.line(self.bola), self.line(self.tunde_lekki)]

    def test_the_run_holds_lekkis_lines_and_lekkis_totals_only(self):
        data = self.ok(f"payroll-runs/{self.payroll_run.pk}/")

        lekki = self.lekki_lines()
        self.assertTrue(data["partial_view"])
        self.assertEqual(sorted(row["id"] for row in data["lines"]), sorted(l.pk for l in lekki))
        for total, field in (("gross_total", "gross_amount"), ("paye_total", "paye_amount"),
                             ("other_deductions_total", "other_deductions_amount"),
                             ("employer_contributions_total", "employer_contributions_amount"),
                             ("net_total", "net_amount")):
            self.assertEqual(data[total], sum(getattr(l, field) for l in lekki), total)
        self.assertEqual([s["branch_name"] for s in data["branch_shares"]], ["Lekki Branch"])

    def test_the_runs_list_and_the_summary_count_lekki_alone(self):
        (row,) = self.ok("payroll-runs/")
        net = sum(l.net_amount for l in self.lekki_lines())
        self.assertEqual(row["net_total"], net)

        summary = self.ok("payroll-runs/summary/")
        self.assertEqual((summary["employees"], summary["net"]), (2, net))

    def test_the_roster_lists_lekkis_staff_only(self):
        rows = self.ok("employee-salaries/")
        self.assertEqual(sorted(r["id"] for r in rows), sorted([self.bola.pk, self.tunde_lekki.pk]))

    def test_every_record_of_an_ikeja_person_is_not_found(self):
        for salary in (self.ada, self.tunde_ikeja):
            line = self.line(salary)
            for path in (
                f"employee-salaries/{salary.pk}/history/",
                f"employee-salaries/{salary.pk}/deductions/",
                f"employee-salaries/{salary.pk}/tax-summary/?year=2026",
                f"employee-salaries/{salary.pk}/tax-summary/?year=2026&output=pdf",
                f"payroll-runs/{self.payroll_run.pk}/lines/{line.pk}/payslip/",
                f"payroll-runs/{self.payroll_run.pk}/lines/{line.pk}/payslip/?output=json",
            ):
                with self.subTest(person=salary.name, path=path):
                    response = self.get(self.ngozi, path)
                    self.assertEqual(response.status_code, 404)
                    self.assert_nothing_of_ikeja(response)

    def test_lekkis_own_records_open(self):
        self.ok(f"employee-salaries/{self.bola.pk}/history/")
        summary = self.ok(f"employee-salaries/{self.bola.pk}/tax-summary/?year=2026")
        self.assertEqual(summary["totals"]["gross"], 80_000 * N)

    def test_the_paye_schedule_holds_lekkis_people_and_lekkis_amounts(self):
        filing = _prepare(self.books, "PAYE-LA")

        data = self.ok(f"tax-filings/{filing.pk}/schedule/")

        self.assertIn("Bola Lawal", [r["employee_name"] for r in data["rows"]])
        self.assertEqual({r["branch_name"] for r in data["rows"]}, {"Lekki Branch"})
        lekki_share = filing.shares.get(branch=self.lekki)
        self.assertEqual(data["total"], lekki_share.gross_liability)
        self.assertEqual(data["employee_total"], sum(r["employee_amount"] for r in data["rows"]))

    def test_the_return_reads_lekkis_share_only(self):
        filing = _prepare(self.books, "PAYE-LA")

        data = self.ok(f"tax-filings/{filing.pk}/")

        self.assertEqual(data["gross_liability"], filing.shares.get(branch=self.lekki).gross_liability)

    def test_a_return_with_no_lekki_share_is_not_found(self):
        for code in ("PAYE-OG", "PENSION-STANBIC"):
            filing = _prepare(self.books, code)
            for path in (f"tax-filings/{filing.pk}/", f"tax-filings/{filing.pk}/schedule/"):
                with self.subTest(path=path):
                    response = self.get(self.ngozi, path)
                    self.assertEqual(response.status_code, 404)
                    self.assert_nothing_of_ikeja(response)

    def test_the_audit_trail_names_none_of_ikejas_staff(self):
        rows = self.ok("audit-logs/?page_size=100")
        self.assertIn("Added Bola Lawal to the payroll.", [r["message"] for r in rows])


class WholeSchoolReaderTests(_ReachFixture):
    """Mr Bello reads every branch's staff, whole, as before."""

    def ok(self, path):
        response = self.get(self.bello, path)
        self.assertEqual(response.status_code, 200, getattr(response, "data", response))
        return response.data["data"] if hasattr(response, "data") else response

    def test_the_run_is_whole(self):
        data = self.ok(f"payroll-runs/{self.payroll_run.pk}/")
        self.assertFalse(data["partial_view"])
        self.assertEqual(len(data["lines"]), 5)
        self.assertEqual(data["gross_total"], (123_456 + 90_000 + 80_000 + 234_567 + 54_321) * N)

    def test_an_ikeja_persons_records_open(self):
        history = self.ok(f"employee-salaries/{self.ada.pk}/history/")
        self.assertEqual([v["gross_amount"] for v in history], [123_456 * N])
        deductions = self.ok(f"employee-salaries/{self.ada.pk}/deductions/")
        self.assertEqual([d["amount"] for d in deductions], [7_777 * N])
        summary = self.ok(f"employee-salaries/{self.ada.pk}/tax-summary/?year=2026")
        self.assertEqual((summary["tax_id"], summary["totals"]["gross"]), ("TIN-ADA", 123_456 * N))
        payslip = self.ok(
            f"payroll-runs/{self.payroll_run.pk}/lines/{self.line(self.ada).pk}/payslip/?output=json")
        self.assertEqual(payslip["pension_pin"], "PEN-ADA")
        pdf = self.ok(f"payroll-runs/{self.payroll_run.pk}/lines/{self.line(self.ada).pk}/payslip/")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_the_paye_schedule_is_every_branchs(self):
        filing = _prepare(self.books, "PAYE-LA")

        data = self.ok(f"tax-filings/{filing.pk}/schedule/")

        self.assertIn("Ada Obi", [r["employee_name"] for r in data["rows"]])
        self.assertIn("Bola Lawal", [r["employee_name"] for r in data["rows"]])
        self.assertEqual(data["total"], filing.gross_liability)


class OwnPayslipTests(_ReachFixture):
    """Staff read their own payslips through my-payslips, and nobody else's, whatever their role."""

    def mine(self, user, path=""):
        return TenantAPIClient(user=user).get(f"/v1/finance/my-payslips/{path}")

    def test_the_list_holds_the_callers_own_only(self):
        response = self.mine(self.bola_user)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([p["id"] for p in response.data["data"]],
                         [Payslip.objects.get(salary=self.bola).pk])
        self.assert_nothing_of_ikeja(response)

    def test_a_colleagues_payslip_is_not_found_by_id_or_as_a_pdf(self):
        ada_payslip = Payslip.objects.get(salary=self.ada).pk
        for path in (f"{ada_payslip}/", f"{ada_payslip}/?output=pdf"):
            with self.subTest(path=path):
                response = self.mine(self.bola_user, path)
                self.assertEqual(response.status_code, 404)
                self.assert_nothing_of_ikeja(response)

    def test_a_payroll_role_does_not_widen_my_payslips(self):
        ada_payslip = Payslip.objects.get(salary=self.ada).pk
        self.assertEqual(self.mine(self.bello_user).data["data"], [])
        for path in (f"{ada_payslip}/", f"{ada_payslip}/?output=pdf"):
            with self.subTest(path=path):
                self.assertEqual(self.mine(self.bello_user, path).status_code, 404)

    def test_the_tax_summary_is_the_callers_own(self):
        for output in ("", "&output=pdf"):
            with self.subTest(output=output):
                response = TenantAPIClient(user=self.bola_user).get(
                    f"/v1/finance/my-tax-summary/?year=2026{output}")
                self.assertEqual(response.status_code, 200)
                self.assert_nothing_of_ikeja(response)


class AnotherTenantTests(_ReachFixture):
    """Rival Group's bursar holds every payroll key and reaches none of Corona's records."""

    def setUp(self):
        super().setUp()
        self.rival = TenantAPIClient(user=self.grant(
            self.user_for(self.rival_tenant, "rival-bursar@rival.test"), *self.KEYS,
            "finance.audit.view", tenant=self.rival_tenant, role_key="reach-rival"))

    def test_nothing_of_corona_is_found(self):
        filing = _prepare(self.books, "PAYE-LA")
        line = self.line(self.bola)
        paths = (
            f"payroll-runs/{self.payroll_run.pk}/",
            f"payroll-runs/{self.payroll_run.pk}/lines/{line.pk}/payslip/",
            f"employee-salaries/{self.bola.pk}/history/",
            f"employee-salaries/{self.bola.pk}/deductions/",
            f"employee-salaries/{self.bola.pk}/tax-summary/?year=2026",
            f"tax-filings/{filing.pk}/schedule/",
        )
        for books in (self.books, self.rival_books):
            for path in paths:
                with self.subTest(books=books.code, path=path):
                    response = self.get(self.rival, path, books=books)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("Bola Lawal", str(getattr(response, "data", "")))
        payslip = Payslip.objects.get(salary=self.bola).pk
        self.assertEqual(self.rival.get(f"/v1/finance/my-payslips/{payslip}/").status_code, 404)
