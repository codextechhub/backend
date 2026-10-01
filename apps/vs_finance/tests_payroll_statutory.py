"""Statutory payroll: PAYE from the national table, deductions and contributions, payslips.

The people in these tests:

* **Corona Group** has Ikeja and Lekki in Lagos and Yaba in Ogun State. Ada works
  and lives in Lagos; Bola works at Yaba; Chidi works at Ikeja but lives in Ogun
  State, so his PAYE goes to Ogun. Ada and Bola are with Stanbic IBTC for their
  pension; Chidi has not named a PFA yet.
* **Single Site** has one branch, in Lagos. Its teacher Miss Eze earns N100,000 a
  month until a raise to N300,000 from July.

PAYE is worked out cumulatively from the 2026 table seeded as data: N100,000 a
month with 8% pension and 2.5% NHF is N3,425 of PAYE in January.
"""
from __future__ import annotations

import datetime
from fractions import Fraction
from unittest import mock

from django.test import SimpleTestCase

from core.test_utils import TenantAPIClient
from vs_finance.constants import (
    FinanceAuditAction,
    PayeSource,
    PayslipEmailStatus,
)
from vs_finance.exceptions import PayrollError
from vs_finance.models import (
    Account,
    BankAccount,
    EmployeeDeduction,
    EmployeeSalary,
    FinanceAuditLog,
    FinancePayrollSettings,
    FiscalPeriod,
    FiscalYear,
    JournalLine,
    LedgerEntity,
    PayeTaxTable,
    PayrollDeductionType,
    PayrollLine,
    PayrollTaxJurisdiction,
    PensionFundAdministrator,
    Payslip,
    SalaryComponent,
    SalaryStructure,
    TaxObligation,
)
from vs_finance.payroll import generate_run_from_roster, pay_payroll, post_payroll
from vs_finance.payroll_tax import YearToDate, band_tax, compute_paye
from vs_finance.seed import seed_chart_of_accounts

from .tests_branch_scope import _FinanceBranchFixture

N = 100  # kobo per naira


def _date(month, day):
    return datetime.date(2026, month, day)


def _snapshot(**extra):
    """The 2026 table as a payroll line records it, built without a database."""
    bands = [
        (0, 800_000, 0), (800_000, 3_000_000, 1500), (3_000_000, 12_000_000, 1800),
        (12_000_000, 25_000_000, 2100), (25_000_000, 50_000_000, 2300), (50_000_000, None, 2500),
    ]
    return {
        "id": 1, "country": "NG", "tax_year": 2026, "revision": 1, "name": "test",
        "minimum_tax_rate_bps": 0, "exempt_income_threshold": 0,
        "bands": [
            {"lower": lo * N, "upper": None if hi is None else hi * N, "rate_bps": rate}
            for lo, hi, rate in bands
        ],
        "reliefs": [
            {"code": "PENSION", "name": "Pension", "kind": "CONTRIBUTION", "basis": "PENSION",
             "rate_bps": 0, "cap_amount": None, "floor_amount": 0},
            {"code": "NHF", "name": "NHF", "kind": "CONTRIBUTION", "basis": "NHF",
             "rate_bps": 0, "cap_amount": None, "floor_amount": 0},
            {"code": "RENT", "name": "Rent", "kind": "PERCENT_CAPPED", "basis": "ANNUAL_RENT",
             "rate_bps": 2000, "cap_amount": 500_000 * N, "floor_amount": 0},
        ],
        **extra,
    }


# --------------------------------------------------------------------------- #
# The engine, without a database                                              #
# --------------------------------------------------------------------------- #

