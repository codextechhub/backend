"""Pay brought forward into a tax year: cumulative PAYE for months no run here paid.

Two kinds. **A previous employer's**: Single Site (one branch, in Lagos) takes
on three people from 1 April 2026, each
on N300,000 a month, with the default policy (8% pension and 2.5% NHF from the
employee, PAYE computed from the 2026 table):

* **Aisha Bello** earned N900,000 taxable at her previous employer from January
  to March, and had N45,000 of PAYE deducted there (its tax deduction card,
  reference TDC-2026-0147).
* **Bayo Ade** comes straight from university: no previous employer.
* **Kemi Obi** earned the same N900,000 at her previous employer, which
  over-deducted N200,000 of PAYE.

**This employer's own, from before its payroll ran here**: Bright Star School
moves its payroll onto these books in June 2026 (see BrightStarMovesHereTests).

Corona Group (Ikeja, Lekki and Yaba) is where the security and branch rules
are proved: Tunde Bello is paid by Ikeja to March and by Lekki from April.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from core.test_utils import TenantAPIClient
from vs_finance.constants import FinanceAuditAction
from vs_finance.exceptions import PayrollError
from vs_finance.models import (
    FinanceAuditLog,
    FinancePayrollSettings,
    Payslip,
    PayrollLine,
    PayBroughtForward,
    TaxObligation,
)
from vs_finance.money import format_naira
from vs_finance.payroll import cancel_payroll_run, generate_run_from_roster, pay_payroll, post_payroll
from vs_finance.payroll_statutory import change_terms, record_creation, save_pay_brought_forward
from vs_finance.payroll_tax import YearToDate, compute_paye

from .tests_payroll_roster_rules import _on, _PayRolesFixture
from .tests_payroll_statutory import N, _snapshot, _StatutoryFixture, _date

#: April to December, as the payroll months of the tax year.
MONTHS = range(4, 13)

#: Aisha's PAYE at this employer, month by month (see AishaByHandTests).
AISHA = {4: 95_330, **{m: 30_830 for m in range(5, 13)}}

#: Bayo's, with nothing brought forward: the figures from before this feature.
BAYO = {4: 275, **{m: 30_275 for m in range(5, 13)}}

#: Kemi's, whose previous employer over-deducted.
KEMI = {4: 0, 5: 0, 6: 1_990, **{m: 30_830 for m in range(7, 13)}}


def _previous(**figures):
    """A previous employer's year to date, in naira."""
    return YearToDate(**{k: v * N for k, v in figures.items()})


def _months(brought_forward, *, gross=300_000 * N):
    """``{month: PAYE}`` from April to December on ``gross`` a month, in naira."""
    prior, out = YearToDate(), {}
    for month in MONTHS:
        pension, nhf = gross * 8 // 100, gross * 25 // 1000
        result = compute_paye(
            _snapshot(), month=month, prior=prior, gross_now=gross, taxable_now=gross,
            pension_now=pension, nhf_now=nhf, annual_rent=0, brought_forward=brought_forward,
        )
        out[month] = result.amount // N
        prior = prior.plus(YearToDate(
            gross=gross, taxable_pay=gross, paye=result.amount, pension=pension, nhf=nhf,
        ))
    return out


# --------------------------------------------------------------------------- #
# The engine, without a database                                              #
# --------------------------------------------------------------------------- #

