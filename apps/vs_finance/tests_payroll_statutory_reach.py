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