class PayeEngineTests(SimpleTestCase):
    """The cumulative calculation against the 2026 bands."""

    def month(self, month, gross, prior, *, rent=0, snapshot=None):
        pension, nhf = gross * 8 // 100, gross * 25 // 1000
        result = compute_paye(
            snapshot or _snapshot(), month=month, prior=prior, gross_now=gross,
            taxable_now=gross, pension_now=pension, nhf_now=nhf, annual_rent=rent,
        )
        return result, pension, nhf

    def test_a_full_year_band_calculation(self):
        tax = band_tax(1_104_000 * N, _snapshot()["bands"], Fraction(1))
        self.assertEqual(tax, 45_600 * N)

    def test_miss_eze_pays_n3425_in_january(self):
        result, _, _ = self.month(1, 100_000 * N, YearToDate())
        self.assertEqual(result.amount, 3_425 * N)
        self.assertEqual(result.working["chargeable_to_date"], 89_500 * N)
        self.assertEqual(result.working["table"]["tax_year"], 2026)

    def test_a_mid_year_raise_totals_exactly_the_years_tax(self):
        """N100,000 to June, N300,000 from July: the year's PAYE is the tax on the year."""
        prior, paid = YearToDate(), []
        for month in range(1, 13):
            gross = (100_000 if month <= 6 else 300_000) * N
            result, pension, nhf = self.month(month, gross, prior)
            paid.append(result.amount)
            prior = YearToDate(
                gross=prior.gross + gross, taxable_pay=prior.taxable_pay + gross,
                paye=prior.paye + result.amount, pension=prior.pension + pension,
                nhf=prior.nhf + nhf,
            )
        annual_gross = 2_400_000 * N
        chargeable = annual_gross - annual_gross * 8 // 100 - annual_gross * 25 // 1000
        self.assertEqual(sum(paid), (chargeable - 800_000 * N) * 15 // 100)
        self.assertEqual(paid[:6], [3_425 * N] * 6)
        self.assertGreater(paid[6], paid[5])

    def test_rent_relief_is_capped(self):
        with_rent, _, _ = self.month(12, 300_000 * N, YearToDate(
            gross=3_300_000 * N, taxable_pay=3_300_000 * N, pension=264_000 * N,
            nhf=82_500 * N, paye=0,
        ), rent=3_000_000 * N)
        rent = next(r for r in with_rent.working["reliefs"] if r["code"] == "RENT")
        self.assertEqual(rent["amount"], 500_000 * N)

    def test_a_pay_cut_withholds_nothing_rather_than_refunding(self):
        prior = YearToDate(gross=600_000 * N, taxable_pay=600_000 * N, paye=200_000 * N)
        result, _, _ = self.month(2, 10_000 * N, prior)
        self.assertEqual(result.amount, 0)

    def test_an_exempt_threshold_in_the_table_charges_nothing(self):
        snapshot = _snapshot(exempt_income_threshold=1_500_000 * N)
        result, _, _ = self.month(1, 100_000 * N, YearToDate(), snapshot=snapshot)
        self.assertEqual(result.amount, 0)
        self.assertTrue(result.working["exempt"])


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #

class _StatutoryFixture(_FinanceBranchFixture):
    """The branch fixture with a full 2026 calendar, branch states and PFAs."""

    KEYS = (
        "finance.payrollrun.create", "finance.payrollrun.view", "finance.payrollrun.post",
        "finance.payrollrun.pay", "finance.salary.view", "finance.salary.create",
        "finance.salary.update", "finance.salary.delete", "finance.tax.view",
        "finance.settings.view", "finance.settings.update",
    )

    @classmethod
    def build_books(cls, code, tenant):
        entity = LedgerEntity.objects.create(
            name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
        )
        seed_chart_of_accounts(entity)
        year = FiscalYear.objects.create(
            entity=entity, year=2026, start_date=_date(1, 1), end_date=_date(12, 31),
        )
        for month in range(1, 13):
            start = _date(month, 1)
            end = (_date(month + 1, 1) if month < 12 else datetime.date(2027, 1, 1)) \
                - datetime.timedelta(days=1)
            FiscalPeriod.objects.create(
                entity=entity, fiscal_year=year, period_no=month,
                name=start.strftime("%b 2026"), start_date=start, end_date=end,
            )
        return entity

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for branch, state in ((cls.ikeja, "Lagos"), (cls.lekki, "Lagos State"),
                              (cls.yaba, "Ogun State"), (cls.solo_main, "Lagos")):
            branch.state = state
            branch.save(update_fields=["state"])
        cls.lagos = PayrollTaxJurisdiction.objects.get(country="NG", code="LA")
        cls.ogun = PayrollTaxJurisdiction.objects.get(country="NG", code="OG")
        cls.stanbic = PensionFundAdministrator.objects.get(code="STANBIC")
        cls.hq_user = cls.grant(cls.user_for(cls.tenant, "stat-hq@corona.test"), *cls.KEYS,
                                tenant=cls.tenant, role_key="stat-hq")
        cls.solo_user = cls.grant(cls.user_for(cls.solo_tenant, "stat@solo.test"), *cls.KEYS,
                                  tenant=cls.solo_tenant, role_key="stat-solo")

    @classmethod
    def person(cls, entity, name, branch, gross_naira, **extra):
        return EmployeeSalary.objects.create(
            entity=entity, name=name, branch=branch, gross_amount=gross_naira * N, **extra,
        )

    @classmethod
    def bank(cls, entity, branch, code):
        gl = Account.objects.create(
            entity=entity, code=code, name=f"Bank {code}", account_type="ASSET", is_postable=True,
        )
        return BankAccount.objects.create(entity=entity, name=f"Bank {code}", branch=branch, gl_account=gl)

    def credits_to(self, entity, code):
        return sum(JournalLine.objects.filter(
            entry__entity=entity, account__code=code,
        ).values_list("credit", flat=True))

    def debits_to(self, entity, code):
        return sum(JournalLine.objects.filter(
            entry__entity=entity, account__code=code,
        ).values_list("debit", flat=True))


# --------------------------------------------------------------------------- #
# A run worked out and posted                                                 #
# --------------------------------------------------------------------------- #

class TwoStateRunTests(_StatutoryFixture):
    """Corona's January run: PAYE by state, pension by PFA, contributions per branch."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ada = cls.person(cls.books, "Ada Obi", cls.ikeja, 100_000, pfa=cls.stanbic,
                             tax_id="TIN-ADA", pension_pin="PEN-ADA")
        cls.bola = cls.person(cls.books, "Bola Lawal", cls.yaba, 300_000, pfa=cls.stanbic)
        cls.chidi = cls.person(cls.books, "Chidi Eze", cls.ikeja, 100_000, residence_state=cls.ogun)
        cls.payroll_run = generate_run_from_roster(cls.books, pay_date=_date(1, 25))
        post_payroll(cls.payroll_run)

    def line(self, salary):
        return PayrollLine.objects.get(run=self.payroll_run, salary=salary)

    def test_each_line_is_worked_out_from_the_table_and_the_policy(self):
        ada = self.line(self.ada)
        self.assertEqual(
            (ada.paye_amount, ada.pension_amount, ada.other_deductions_amount,
             ada.employer_contributions_amount),
            (3_425 * N, 8_000 * N, 2_500 * N, 12_000 * N),
        )
        self.assertEqual(ada.net_amount, (100_000 - 3_425 - 8_000 - 2_500) * N)
        self.assertEqual(ada.paye_source, PayeSource.COMPUTED)
        self.assertEqual(ada.tax_table.tax_year, 2026)
        self.assertEqual(ada.tax_basis["month"], 1)
        self.assertEqual((ada.tax_id, ada.pension_pin), ("TIN-ADA", "PEN-ADA"))
        self.assertEqual(self.line(self.bola).paye_amount, 30_830 * N)

    def test_paye_follows_residence_and_falls_back_to_the_branch_state(self):
        self.assertEqual(self.line(self.ada).tax_state, self.lagos)
        self.assertEqual(self.line(self.bola).tax_state, self.ogun)
        self.assertEqual(self.line(self.chidi).tax_state, self.ogun)

    def test_each_state_gets_its_own_payable_and_obligation(self):
        self.assertEqual(self.credits_to(self.books, "2310-LA"), 3_425 * N)
        self.assertEqual(self.credits_to(self.books, "2310-OG"), (30_830 + 3_425) * N)
        ogun = TaxObligation.objects.get(entity=self.books, code="PAYE-OG")
        self.assertEqual(ogun.authority_name, "Ogun State Internal Revenue Service")
        self.assertEqual(ogun.liability_account.code, "2310-OG")

    def test_pension_goes_to_the_pfa_and_employer_cost_is_expensed_per_branch(self):
        self.assertEqual(self.credits_to(self.books, "2320-STANBIC"),
                         (8_000 + 10_000 + 24_000 + 30_000) * N)
        self.assertEqual(self.credits_to(self.books, "2320"), (8_000 + 10_000) * N)
        self.assertTrue(TaxObligation.objects.filter(entity=self.books, code="PENSION-STANBIC").exists())
        self.assertEqual(self.debits_to(self.books, "5210"), (10_000 + 30_000 + 10_000) * N)
        self.assertEqual(self.credits_to(self.books, "2340"), (2_500 + 7_500 + 2_500) * N)
        shares = {s.branch_id: s for s in self.payroll_run.branch_shares.all()}
        self.assertEqual(set(shares), {self.ikeja.pk, self.yaba.pk})
        self.assertEqual(shares[self.yaba.pk].employer_contributions_total, 36_000 * N)
        for share in shares.values():
            lines = share.journal.lines.all()
            self.assertEqual(sum(l.debit for l in lines), sum(l.credit for l in lines))

    def test_the_ogun_return_names_each_person_behind_it(self):
        from vs_finance.payroll_statutory import remittance_schedule
        from vs_finance.tax_filing import prepare_filing

        ogun = TaxObligation.objects.get(entity=self.books, code="PAYE-OG")
        filing = prepare_filing(ogun, period_start=_date(1, 1), period_end=_date(1, 31))
        self.assertEqual(filing.gross_liability, (30_830 + 3_425) * N)
        by_branch = {s.branch_id: s.gross_liability for s in filing.shares.all()}
        self.assertEqual(by_branch, {self.ikeja.pk: 3_425 * N, self.yaba.pk: 30_830 * N})
        rows = remittance_schedule(filing)
        self.assertEqual(sorted((r["employee_name"], r["total"]) for r in rows),
                         [("Bola Lawal", 30_830 * N), ("Chidi Eze", 3_425 * N)])

    def test_the_pfa_return_totals_employee_and_employer_pension(self):
        from vs_finance.payroll_statutory import remittance_schedule
        from vs_finance.tax_filing import prepare_filing

        stanbic = TaxObligation.objects.get(entity=self.books, code="PENSION-STANBIC")
        filing = prepare_filing(stanbic, period_start=_date(1, 1), period_end=_date(1, 31))
        self.assertEqual(filing.amount_due, 72_000 * N)
        rows = {r["employee_name"]: r for r in remittance_schedule(filing)}
        self.assertEqual((rows["Bola Lawal"]["employee_amount"], rows["Bola Lawal"]["employer_amount"]),
                         (24_000 * N, 30_000 * N))

    def test_a_branch_bound_reader_sees_only_their_branchs_part_of_the_schedule(self):
        from vs_finance.tax_filing import prepare_filing

        ogun = TaxObligation.objects.get(entity=self.books, code="PAYE-OG")
        filing = prepare_filing(ogun, period_start=_date(1, 1), period_end=_date(1, 31))
        yaba_reader = TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, "stat-yaba@corona.test"), "finance.tax.view",
            tenant=self.tenant, role_key="stat-yaba", branch=self.yaba))
        response = yaba_reader.get(
            f"/v1/finance/tax-filings/{filing.pk}/schedule/?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([r["employee_name"] for r in response.data["data"]["rows"]], ["Bola Lawal"])


class SingleBranchRunTests(_StatutoryFixture):
    """One branch: one journal, the dimension recedes, PAYE still goes to its state."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.eze = cls.person(cls.solo_books, "Grace Eze", cls.solo_main, 100_000)
        cls.payroll_run = generate_run_from_roster(cls.solo_books, pay_date=_date(1, 25))
        post_payroll(cls.payroll_run)

    def test_one_journal_and_paye_to_lagos(self):
        self.payroll_run.refresh_from_db()
        self.assertIsNotNone(self.payroll_run.journal_id)
        self.assertFalse(self.payroll_run.branch_shares.exists())
        self.assertEqual(self.credits_to(self.solo_books, "2310-LA"), 3_425 * N)
        self.assertEqual(self.credits_to(self.solo_books, "2350"), 1_000 * N)
        self.assertEqual(self.debits_to(self.solo_books, "5230"), 1_000 * N)


class MidYearRaiseTests(_StatutoryFixture):
    """Miss Eze's raise from July: every month on its own terms, the year's tax exact."""

    @classmethod
    def setUpTestData(cls):
        from vs_finance.payroll_statutory import change_terms

        super().setUpTestData()
        cls.eze = cls.person(cls.solo_books, "Grace Eze", cls.solo_main, 100_000)
        cls.runs = {}
        for month in range(1, 7):
            cls.runs[month] = generate_run_from_roster(cls.solo_books, pay_date=_date(month, 25))
        change_terms(cls.eze, {"gross_amount": 300_000 * N}, effective_from=_date(7, 1))
        for month in range(7, 13):
            cls.runs[month] = generate_run_from_roster(cls.solo_books, pay_date=_date(month, 25))

    def test_the_years_paye_is_the_tax_on_the_years_income(self):
        paid = [self.runs[m].lines.get().paye_amount for m in range(1, 13)]
        annual = 2_400_000 * N
        chargeable = annual - annual * 8 // 100 - annual * 25 // 1000
        self.assertEqual(sum(paid), (chargeable - 800_000 * N) * 15 // 100)
        self.assertEqual(paid[0], 3_425 * N)

    def test_july_reads_the_months_before_it(self):
        july = self.runs[7].lines.get()
        self.assertEqual(july.gross_amount, 300_000 * N)
        self.assertEqual(july.tax_basis["inputs"]["gross_before"], 600_000 * N)
        self.assertEqual(july.tax_basis["month"], 7)

    def test_a_past_month_reads_the_terms_it_was_paid_on(self):
        self.eze.refresh_from_db()
        self.assertEqual(self.eze.gross_amount, 300_000 * N)
        self.assertEqual(self.eze.terms_on(_date(3, 31)).gross_amount, 100_000 * N)
        self.assertEqual(self.eze.versions.count(), 2)

    def test_a_change_cannot_be_dated_into_a_paid_month(self):
        from rest_framework.exceptions import ValidationError

        from vs_finance.payroll_statutory import change_terms

        with self.assertRaises(ValidationError):
            change_terms(self.eze, {"gross_amount": 1 * N}, effective_from=_date(12, 1))

    def test_a_year_with_no_table_refuses_to_compute(self):
        with self.assertRaisesMessage(PayrollError, "2027"):
            from vs_finance.payroll_tax import table_for

            table_for("NG", datetime.date(2027, 1, 31))


# --------------------------------------------------------------------------- #
# Paid once, never twice and never not at all                                 #
# --------------------------------------------------------------------------- #

class CentralPersonGuardTests(_StatutoryFixture):
    """A central school's second run of a month pays only the people not yet on one."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ada = cls.person(cls.books, "Ada Obi", cls.ikeja, 100_000)
        cls.bola = cls.person(cls.books, "Bola Lawal", cls.lekki, 100_000)

    def test_a_second_generated_run_pays_only_the_late_hire(self):
        generate_run_from_roster(self.books, pay_date=_date(1, 25))
        with self.assertRaisesMessage(PayrollError, "already on a payroll run"):
            generate_run_from_roster(self.books, pay_date=_date(1, 31))
        self.person(self.books, "Dele Ade", self.yaba, 100_000)
        late = generate_run_from_roster(self.books, pay_date=_date(1, 31))
        self.assertEqual([l.employee_name for l in late.lines.all()], ["Dele Ade"])
        self.assertEqual(sorted(name for name, _ in late.skipped), ["Ada Obi", "Bola Lawal"])

    def test_a_cancelled_run_frees_its_people(self):
        from vs_finance.payroll import cancel_payroll_run

        first = generate_run_from_roster(self.books, pay_date=_date(1, 25))
        cancel_payroll_run(first)
        again = generate_run_from_roster(self.books, pay_date=_date(1, 26))
        self.assertEqual(again.lines.count(), 2)

    def test_the_scope_waits_for_a_month_no_run_has_started(self):
        from vs_config.exceptions import InvalidConfigurationValue

        from .tests_payroll_branch import _PayrollFixture

        generate_run_from_roster(self.books, pay_date=_date(1, 25))
        with mock.patch("vs_config.clock.tenant_today", return_value=_date(1, 28)):
            with self.assertRaises(InvalidConfigurationValue):
                _PayrollFixture.set_scope(self.tenant, "PER_BRANCH")
        with mock.patch("vs_config.clock.tenant_today", return_value=_date(2, 1)):
            _PayrollFixture.set_scope(self.tenant, "PER_BRANCH")


class PerBranchMoveTests(_StatutoryFixture):
    """Moving branch mid-month under per-branch payroll: one branch pays the whole month."""

    @classmethod
    def setUpTestData(cls):
        from .tests_payroll_branch import _PayrollFixture

        super().setUpTestData()
        cls.bello = cls.person(cls.books, "Musa Bello", cls.ikeja, 100_000)
        cls.okafor = cls.person(cls.books, "Ngozi Okafor", cls.lekki, 100_000)
        cls.person(cls.books, "Yemi Yaba", cls.yaba, 100_000)
        with mock.patch("vs_config.clock.tenant_today", return_value=_date(1, 2)):
            _PayrollFixture.set_scope(cls.tenant, "PER_BRANCH")

    def setUp(self):
        super().setUp()
        self.hq = TenantAPIClient(user=self.hq_user)
        self.clock = mock.patch("vs_config.clock.tenant_today", return_value=_date(1, 21))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def move(self, salary, branch, **body):
        return self.hq.patch(
            f"/v1/finance/employee-salaries/{salary.pk}/?entity={self.books.code}",
            {"branch": branch.pk, **body}, format="json",
        )

    def names(self, run):
        return sorted(l.employee_name for l in run.lines.all())

    def test_paid_by_ikeja_then_moved_lekki_does_not_pay_him_again(self):
        generate_run_from_roster(self.books, pay_date=_date(1, 20), branch=self.ikeja)
        refused = self.move(self.bello, self.lekki, effective_from="2026-01-22")
        self.assertEqual(refused.status_code, 400, refused.data)
        moved = self.move(self.bello, self.lekki)
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertEqual(self.bello.versions.order_by("-id").first().effective_from, _date(2, 1))
        lekki = generate_run_from_roster(self.books, pay_date=_date(1, 25), branch=self.lekki)
        self.assertEqual(self.names(lekki), ["Ngozi Okafor"])
        february = generate_run_from_roster(self.books, pay_date=_date(2, 25), branch=self.lekki)
        self.assertEqual(self.names(february), ["Musa Bello", "Ngozi Okafor"])

    def test_a_move_that_would_leave_her_unpaid_is_refused(self):
        generate_run_from_roster(self.books, pay_date=_date(1, 20), branch=self.ikeja)
        refused = self.move(self.okafor, self.ikeja, effective_from="2026-01-22")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("without Ngozi Okafor", str(refused.data))
        moved = self.move(self.okafor, self.ikeja, effective_from="2026-02-01")
        self.assertEqual(moved.status_code, 200, moved.data)
        lekki = generate_run_from_roster(self.books, pay_date=_date(1, 25), branch=self.lekki)
        self.assertEqual(self.names(lekki), ["Ngozi Okafor"])

    def test_the_branch_on_the_payroll_date_pays_the_whole_month(self):
        moved = self.move(self.okafor, self.ikeja, effective_from="2026-01-22")
        self.assertEqual(moved.status_code, 200, moved.data)
        ikeja = generate_run_from_roster(self.books, pay_date=_date(1, 20), branch=self.ikeja)
        self.assertEqual(self.names(ikeja), ["Musa Bello", "Ngozi Okafor"])
        with self.assertRaisesMessage(PayrollError, "No active employees on the Lekki"):
            generate_run_from_roster(self.books, pay_date=_date(1, 25), branch=self.lekki)

    def test_an_active_person_needs_a_branch(self):
        from rest_framework.exceptions import ValidationError

        from vs_finance.payroll_statutory import record_creation

        orphan = EmployeeSalary.objects.create(entity=self.books, name="No Branch", gross_amount=1)
        with self.assertRaises(ValidationError):
            record_creation(orphan)


# --------------------------------------------------------------------------- #
# Overrides, supplied figures, voluntary deductions                           #
# --------------------------------------------------------------------------- #

class PayeOverrideTests(_StatutoryFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.eze = cls.person(cls.solo_books, "Grace Eze", cls.solo_main, 100_000)

    def setUp(self):
        super().setUp()
        self.client_ = TenantAPIClient(user=self.solo_user)

    def patch(self, **body):
        return self.client_.patch(
            f"/v1/finance/employee-salaries/{self.eze.pk}/?entity={self.solo_books.code}",
            body, format="json",
        )

    def test_an_override_needs_a_reason_and_is_audited(self):
        self.assertEqual(self.patch(paye_override=5_000 * N).status_code, 400)
        response = self.patch(paye_override=5_000 * N, paye_override_reason="Bureau figure")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(FinanceAuditLog.objects.filter(
            entity=self.solo_books, action=FinanceAuditAction.PAYE_OVERRIDE_CHANGED).exists())
        line = generate_run_from_roster(self.solo_books, pay_date=_date(1, 25)).lines.get()
        self.assertEqual((line.paye_amount, line.paye_source), (5_000 * N, PayeSource.OVERRIDE))
        self.assertEqual(line.tax_basis["override"]["computed"], 3_425 * N)

    def test_a_raise_through_the_api_is_a_new_audited_version(self):
        response = self.patch(gross_amount=150_000 * N, reason="Promotion")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.eze.versions.count(), 2)
        audit = FinanceAuditLog.objects.filter(
            entity=self.solo_books, action=FinanceAuditAction.SALARY_CHANGED).latest("id")
        self.assertEqual(audit.after["gross_amount"], 150_000 * N)
        history = self.client_.get(
            f"/v1/finance/employee-salaries/{self.eze.pk}/history/?entity={self.solo_books.code}")
        self.assertEqual([v["gross_amount"] for v in history.data["data"]],
                         [100_000 * N, 150_000 * N])

    def test_removing_a_person_keeps_their_row(self):
        response = self.client_.delete(
            f"/v1/finance/employee-salaries/{self.eze.pk}/?entity={self.solo_books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        self.eze.refresh_from_db()
        self.assertFalse(self.eze.is_active)


class SuppliedAndVoluntaryTests(_StatutoryFixture):
    """A tenant supplying its own PAYE, and a cooperative deduction that runs out."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        FinancePayrollSettings.objects.create(
            entity=cls.solo_books, paye_method="SUPPLIED", employer_pension_enabled=False,
            nhf_enabled=False, nsitf_enabled=False, itf_enabled=False,
        )
        cls.eze = cls.person(cls.solo_books, "Grace Eze", cls.solo_main, 100_000,
                             paye_amount=4_000 * N, pension_amount=8_000 * N)
        coop = Account.objects.create(entity=cls.solo_books, code="2450", name="Cooperative savings",
                                      account_type="LIABILITY", is_postable=True)
        cls.coop = PayrollDeductionType.objects.create(
            entity=cls.solo_books, code="COOP", name="Cooperative savings", liability_account=coop)
        EmployeeDeduction.objects.create(
            salary=cls.eze, deduction_type=cls.coop, amount=10_000 * N, total_limit=25_000 * N)

    def test_supplied_figures_and_a_capped_deduction(self):
        amounts = []
        for month in (1, 2, 3, 4):
            run = generate_run_from_roster(self.solo_books, pay_date=_date(month, 25))
            line = run.lines.get()
            self.assertEqual((line.paye_amount, line.paye_source), (4_000 * N, PayeSource.SUPPLIED))
            amounts.append(line.other_deductions_amount)
            if month == 1:
                post_payroll(run)
        self.assertEqual(amounts, [10_000 * N, 10_000 * N, 5_000 * N, 0])
        self.assertEqual(self.credits_to(self.solo_books, "2450"), 10_000 * N)


# --------------------------------------------------------------------------- #
# Payslips                                                                    #
# --------------------------------------------------------------------------- #

class PayslipTests(_StatutoryFixture):
    """Paid lines get payslips; the employee sees their own and nobody else's."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.eze_user = cls.user_for(cls.solo_tenant, "grace@solo.test")
        cls.other_user = cls.user_for(cls.solo_tenant, "tunde@solo.test")
        cls.eze = cls.person(cls.solo_books, "Grace Eze", cls.solo_main, 100_000, employee=cls.eze_user)
        cls.tunde = cls.person(cls.solo_books, "Tunde Bello", cls.solo_main, 80_000, employee=cls.other_user)
        cls.bank_account = cls.bank(cls.solo_books, cls.solo_main, "1191")
        cls.payroll_run = generate_run_from_roster(cls.solo_books, pay_date=_date(1, 25))
        post_payroll(cls.payroll_run)
        pay_payroll(cls.payroll_run, bank_account=cls.bank_account)
        cls.payslip = Payslip.objects.get(salary=cls.eze)

    def deliver(self):
        from django.core.files.storage import default_storage

        from vs_finance.payslips import deliver_payslips

        with mock.patch("vs_notifications.notify.send_notification", return_value=["n1"]) as send, \
                mock.patch.object(default_storage, "save", return_value="stored.pdf"):
            deliver_payslips([self.payslip.pk])
        return send

    def test_paying_issues_a_payslip_per_line(self):
        self.assertEqual(Payslip.objects.filter(run=self.payroll_run).count(), 2)
        self.assertEqual(self.payslip.email_status, PayslipEmailStatus.PENDING)

    def test_delivery_sends_in_app_and_email_by_default(self):
        send = self.deliver()
        events = [call.kwargs["event_key"] for call in send.call_args_list]
        self.assertEqual(events, ["payroll.payslip_ready", "payroll.payslip_emailed"])
        self.payslip.refresh_from_db()
        self.assertEqual(self.payslip.email_status, PayslipEmailStatus.QUEUED)

    def test_the_tenant_can_turn_email_off(self):
        FinancePayrollSettings.objects.create(entity=self.solo_books, payslip_email=False)
        send = self.deliver()
        self.assertEqual([c.kwargs["event_key"] for c in send.call_args_list], ["payroll.payslip_ready"])

    def test_an_employee_sees_only_their_own(self):
        mine = TenantAPIClient(user=self.eze_user).get("/v1/finance/my-payslips/")
        self.assertEqual(mine.status_code, 200, mine.data)
        self.assertEqual([p["id"] for p in mine.data["data"]], [self.payslip.pk])
        theirs = TenantAPIClient(user=self.other_user).get(f"/v1/finance/my-payslips/{self.payslip.pk}/")
        self.assertEqual(theirs.status_code, 404)
        pdf = TenantAPIClient(user=self.eze_user).get(f"/v1/finance/my-payslips/{self.payslip.pk}/?output=pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_turning_in_app_off_hides_them(self):
        FinancePayrollSettings.objects.create(entity=self.solo_books, payslip_in_app=False)
        mine = TenantAPIClient(user=self.eze_user).get("/v1/finance/my-payslips/")
        self.assertEqual(mine.data["data"], [])

    def test_the_yearly_summary_adds_up_the_months(self):
        response = TenantAPIClient(user=self.eze_user).get("/v1/finance/my-tax-summary/?year=2026")
        self.assertEqual(response.status_code, 200, response.data)
        summary = response.data["data"][0]
        self.assertEqual(summary["totals"]["paye"], 3_425 * N)
        self.assertEqual(summary["tax_states"], ["Lagos"])

    def test_a_staff_payslip_needs_every_pay_figure(self):
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import install_declared_fields, set_field_access

        keys = install_declared_fields("finance.payrollrun")
        path = (f"/v1/finance/payroll-runs/{self.payroll_run.pk}/lines/{self.payslip.line_id}/payslip/"
                f"?entity={self.solo_books.code}")
        closed = self.grant(self.user_for(self.solo_tenant, "closed@solo.test"),
                            "finance.payrollrun.view", tenant=self.solo_tenant, role_key="closed")
        self.assertEqual(TenantAPIClient(user=closed).get(path).status_code, 403)
        opened = self.grant(self.user_for(self.solo_tenant, "open@solo.test"),
                            "finance.payrollrun.view", tenant=self.solo_tenant, role_key="opened")
        set_field_access(TenantRoleTemplate.objects.get(tenant=self.solo_tenant, key="opened"),
                         *keys, read=True, write=False)
        response = TenantAPIClient(user=opened).get(path)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_another_tenants_payslip_is_not_found(self):
        rival = self.grant(self.user_for(self.rival_tenant, "rival@rival.test"),
                           "finance.payrollrun.view", tenant=self.rival_tenant, role_key="rival")
        response = TenantAPIClient(user=rival).get(
            f"/v1/finance/payroll-runs/{self.payroll_run.pk}/lines/{self.payslip.line_id}/payslip/"
            f"?entity={self.rival_books.code}")
        self.assertEqual(response.status_code, 404)


# --------------------------------------------------------------------------- #
# Settings and national data                                                  #
# --------------------------------------------------------------------------- #

class PayrollSettingsTests(_StatutoryFixture):

    def setUp(self):
        super().setUp()
        self.hq = TenantAPIClient(user=self.hq_user)
        self.path = f"/v1/finance/settings/payroll/?entity={self.books.code}"

    def test_defaults_and_an_audited_change(self):
        settings = self.hq.get(self.path).data["data"]["settings"]
        self.assertEqual(
            (settings["paye_method"], settings["employee_pension_rate_bps"],
             settings["employer_pension_rate_bps"], settings["nhf_rate_bps"],
             settings["nsitf_rate_bps"], settings["itf_rate_bps"],
             settings["payslip_in_app"], settings["payslip_email"]),
            ("COMPUTED", 800, 1000, 250, 100, 100, True, True),
        )
        response = self.hq.patch(self.path, {"itf_enabled": False}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(FinancePayrollSettings.objects.get(entity=self.books).itf_enabled)
        self.assertEqual(len(response.data["data"]["history"]), 1)

    def test_a_bad_rate_is_refused(self):
        response = self.hq.patch(self.path, {"nhf_rate_bps": 20000}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_a_branch_bound_bursar_cannot_change_them(self):
        pinned = TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, "stat-pinned@corona.test"), *self.KEYS,
            tenant=self.tenant, role_key="stat-pinned", branch=self.lekki))
        response = pinned.patch(self.path, {"itf_enabled": False}, format="json")
        self.assertEqual(response.status_code, 403)


class NationalDataTests(_StatutoryFixture):
    """The tax tables are read by tenants and changed only by platform staff."""

    def test_a_tenant_reads_but_cannot_write(self):
        hq = TenantAPIClient(user=self.hq_user)
        listed = hq.get("/v1/finance/payroll/tax-tables/")
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertIn(2026, [t["tax_year"] for t in listed.data["data"]])
        table = PayeTaxTable.objects.get(country="NG", tax_year=2026)
        self.assertEqual(table.bands.count(), 6)
        response = hq.patch(f"/v1/finance/payroll/tax-tables/{table.pk}/", {"notes": "x"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_platform_staff_edit_a_table_and_the_revision_moves(self):
        from vs_rbac.tests.helpers import platform_tenant

        codex = platform_tenant()
        staff = self.grant(self.user_for(codex, "tax@codex.test"), "finance.statutory.update",
                           "finance.statutory.create", tenant=codex, role_key="tax-data")
        table = PayeTaxTable.objects.get(country="NG", tax_year=2026)
        response = TenantAPIClient(user=staff).patch(
            f"/v1/finance/payroll/tax-tables/{table.pk}/", {"notes": "Confirmed."}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        table.refresh_from_db()
        self.assertEqual((table.notes, table.revision), ("Confirmed.", 2))
        broken = TenantAPIClient(user=staff).patch(
            f"/v1/finance/payroll/tax-tables/{table.pk}/",
            {"bands": [{"lower": 0, "upper": 100, "rate_bps": 0}, {"lower": 50, "rate_bps": 100}]},
            format="json")
        self.assertEqual(broken.status_code, 400)


class StructureHistoryTests(_StatutoryFixture):
    """A structure edit closes its lines; a past month still reads the old ones."""

    @classmethod
    def setUpTestData(cls):
        from vs_finance.payroll_statutory import replace_components

        super().setUpTestData()
        cls.structure = SalaryStructure.objects.create(entity=cls.solo_books, name="Teaching")
        replace_components(cls.structure, [
            SalaryComponent(name="Basic", kind="EARNING", calc_method="PERCENT_OF_GROSS",
                            rate_bps=5000, is_basic=True, is_pensionable=True),
        ], creating=True)
        cls.eze = cls.person(cls.solo_books, "Grace Eze", cls.solo_main, 100_000,
                             structure=cls.structure)
        cls.january = generate_run_from_roster(cls.solo_books, pay_date=_date(1, 25))

    def test_an_edit_takes_effect_after_the_paid_month(self):
        client = TenantAPIClient(user=self.solo_user)
        response = client.patch(
            f"/v1/finance/salary-structures/{self.structure.pk}/?entity={self.solo_books.code}",
            {"components": [{"name": "Basic", "kind": "EARNING", "calc_method": "PERCENT_OF_GROSS",
                             "rate_bps": 6000, "is_basic": True, "is_pensionable": True}]},
            format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([c["rate_bps"] for c in response.data["data"]["components"]], [6000])
        history = client.get(
            f"/v1/finance/salary-structures/{self.structure.pk}/history/?entity={self.solo_books.code}")
        self.assertEqual([(c["rate_bps"], c["effective_to"]) for c in history.data["data"]],
                         [(5000, "2026-01-31"), (6000, None)])
        self.assertEqual(self.january.lines.get().pension_amount, 4_000 * N)
        february = generate_run_from_roster(self.solo_books, pay_date=_date(2, 25)).lines.get()
        self.assertEqual(february.pension_amount, 4_800 * N)
        self.assertTrue(FinanceAuditLog.objects.filter(
            entity=self.solo_books, action=FinanceAuditAction.SALARY_STRUCTURE_CHANGED).exists())