class AishaByHandTests(SimpleTestCase):
    """Aisha's year worked out by hand, April to December.

    The 2026 bands, scaled to ``m/12`` of a year in month ``m``: 0% to
    N800,000, 15% to N3,000,000, 18% to N12,000,000. In month ``m`` she has
    been paid here for ``m - 3`` months, so to date:

    * income = 900,000 brought forward + 300,000 x (m - 3)
    * reliefs = her pension and NHF here, 24,000 + 7,500 = 31,500 a month
      (nothing brought forward: her previous employer's card shows none)
    * chargeable = 900,000 + 268,500 x (m - 3) = 94,500 + 268,500 x m

    The chargeable sits in the 18% band all year (above 250,000 x m, below
    1,000,000 x m), so the tax due to date is

    * 15% x 2,200,000 x m/12 + 18% x (94,500 + 268,500 m - 250,000 m)
      = 27,500 m + 17,010 + 3,330 m = **30,830 m + 17,010**

    and each month deducts the tax due to date less everything already
    deducted, hers here and the N45,000 her previous employer took:

    ====== =========== ============ ==================== ============
    Month  Chargeable  Tax to date  Already deducted     PAYE
    ====== =========== ============ ==================== ============
    April  1,168,500   140,330      45,000               **95,330**
    May    1,437,000   171,160      45,000 + 95,330      **30,830**
    June   1,705,500   201,990      171,160              **30,830**
    Jul    1,974,000   232,820      201,990              **30,830**
    Aug    2,242,500   263,650      232,820              **30,830**
    Sep    2,511,000   294,480      263,650              **30,830**
    Oct    2,779,500   325,310      294,480              **30,830**
    Nov    3,048,000   356,140      325,310              **30,830**
    Dec    3,316,500   386,970      356,140              **30,830**
    ====== =========== ============ ==================== ============

    December is the whole year: 15% x 2,200,000 + 18% x 316,500 = 386,970,
    of which her previous employer deducted 45,000 and this one 341,970.
    Without her earlier pay, April would have taxed N268,500 against four
    months of bands (N275) and every later month N30,275: N242,475 by
    December, N99,495 short of what the year's tax requires of this employer.
    """

    def test_aisha_month_by_month(self):
        self.assertEqual(_months(_previous(gross=900_000, taxable_pay=900_000, paye=45_000)), AISHA)

    def test_her_two_employers_together_deduct_exactly_the_years_tax(self):
        paid_here = sum(AISHA.values())
        self.assertEqual(paid_here, 341_970)
        self.assertEqual(paid_here + 45_000, 386_970)

    def test_with_nothing_brought_forward_the_months_are_unchanged(self):
        self.assertEqual(_months(None), BAYO)
        self.assertEqual(_months(YearToDate()), BAYO)

    def test_an_over_deduction_is_carried_forward_and_never_refunded(self):
        """Kemi's April and May deduct nothing; the excess runs out in June.

        Tax to date is the same as Aisha's; already deducted is N200,000.
        April 140,330 - 200,000 is below nothing: deduct 0, 59,670 still in
        hand. May 171,160 - 200,000: deduct 0, 28,840 in hand. June
        201,990 - 200,000 = 1,990.
        """
        kemi = _previous(gross=900_000, taxable_pay=900_000, paye=200_000)
        self.assertEqual(_months(kemi), KEMI)
        april = compute_paye(
            _snapshot(), month=4, prior=YearToDate(), gross_now=300_000 * N,
            taxable_now=300_000 * N, pension_now=24_000 * N, nhf_now=7_500 * N,
            annual_rent=0, brought_forward=kemi,
        )
        self.assertEqual(april.working["excess_withheld"], 59_670 * N)
        self.assertEqual(april.working["inputs"]["paye_before"], 0)
        self.assertEqual(april.working["brought_forward"]["paye"], 200_000 * N)

    def test_previous_contributions_are_reliefs_and_rent_relief_is_given_once(self):
        """Her previous pension relieves her tax; rent relief covers the year once.

        N72,000 of pension at her previous employer takes N72,000 off April's
        chargeable income. Her N1,000,000 rent earns 20% = N200,000 of relief a
        year, four twelfths of it by April, whoever paid her January to March.
        """
        def april(brought_forward, rent=0):
            return compute_paye(
                _snapshot(), month=4, prior=YearToDate(), gross_now=300_000 * N,
                taxable_now=300_000 * N, pension_now=24_000 * N, nhf_now=7_500 * N,
                annual_rent=rent * N, brought_forward=brought_forward,
            ).working

        plain = april(_previous(gross=900_000, taxable_pay=900_000))
        pension = april(_previous(gross=900_000, taxable_pay=900_000, pension=72_000))
        self.assertEqual(plain["chargeable_to_date"] - pension["chargeable_to_date"], 72_000 * N)
        rent = april(_previous(gross=900_000, taxable_pay=900_000), rent=1_000_000)
        relief = next(r for r in rent["reliefs"] if r["code"] == "RENT")
        self.assertEqual(relief["amount"], 66_666_67)


# --------------------------------------------------------------------------- #
# Runs over the year                                                          #
# --------------------------------------------------------------------------- #

class _StartersFixture(_StatutoryFixture):
    """Aisha, Bayo and Kemi join Single Site from 1 April on N300,000."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.aisha_user = cls.user_for(cls.solo_tenant, "aisha@solo.test")
        cls.aisha = cls.starter("Aisha Bello", employee=cls.aisha_user)
        cls.aisha_previous = save_pay_brought_forward(cls.aisha, 2026, {
            "gross_amount": 900_000 * N, "taxable_pay": 900_000 * N, "paye_amount": 45_000 * N,
            "pension_amount": 0, "nhf_amount": 0, "employer_name": "Greenfield Academy",
            "evidence_reference": "TDC-2026-0147",
        })

    @classmethod
    def starter(cls, name, **extra):
        row = cls.person(cls.solo_books, name, cls.solo_main, 300_000, **extra)
        record_creation(row, effective_from=_date(4, 1))
        return row

    @classmethod
    def raise_run(cls, month, *, post=True):
        run = generate_run_from_roster(cls.solo_books, pay_date=_date(month, 25))
        if post:
            post_payroll(run)
        return run


class StartersOverTheYearTests(_StartersFixture):
    """April to December posted, April paid: each PAYE as worked out by hand."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bayo = cls.starter("Bayo Ade")
        cls.kemi = cls.starter("Kemi Obi")
        save_pay_brought_forward(cls.kemi, 2026, {
            "gross_amount": 900_000 * N, "taxable_pay": 900_000 * N, "paye_amount": 200_000 * N,
            "pension_amount": 0, "nhf_amount": 0, "employer_name": "Lagoon Tutors",
            "evidence_reference": "",
        })
        cls.runs = {month: cls.raise_run(month) for month in MONTHS}
        pay_payroll(cls.runs[4], bank_account=cls.bank(cls.solo_books, cls.solo_main, "1192"))

    def paye(self, salary):
        return {
            m: PayrollLine.objects.get(run=self.runs[m], salary=salary).paye_amount // N
            for m in MONTHS
        }

    def test_aisha_is_taxed_on_her_whole_year(self):
        self.assertEqual(self.paye(self.aisha), AISHA)

    def test_bayo_with_no_previous_employer_is_taxed_as_before(self):
        self.assertEqual(self.paye(self.bayo), BAYO)

    def test_kemis_over_deduction_is_used_up_before_anything_is_deducted(self):
        self.assertEqual(self.paye(self.kemi), KEMI)

    def test_the_working_keeps_the_two_employers_apart(self):
        may = PayrollLine.objects.get(run=self.runs[5], salary=self.aisha).tax_basis
        self.assertEqual(may["inputs"]["paye_before"], 95_330 * N)
        self.assertEqual(may["brought_forward"]["paye"], 45_000 * N)
        self.assertEqual(may["brought_forward"]["employer_name"], "Greenfield Academy")
        self.assertEqual(may["tax_to_date"], 171_160 * N)

    def test_the_payslip_year_to_date_is_this_employers_and_the_previous_is_apart(self):
        from vs_finance.payslips import payslip_context

        context = payslip_context(PayrollLine.objects.get(run=self.runs[5], salary=self.aisha))
        self.assertEqual(context["ytd"]["gross"], format_naira(600_000 * N))
        self.assertEqual(context["ytd"]["paye"], format_naira((95_330 + 30_830) * N))
        self.assertEqual(context["brought_forward"]["paye"], format_naira(45_000 * N))
        self.assertEqual(context["brought_forward"]["gross"], format_naira(900_000 * N))
        self.assertEqual(context["brought_forward"]["employer_name"], "Greenfield Academy")
        bayo = payslip_context(PayrollLine.objects.get(run=self.runs[5], salary=self.bayo))
        self.assertIsNone(bayo["brought_forward"])

    def test_her_own_payslip_shows_what_was_brought_forward(self):
        payslip = Payslip.objects.get(salary=self.aisha)
        client = TenantAPIClient(user=self.aisha_user)
        response = client.get(f"/v1/finance/my-payslips/{payslip.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["brought_forward"]["paye"], format_naira(45_000 * N))
        pdf = client.get(f"/v1/finance/my-payslips/{payslip.pk}/?output=pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_the_tax_summary_totals_only_this_employers_months(self):
        from vs_finance.payslips import tax_summary, tax_summary_pdf_bytes

        summary = tax_summary(self.solo_books, year=2026, salary=self.aisha)
        self.assertEqual(summary["totals"]["paye"], 341_970 * N)
        self.assertEqual(summary["totals"]["gross"], 9 * 300_000 * N)
        self.assertEqual(summary["brought_forward"]["paye"], 45_000 * N)
        self.assertTrue(tax_summary_pdf_bytes(summary).startswith(b"%PDF"))

    def test_the_april_paye_return_declares_only_what_this_employer_deducted(self):
        from vs_finance.payroll_statutory import remittance_schedule
        from vs_finance.tax_filing import prepare_filing

        lagos = TaxObligation.objects.get(entity=self.solo_books, code="PAYE-LA")
        filing = prepare_filing(lagos, period_start=_date(4, 1), period_end=_date(4, 30))
        self.assertEqual(filing.gross_liability, (95_330 + 275) * N)
        rows = {r["employee_name"]: r["total"] for r in remittance_schedule(filing)}
        self.assertEqual(rows, {"Aisha Bello": 95_330 * N, "Bayo Ade": 275 * N})

    def test_bayo_is_flagged_and_the_others_are_not(self):
        april = self.runs[4]
        self.assertEqual(getattr(april, "previous_pay_missing", None), ["Bayo Ade"])
        client = TenantAPIClient(user=self.solo_user)
        with _on(_date(5, 2)):
            response = client.get(
                f"/v1/finance/employee-salaries/previous-pay-missing/?year=2026"
                f"&entity={self.solo_books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["required"])
        self.assertEqual(
            [(p["name"], p["first_month"]) for p in response.data["data"]["people"]],
            [("Bayo Ade", 4)],
        )

    def test_a_record_of_zeros_says_there_was_no_previous_employer(self):
        save_pay_brought_forward(self.bayo, 2026, {
            "gross_amount": 0, "taxable_pay": 0, "paye_amount": 0, "pension_amount": 0,
            "nhf_amount": 0, "employer_name": "", "evidence_reference": "",
        })
        from vs_finance.payroll_statutory import starters_without_previous_pay

        self.assertEqual(starters_without_previous_pay(
            self.solo_books, [self.aisha, self.bayo, self.kemi], tax_year=2026), [])


class MidYearCorrectionTests(_StartersFixture):
    """Aisha's card is re-issued after April is posted: N60,000 of PAYE, not N45,000.

    April stays as posted (N95,330). May works the year out again: tax to date
    171,160, less 60,000 and April's 95,330, is N15,830; June onward is
    N30,830 again. Her two employers together deduct exactly the year's
    N386,970 by December.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.april = cls.raise_run(4)

    def setUp(self):
        super().setUp()
        self.client_ = TenantAPIClient(user=self.solo_user)

    def correct(self, **body):
        return self.client_.patch(
            f"/v1/finance/employee-pay-brought-forward/{self.aisha_previous.pk}/"
            f"?entity={self.solo_books.code}", body, format="json")

    def test_a_correction_after_april_is_posted_reaches_may_and_not_april(self):
        april_line = PayrollLine.objects.get(run=self.april, salary=self.aisha)
        response = self.correct(brought_forward_paye_amount=60_000 * N, evidence_reference="TDC-2026-0147A")
        self.assertEqual(response.status_code, 200, response.data)

        april_line.refresh_from_db()
        self.assertEqual(april_line.paye_amount, 95_330 * N)
        self.assertEqual(april_line.tax_basis["brought_forward"]["paye"], 45_000 * N)
        may = self.raise_run(5).lines.get(salary=self.aisha)
        self.assertEqual(may.paye_amount, 15_830 * N)
        june = self.raise_run(6).lines.get(salary=self.aisha)
        self.assertEqual(june.paye_amount, 30_830 * N)

    def test_the_correction_is_audited_without_a_figure_in_the_message(self):
        self.correct(brought_forward_paye_amount=60_000 * N)
        entry = FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.SALARY_CHANGED, target_id=str(self.aisha.pk),
        ).latest("id")
        self.assertEqual(entry.before, {"brought_forward_paye_amount": 45_000 * N})
        self.assertEqual(entry.after, {"brought_forward_paye_amount": 60_000 * N})
        self.assertNotIn("45", entry.message)
        self.assertNotIn("60", entry.message)

    def test_a_draft_holding_her_must_be_voided_first(self):
        may = self.raise_run(5, post=False)
        response = self.correct(brought_forward_paye_amount=60_000 * N)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(may.document_number or str(may.pk), str(response.data))
        self.aisha_previous.refresh_from_db()
        self.assertEqual(self.aisha_previous.paye_amount, 45_000 * N)

        cancel_payroll_run(may)
        self.assertEqual(self.correct(brought_forward_paye_amount=60_000 * N).status_code, 200)
        self.assertEqual(self.raise_run(5).lines.get(salary=self.aisha).paye_amount, 15_830 * N)

    def test_figures_that_cannot_be_one_employers_pay_are_refused(self):
        self.assertEqual(self.correct(brought_forward_taxable_pay=950_000 * N).status_code, 400)
        self.assertEqual(self.correct(employer_name="").status_code, 400)
        self.assertEqual(self.correct(brought_forward_paye_amount=-1).status_code, 400)

    def test_a_second_record_for_the_same_year_is_refused(self):
        response = self.client_.post(
            f"/v1/finance/employee-salaries/{self.aisha.pk}/pay-brought-forward/"
            f"?entity={self.solo_books.code}",
            {"tax_year": 2026, "brought_forward_gross_amount": 1, "brought_forward_taxable_pay": 1,
             "employer_name": "Greenfield Academy"}, format="json")
        self.assertEqual(response.status_code, 400, response.data)


class RequiredAtHireTests(_StartersFixture):
    """Single Site requires earlier pay at hire: Bayo is not paid until his is recorded."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bayo = cls.starter("Bayo Ade")
        FinancePayrollSettings.objects.create(entity=cls.solo_books, previous_pay_required=True)

    def test_the_run_is_refused_until_his_earlier_pay_is_recorded(self):
        with self.assertRaisesMessage(PayrollError, "1 person(s)"):
            self.raise_run(4)

        client = TenantAPIClient(user=self.solo_user)
        response = client.post(
            f"/v1/finance/employee-salaries/{self.bayo.pk}/pay-brought-forward/"
            f"?entity={self.solo_books.code}", {"tax_year": 2026}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        run = self.raise_run(4)
        self.assertEqual(run.lines.get(salary=self.bayo).paye_amount, 275 * N)
        self.assertEqual(run.lines.get(salary=self.aisha).paye_amount, 95_330 * N)

    def test_the_setting_is_a_payroll_setting_with_a_default(self):
        self.assertFalse(FinancePayrollSettings().previous_pay_required)
        response = TenantAPIClient(user=self.solo_user).get(
            f"/v1/finance/settings/payroll/?entity={self.solo_books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["settings"]["previous_pay_required"])
        self.assertIn("previous_pay_required", response.data["data"]["consumers"])


# --------------------------------------------------------------------------- #
# Who may read and write it                                                   #
# --------------------------------------------------------------------------- #

class PreviousPayReachTests(_StatutoryFixture):
    """Tunde's earlier pay follows his salary record from Ikeja to Lekki on 1 April."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tunde = cls.person(cls.books, "Tunde Bello", cls.ikeja, 300_000)
        record_creation(cls.tunde)
        change_terms(cls.tunde, {"branch_id": cls.lekki.pk}, effective_from=_date(4, 1))
        cls.record = save_pay_brought_forward(cls.tunde, 2026, {
            "gross_amount": 500_000 * N, "taxable_pay": 500_000 * N, "paye_amount": 20_000 * N,
            "pension_amount": 0, "nhf_amount": 0, "employer_name": "Greenfield Academy",
            "evidence_reference": "",
        })
        cls.ikeja_user = cls.grant(cls.user_for(cls.tenant, "prev-ikeja@corona.test"),
                                   *cls.KEYS, tenant=cls.tenant, role_key="prev-ikeja",
                                   branch=cls.ikeja)
        cls.lekki_user = cls.grant(cls.user_for(cls.tenant, "prev-lekki@corona.test"),
                                   *cls.KEYS, tenant=cls.tenant, role_key="prev-lekki",
                                   branch=cls.lekki)
        cls.viewer = cls.grant(cls.user_for(cls.tenant, "prev-viewer@corona.test"),
                               "finance.salary.view", tenant=cls.tenant, role_key="prev-viewer")
        cls.rival = cls.grant(cls.user_for(cls.rival_tenant, "prev@rival.test"), *cls.KEYS,
                              tenant=cls.rival_tenant, role_key="prev-rival")

    def call(self, user, method, suffix, body=None, entity=None):
        entity = entity or self.books
        return getattr(TenantAPIClient(user=user), method)(
            f"/v1/finance/{suffix}?entity={entity.code}", body, format="json")

    def list_path(self):
        return f"employee-salaries/{self.tunde.pk}/pay-brought-forward/"

    def detail_path(self):
        return f"employee-pay-brought-forward/{self.record.pk}/"

    def test_in_march_ikeja_reaches_it_and_lekki_does_not(self):
        with _on(_date(3, 15)):
            ikeja = self.call(self.ikeja_user, "get", self.list_path())
            lekki = self.call(self.lekki_user, "get", self.list_path())
            lekki_detail = self.call(self.lekki_user, "patch", self.detail_path(),
                                     {"evidence_reference": "X"})
        self.assertEqual(ikeja.status_code, 200, ikeja.data)
        self.assertEqual([r["brought_forward_paye_amount"] for r in ikeja.data["data"]], [20_000 * N])
        self.assertEqual(lekki.status_code, 404)
        self.assertEqual(lekki_detail.status_code, 404)

    def test_from_april_lekki_reaches_it_and_ikeja_does_not(self):
        with _on(_date(4, 2)):
            lekki = self.call(self.lekki_user, "get", self.detail_path())
            ikeja = self.call(self.ikeja_user, "get", self.detail_path())
            ikeja_new = self.call(self.ikeja_user, "post", self.list_path(), {"tax_year": 2027})
        self.assertEqual(lekki.status_code, 200, lekki.data)
        self.assertEqual(ikeja.status_code, 404)
        self.assertEqual(ikeja_new.status_code, 404)

    def test_another_tenant_finds_nothing(self):
        for method, path in (("get", self.list_path()), ("get", self.detail_path()),
                             ("delete", self.detail_path())):
            with self.subTest(method=method, path=path):
                response = self.call(self.rival, method, path, entity=self.rival_books)
                self.assertEqual(response.status_code, 404)
        self.assertTrue(PayBroughtForward.objects.filter(pk=self.record.pk).exists())

    def test_a_reader_without_the_write_keys_is_refused(self):
        self.assertEqual(self.call(self.viewer, "get", self.list_path()).status_code, 200)
        for method, path, body in (
            ("post", self.list_path(), {"tax_year": 2027}),
            ("patch", self.detail_path(), {"evidence_reference": "X"}),
            ("delete", self.detail_path(), None),
        ):
            with self.subTest(method=method):
                self.assertEqual(self.call(self.viewer, method, path, body).status_code, 403)
        self.assertTrue(PayBroughtForward.objects.filter(pk=self.record.pk).exists())

    def test_removing_it_is_audited_under_the_branch_owning_him(self):
        with _on(_date(4, 2)):
            response = self.call(self.hq_user, "delete", self.detail_path())
        self.assertEqual(response.status_code, 200, response.data)
        entry = FinanceAuditLog.objects.filter(target_id=str(self.tunde.pk)).latest("id")
        self.assertEqual(entry.before["brought_forward_paye_amount"], 20_000 * N)
        self.assertEqual(entry.branch_id, self.lekki.pk)


class PreviousPayFieldAccessTests(_PayRolesFixture):
    """Mrs Okafor reads pay and changes none; Mr Adeyemi changes it; Mr Eze cannot see PAYE."""

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import set_field_access

        super().setUpTestData()
        cls.tunde = cls.person(cls.books, "Tunde Bello", cls.ikeja, 300_000)
        record_creation(cls.tunde)
        cls.record = save_pay_brought_forward(cls.tunde, 2026, {
            "gross_amount": 500_000 * N, "taxable_pay": 500_000 * N, "paye_amount": 20_000 * N,
            "pension_amount": 40_000 * N, "nhf_amount": 0, "employer_name": "Greenfield Academy",
            "evidence_reference": "",
        })
        cls.eze_user = cls.grant(cls.user_for(cls.tenant, "prev-eze@corona.test"), *cls.KEYS,
                                 tenant=cls.tenant, role_key="prev-no-paye")
        no_paye = TenantRoleTemplate.objects.get(tenant=cls.tenant, key="prev-no-paye")
        set_field_access(no_paye, *cls.pay_keys, read=True, write=False)
        set_field_access(no_paye, "finance.salary.paye_amount", read=False, write=False)

    def detail(self):
        return f"employee-pay-brought-forward/{self.record.pk}/"

    def test_every_figure_needs_its_write_switch_and_nothing_is_written(self):
        writes = [
            ("post", f"employee-salaries/{self.tunde.pk}/pay-brought-forward/",
             {"tax_year": 2027, "brought_forward_gross_amount": 1_000 * N,
              "employer_name": "Greenfield Academy"}, "brought_forward_gross_amount"),
            ("patch", self.detail(), {"brought_forward_paye_amount": 25_000 * N}, "brought_forward_paye_amount"),
            ("patch", self.detail(), {"brought_forward_taxable_pay": 400_000 * N}, "brought_forward_taxable_pay"),
            ("patch", self.detail(), {"brought_forward_pension_amount": 1}, "brought_forward_pension_amount"),
            ("patch", self.detail(), {"brought_forward_nhf_amount": 1}, "brought_forward_nhf_amount"),
        ]
        for method, path, body, field in writes:
            with self.subTest(field=field):
                response = self.call(self.okafor, method, path, body)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertIn(field, str(response.data))
        self.record.refresh_from_db()
        self.assertEqual((self.record.paye_amount, self.record.taxable_pay), (20_000 * N, 500_000 * N))
        self.assertEqual(PayBroughtForward.objects.count(), 1)
        for method, path, body, field in writes:
            with self.subTest(field=field, writer="adeyemi"):
                response = self.call(self.adeyemi, method, path, body)
                self.assertIn(response.status_code, (200, 201), response.data)

    def test_the_form_sent_back_unchanged_beside_new_words_is_not_a_figure_write(self):
        response = self.call(self.okafor, "patch", self.detail(), {
            "brought_forward_gross_amount": 500_000 * N, "brought_forward_paye_amount": str(20_000 * N),
            "evidence_reference": "TDC-2026-0201",
        })
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_role_without_paye_reads_every_figure_but_the_tax(self):
        response = self.call(TenantAPIClient(user=self.eze_user), "get", self.detail(), None)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertNotIn("brought_forward_paye_amount", data)
        self.assertEqual(data["brought_forward_gross_amount"], 500_000 * N)
        self.assertEqual(data["brought_forward_pension_amount"], 40_000 * N)

    def test_an_auditor_without_the_pay_switches_reads_that_it_was_recorded_and_no_figure(self):
        auditor = self.grant(self.user_for(self.tenant, "prev-audit@corona.test"),
                             "finance.audit.view", tenant=self.tenant, role_key="prev-audit")
        response = self.call(TenantAPIClient(user=auditor), "get", "audit-logs/", None)
        self.assertEqual(response.status_code, 200, response.data)
        (row,) = [r for r in response.data["data"]
                  if r["target_type"] == "EmployeeSalary" and str(r["target_id"]) == str(self.tunde.pk)
                  and "previous employer" in r["message"]]
        self.assertEqual(row["after"].get("employer_name"), "Greenfield Academy")
        for leaked in ("50000000", "2000000", "4000000"):
            self.assertNotIn(leaked, str(row))


# --------------------------------------------------------------------------- #
# A tenant that moves its payroll here mid-year                               #
# --------------------------------------------------------------------------- #

#: Ngozi's PAYE on Bright Star's runs here, June to December (see the class docstring).
NGOZI = {m: 16_850 for m in range(6, 13)}


class BrightStarMovesHereTests(_StatutoryFixture):
    """Bright Star School moves its payroll onto these books from June 2026.

    Its settings say so (``payroll_moved_here_on`` 1 June 2026). Three people:

    * **Ngozi Eze**, a teacher since 2019 on N200,000 a month. January to May
      ran on Bright Star's old payroll, whose figures are recorded as her
      opening figures: gross 5 x 200,000 = N1,000,000, all taxable; pension 8%
      = N80,000; NHF 2.5% = N25,000; PAYE N84,250.
    * **Musa Bello**, a cleaner since 2021 on N100,000, with no opening
      figures recorded: optional, and he is not taken for a joiner, because
      he is first paid here in June, the month the payroll moved.
    * **Kola Ade**, hired in July from another school, with nothing recorded:
      a mid-year joiner, flagged.

    Ngozi by hand. Each month she earns 200,000 less 16,000 pension and 5,000
    NHF: 179,000 chargeable. In June (m = 6) the year to date is six months:

    * chargeable to date = 179,000 x 6 = 1,074,000
    * bands at 6/12: 0% to 400,000, 15% to 1,500,000
    * tax to date = 15% x (1,074,000 - 400,000) = 101,100
    * already deducted = 84,250 (her opening PAYE): June deducts **16,850**

    and every month to December the same, 15% x (179,000 - 66,666.67) =
    16,850, so the year's PAYE is 84,250 + 7 x 16,850 = 202,200, the tax on
    her N2,400,000: 15% x (2,148,000 - 800,000). Without her opening figures
    June would tax one month's 179,000 against six months of bands, and
    deduct nothing.

    Her opening months are Bright Star's own pay: they are in her year to date
    and in its annual return (gross N2,400,000, PAYE N202,200), and in no
    monthly remittance schedule (June declares only the N16,850 deducted here).
    """

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.tests.helpers import make_branch, make_school

        from vs_finance.constants import PayBroughtForwardSource

        super().setUpTestData()
        school = make_school(slug="bright-star", name="Bright Star School", status="ACTIVE")
        cls.bright_tenant = school.tenant
        cls.bright_main = make_branch(school, name="Main Branch")
        cls.bright_main.state = "Lagos"
        cls.bright_main.save(update_fields=["state"])
        cls.bright = cls.build_books("BRIGHT", cls.bright_tenant)
        FinancePayrollSettings.objects.create(
            entity=cls.bright, payroll_moved_here_on=_date(6, 1),
        )
        cls.bursar = cls.grant(cls.user_for(cls.bright_tenant, "bursar@brightstar.test"),
                               *cls.KEYS, tenant=cls.bright_tenant, role_key="bright-bursar")
        cls.ngozi_user = cls.user_for(cls.bright_tenant, "ngozi@brightstar.test")
        cls.ngozi = cls.person(cls.bright, "Ngozi Eze", cls.bright_main, 200_000,
                               employee=cls.ngozi_user)
        cls.musa = cls.person(cls.bright, "Musa Bello", cls.bright_main, 100_000)
        cls.kola = cls.person(cls.bright, "Kola Ade", cls.bright_main, 150_000)
        for row in (cls.ngozi, cls.musa):
            record_creation(row)
        record_creation(cls.kola, effective_from=_date(7, 1))
        cls.opening = save_pay_brought_forward(cls.ngozi, 2026, {
            "gross_amount": 1_000_000 * N, "taxable_pay": 1_000_000 * N,
            "paye_amount": 84_250 * N, "pension_amount": 80_000 * N, "nhf_amount": 25_000 * N,
            "employer_name": "", "evidence_reference": "Old payroll, May 2026 year to date",
        }, source=PayBroughtForwardSource.THIS_EMPLOYER)
        cls.runs = {}
        for month in range(6, 13):
            cls.runs[month] = generate_run_from_roster(cls.bright, pay_date=_date(month, 25))
            post_payroll(cls.runs[month])

    def line(self, month, salary):
        return PayrollLine.objects.get(run=self.runs[month], salary=salary)

    def test_ngozi_is_taxed_on_her_whole_year(self):
        self.assertEqual({m: self.line(m, self.ngozi).paye_amount // N for m in NGOZI}, NGOZI)
        june = self.line(6, self.ngozi).tax_basis
        self.assertEqual(june["tax_to_date"], 101_100 * N)
        self.assertEqual(june["opening"]["paye"], 84_250 * N)
        self.assertEqual(june["inputs"]["paye_before"], 0)
        self.assertNotIn("brought_forward", june)

    def test_without_her_opening_figures_june_would_deduct_nothing(self):
        june = compute_paye(
            _snapshot(), month=6, prior=YearToDate(), gross_now=200_000 * N,
            taxable_now=200_000 * N, pension_now=16_000 * N, nhf_now=5_000 * N, annual_rent=0,
        )
        self.assertEqual(june.amount, 0)

    def test_only_kola_is_flagged_as_a_mid_year_joiner(self):
        self.assertEqual(self.runs[6].previous_pay_missing, [])
        self.assertEqual(self.runs[7].previous_pay_missing, ["Kola Ade"])
        with _on(_date(8, 2)):
            response = TenantAPIClient(user=self.bursar).get(
                f"/v1/finance/employee-salaries/previous-pay-missing/?year=2026"
                f"&entity={self.bright.code}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([p["name"] for p in response.data["data"]["people"]], ["Kola Ade"])

    def test_her_june_payslip_counts_the_opening_months_in_the_year_to_date(self):
        from vs_finance.payslips import payslip_context

        context = payslip_context(self.line(6, self.ngozi))
        self.assertEqual(context["ytd"]["gross"], format_naira(1_200_000 * N))
        self.assertEqual(context["ytd"]["paye"], format_naira(101_100 * N))
        self.assertEqual(context["ytd"]["net"], format_naira(self.line(6, self.ngozi).net_amount))
        self.assertEqual(context["opening"]["paye"], format_naira(84_250 * N))
        self.assertIsNone(context["brought_forward"])

    def test_the_june_remittance_declares_only_what_was_deducted_here(self):
        from vs_finance.payroll_statutory import remittance_schedule
        from vs_finance.tax_filing import prepare_filing

        lagos = TaxObligation.objects.get(entity=self.bright, code="PAYE-LA")
        filing = prepare_filing(lagos, period_start=_date(6, 1), period_end=_date(6, 30))
        self.assertEqual(filing.gross_liability, 16_850 * N)
        rows = remittance_schedule(filing)
        self.assertEqual([(r["employee_name"], r["total"]) for r in rows],
                         [("Ngozi Eze", 16_850 * N)])

    def test_the_annual_return_and_her_tax_summary_include_the_opening_months(self):
        from vs_finance.payslips import tax_summary

        response = TenantAPIClient(user=self.bursar).get(
            f"/v1/finance/payroll/annual-return/?year=2026&entity={self.bright.code}")
        self.assertEqual(response.status_code, 200, response.data)
        rows = {r["employee_name"]: r for r in response.data["data"]["rows"]}
        ngozi = rows["Ngozi Eze"]
        self.assertEqual(
            (ngozi["gross"], ngozi["paye"], ngozi["opening_gross"], ngozi["opening_paye"],
             ngozi["months"]),
            (2_400_000 * N, 202_200 * N, 1_000_000 * N, 84_250 * N, 7),
        )
        self.assertEqual(rows["Kola Ade"]["opening_gross"], 0)
        summary = tax_summary(self.bright, year=2026, salary=self.ngozi)
        self.assertEqual(summary["totals"]["paye"], 202_200 * N)
        self.assertEqual(summary["totals"]["gross"], 2_400_000 * N)
        self.assertEqual(len(summary["months"]), 7)
        self.assertEqual(summary["opening"]["paye"], 84_250 * N)
        from vs_finance.payslips import payslip_pdf_bytes, tax_summary_pdf_bytes

        self.assertTrue(tax_summary_pdf_bytes(summary).startswith(b"%PDF"))
        self.assertTrue(payslip_pdf_bytes(self.line(6, self.ngozi)).startswith(b"%PDF"))

    def test_the_annual_return_is_refused_without_the_tax_key_and_to_another_tenant(self):
        clerk = self.grant(self.user_for(self.bright_tenant, "clerk@brightstar.test"),
                           "finance.salary.view", tenant=self.bright_tenant, role_key="bright-clerk")
        path = f"/v1/finance/payroll/annual-return/?year=2026&entity={self.bright.code}"
        self.assertEqual(TenantAPIClient(user=clerk).get(path).status_code, 403)
        rival = self.grant(self.user_for(self.rival_tenant, "annual@rival.test"),
                           "finance.tax.view", tenant=self.rival_tenant, role_key="annual-rival")
        response = TenantAPIClient(user=rival).get(
            f"/v1/finance/payroll/annual-return/?year=2026&entity={self.rival_books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["rows"], [])

    def test_opening_figures_name_no_other_employer(self):
        response = TenantAPIClient(user=self.bursar).post(
            f"/v1/finance/employee-salaries/{self.musa.pk}/pay-brought-forward/"
            f"?entity={self.bright.code}",
            {"tax_year": 2026, "source": "THIS_EMPLOYER", "brought_forward_gross_amount": 1,
             "brought_forward_taxable_pay": 1, "employer_name": "Somewhere"}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("employer_name", str(response.data))

    def test_a_person_may_hold_one_of_each_kind_a_year(self):
        response = TenantAPIClient(user=self.bursar).post(
            f"/v1/finance/employee-salaries/{self.ngozi.pk}/pay-brought-forward/"
            f"?entity={self.bright.code}",
            {"tax_year": 2026, "source": "PREVIOUS_EMPLOYER"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        again = TenantAPIClient(user=self.bursar).post(
            f"/v1/finance/employee-salaries/{self.ngozi.pk}/pay-brought-forward/"
            f"?entity={self.bright.code}",
            {"tax_year": 2026, "source": "THIS_EMPLOYER"}, format="json")
        self.assertEqual(again.status_code, 400, again.data)

    def test_the_move_is_a_whole_tenant_payroll_setting(self):
        client = TenantAPIClient(user=self.bursar)
        path = f"/v1/finance/settings/payroll/?entity={self.bright.code}"
        response = client.patch(path, {"payroll_moved_here_on": "2026-07-01"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["settings"]["payroll_moved_here_on"], "2026-07-01")
        self.assertEqual(client.patch(path, {"payroll_moved_here_on": "June"},
                                      format="json").status_code, 400)
        cleared = client.patch(path, {"payroll_moved_here_on": None}, format="json")
        self.assertIsNone(cleared.data["data"]["settings"]["payroll_moved_here_on"])
