"""The payslip and the yearly tax summary are HTML templates, rendered on the server.

Grace Eze is paid 100,000 for January 2026 by Single Site, which owns one branch;
Ada Obi is paid by Ikeja Branch of Corona Group, which owns three. Whether a
payslip names the branch follows that, and a server without WeasyPrint's
libraries still sends both documents, in the plain layout, with an error logged.
"""
from __future__ import annotations

import types
from unittest import mock

from django.test import SimpleTestCase

from vs_finance.models import PayrollLine
from vs_finance.payroll import generate_run_from_roster, post_payroll
from vs_finance.payroll_test_support import approve_for_posting
from vs_finance.payslips import gross_shares, payslip_context, tax_summary

from . import tests_payroll_statutory as base
from .tests_document_pdf import has_weasyprint, pdf_bytes

N = base.N


def item(label, amount):
    return types.SimpleNamespace(label=label, amount=amount, get_code_display=lambda: label)


def rendered_html(render, *args):
    """The HTML ``render`` hands to WeasyPrint, captured by a stand-in for the library."""
    html = mock.MagicMock()
    html.return_value.write_pdf.return_value = b"%PDF-captured"
    stand_in = types.SimpleNamespace(
        HTML=html, CSS=mock.MagicMock(), FontConfiguration=mock.MagicMock(),
        URLFetcher=type("URLFetcher", (), {"__init__": lambda self, **kwargs: None}),
    )
    with mock.patch("vs_finance.pdf._weasyprint", return_value=stand_in):
        assert render(*args) == b"%PDF-captured"
    return html.call_args.kwargs["string"]


class _PayslipFixture(base._StatutoryFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.grace = cls.person(cls.solo_books, "Grace Eze", cls.solo_main, 100_000,
                               tax_id="TIN-GRACE", pension_pin="PEN-GRACE")
        cls.ada = cls.person(cls.books, "Ada Obi", cls.ikeja, 100_000, pfa=cls.stanbic)
        cls.script = cls.person(cls.books, "<script>alert(1)</script>", cls.ikeja, 100_000)
        cls.solo_run = generate_run_from_roster(cls.solo_books, pay_date=base._date(1, 25))
        approve_for_posting(cls.solo_run)
        post_payroll(cls.solo_run)
        cls.multi_run = generate_run_from_roster(cls.books, pay_date=base._date(1, 25))
        approve_for_posting(cls.multi_run)
        post_payroll(cls.multi_run)

    def line(self, run, salary):
        return PayrollLine.objects.select_related(
            "run__entity__tenant", "branch", "tax_state", "pfa", "tax_table",
        ).get(run=run, salary=salary)


class PayslipPdfIsTheTemplateTests(_PayslipFixture):

    def html(self, line):
        from vs_finance.pdf import payslip_pdf

        return rendered_html(payslip_pdf, payslip_context(line))

    def test_the_pdf_is_the_payslip_design(self):
        line = self.line(self.solo_run, self.grace)
        html = self.html(line)

        self.assertIn("Grace Eze", html)
        self.assertIn(f"{self.solo_run.document_number or self.solo_run.pk}/{line.line_no}", html)
        self.assertIn("Share of gross pay", html)
        self.assertIn("Paid by your employer on top of your pay", html)
        self.assertIn("Single Site", html)
        # Money is bare: the currency is named in the headings.
        self.assertNotIn("₦", html)
        self.assertIn(f"{line.net_amount / N:,.2f}", html)

    def test_a_one_branch_tenant_prints_no_branch(self):
        html = self.html(self.line(self.solo_run, self.grace))

        self.assertNotIn("Main Branch", html)
        self.assertNotIn(">Branch<", html)

    def test_a_several_branch_tenant_prints_the_branch(self):
        html = self.html(self.line(self.multi_run, self.ada))

        self.assertIn("Ikeja Branch", html)
        self.assertIn(">Branch<", html)

    def test_show_branch_follows_the_tenants_branch_count(self):
        self.assertFalse(payslip_context(self.line(self.solo_run, self.grace))["show_branch"])
        self.assertTrue(payslip_context(self.line(self.multi_run, self.ada))["show_branch"])

    def test_opening_and_brought_forward_appear_only_when_recorded(self):
        line = self.line(self.solo_run, self.grace)
        plain = self.html(line)
        self.assertNotIn("brought forward from before this payroll", plain)
        self.assertNotIn("Earlier this tax year", plain)

        line.tax_basis = {
            **(line.tax_basis or {}),
            "opening": {"gross": 50_000 * N, "paye": 2_000 * N, "taxable_pay": 0, "pension": 0},
            "brought_forward": {
                "employer_name": "Greenfield Academy", "gross": 90_000 * N,
                "taxable_pay": 80_000 * N, "paye": 4_500 * N, "pension": 7_200 * N,
            },
        }
        full = self.html(line)

        self.assertIn("Of which gross brought forward from before this payroll", full)
        self.assertIn("Net pay through this payroll", full)
        self.assertIn("Earlier this tax year with Greenfield Academy", full)
        self.assertIn("90,000.00", full)

    def test_the_employer_section_is_omitted_when_there_are_no_employer_items(self):
        line = self.line(self.solo_run, self.grace)
        context = payslip_context(line)
        self.assertTrue(context["employer"])
        from vs_finance.pdf import payslip_pdf

        html = rendered_html(payslip_pdf, {**context, "employer": [], "employer_total": "₦0.00"})

        self.assertNotIn("Paid by your employer on top of your pay", html)
        self.assertIn("Year to date", html)

    def test_an_employee_name_is_escaped(self):
        html = self.html(self.line(self.multi_run, self.script))

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_the_continuation_header_names_the_employee_and_document_escaped(self):
        line = self.line(self.multi_run, self.script)
        number = f"{self.multi_run.document_number or self.multi_run.pk}/{line.line_no}"
        html = self.html(line)

        self.assertIn(
            f'<div class="page-head">&lt;script&gt;alert(1)&lt;/script&gt; \u00b7 Payslip {number}</div>',
            html,
        )

    def test_the_stylesheet_never_carries_the_employees_words(self):
        from vs_finance.pdf import _payroll_stylesheet

        self.assertNotIn("script", _payroll_stylesheet())
        self.assertIn("element(page-head)", _payroll_stylesheet())

    def test_the_employer_total_is_the_contributions_together(self):
        line = self.line(self.solo_run, self.grace)

        self.assertEqual(
            payslip_context(line)["employer_total"],
            f"₦{line.employer_contributions_amount / N:,.2f}",
        )


class YearToDateOfADraftPayslipTests(_PayslipFixture):
    """A payslip previewed before its run posts prints the year the paid payslip will print.

    Grace is paid 100,000 for January (posted). Her February and March runs are
    both still drafts: opening March's preview counts March itself, and leaves
    February's draft out because that run has not been posted.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.feb_run = generate_run_from_roster(cls.solo_books, pay_date=base._date(2, 25))
        cls.mar_run = generate_run_from_roster(cls.solo_books, pay_date=base._date(3, 25))

    def ytd(self, run, key="gross"):
        return payslip_context(self.line(run, self.grace))["ytd"][key]

    def naira(self, kobo):
        return f"\u20a6{kobo / N:,.2f}"

    def test_a_draft_payslip_counts_its_own_line(self):
        february = self.line(self.feb_run, self.grace)
        january = self.line(self.solo_run, self.grace)
        self.assertEqual(self.ytd(self.feb_run), self.naira(january.gross_amount + february.gross_amount))
        self.assertEqual(self.ytd(self.feb_run, "net"), self.naira(january.net_amount + february.net_amount))

    def test_a_posted_payslip_counts_its_own_line_once(self):
        january = self.line(self.solo_run, self.grace)
        self.assertEqual(self.ytd(self.solo_run), self.naira(january.gross_amount))
        self.assertEqual(self.ytd(self.solo_run, "net"), self.naira(january.net_amount))

    def test_another_draft_run_of_the_same_person_is_not_counted(self):
        january = self.line(self.solo_run, self.grace)
        march = self.line(self.mar_run, self.grace)
        self.assertEqual(self.ytd(self.mar_run), self.naira(january.gross_amount + march.gross_amount))


class GrossSharesTests(SimpleTestCase):
    """Bisi's 210,000: net 174,988.75, pension 16,800, PAYE 15,586.25, NHF 2,625."""

    def line(self, gross, net):
        return types.SimpleNamespace(gross_amount=gross, net_amount=net)

    def test_net_first_then_deductions_largest_first(self):
        shares = gross_shares(self.line(21_000_000, 17_498_875), [
            item("National Housing Fund", 262_500), item("Pension", 1_680_000),
            item("PAYE", 1_558_625),
        ])

        self.assertEqual([s["name"] for s in shares], ["Net pay", "Pension", "PAYE", "National Housing Fund"])
        self.assertEqual([s["percent"] for s in shares], ["83.3", "8.0", "7.4", "1.3"])
        self.assertEqual([s["width"] for s in shares], ["83.33", "8.00", "7.42", "1.25"])

    def test_zero_amounts_are_skipped(self):
        shares = gross_shares(self.line(100_000, 90_000), [item("PAYE", 10_000), item("Loan", 0)])

        self.assertEqual([s["name"] for s in shares], ["Net pay", "PAYE"])

    def test_no_gross_pay_means_no_bar(self):
        self.assertEqual(gross_shares(self.line(0, 0), [item("PAYE", 5)]), [])
        self.assertEqual(gross_shares(self.line(-100, 0), []), [])

    def test_a_negative_net_counts_as_nothing(self):
        shares = gross_shares(self.line(100_000, -5_000), [item("PAYE", 20_000)])

        self.assertEqual(shares[0], {"name": "Net pay", "percent": "0.0", "width": "0.00"})


class TaxSummaryIsTheTemplateTests(_PayslipFixture):

    def summary(self):
        return tax_summary(self.solo_books, year=2026, salary=self.grace)

    def html(self, summary):
        from vs_finance.pdf import tax_summary_pdf

        return rendered_html(tax_summary_pdf, summary)

    def test_months_and_total_are_printed(self):
        html = self.html(self.summary())

        self.assertIn("Grace Eze", html)
        self.assertIn("Tax year 2026", html)
        self.assertIn("TIN-GRACE", html)
        self.assertIn('class="total"', html)
        self.assertIn("100,000.00", html)
        self.assertNotIn("Before this payroll", html)
        self.assertNotIn("Earlier this tax year", html)

    def test_the_opening_row_and_brought_forward_print_when_set(self):
        summary = self.summary()
        summary["opening"] = {"gross": 50_000 * N, "taxable_pay": 45_000 * N, "paye": 2_000 * N,
                              "pension": 4_000 * N, "nhf": 0}
        summary["brought_forward"] = {
            "employer_name": "Greenfield Academy", "gross": 90_000 * N, "taxable_pay": 80_000 * N,
            "paye": 4_500 * N, "pension": 7_200 * N,
        }
        html = self.html(summary)

        self.assertIn("Before this payroll", html)
        self.assertIn("Earlier this tax year with Greenfield Academy", html)
        self.assertIn("50,000.00", html)

    def test_the_continuation_header_names_the_employee_and_year_escaped(self):
        summary = self.summary()
        summary["employee_name"] = '"><b>Grace</b>'

        html = self.html(summary)

        self.assertIn(
            '<div class="page-head">&quot;&gt;&lt;b&gt;Grace&lt;/b&gt; \u00b7 Tax summary 2026</div>', html,
        )

    def test_the_employee_name_is_escaped(self):
        summary = self.summary()
        summary["employee_name"] = "<b>Grace</b>"

        html = self.html(summary)

        self.assertNotIn("<b>Grace</b>", html)
        self.assertIn("&lt;b&gt;Grace&lt;/b&gt;", html)


class WithoutWeasyPrintTests(_PayslipFixture):
    """A server missing Pango still sends both documents, and says so in the error log."""

    def test_the_payslip_falls_back_to_the_plain_layout(self):
        from vs_finance.pdf import payslip_pdf

        context = payslip_context(self.line(self.solo_run, self.grace))
        with mock.patch("vs_finance.pdf._weasyprint", return_value=None), \
                self.assertLogs("vs_finance.pdf", "ERROR") as logs:
            pdf = payslip_pdf(context)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertNotIn(b"WeasyPrint", pdf_bytes(pdf))
        self.assertIn("payslip_document.html", logs.output[0])

    def test_the_tax_summary_falls_back_to_the_plain_layout(self):
        from vs_finance.pdf import tax_summary_pdf

        summary = tax_summary(self.solo_books, year=2026, salary=self.grace)
        with mock.patch("vs_finance.pdf._weasyprint", return_value=None), \
                self.assertLogs("vs_finance.pdf", "ERROR") as logs:
            pdf = tax_summary_pdf(summary)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertNotIn(b"WeasyPrint", pdf_bytes(pdf))
        self.assertIn("tax_summary_document.html", logs.output[0])


class RealRenderTests(_PayslipFixture):

    def setUp(self):
        if not has_weasyprint():
            self.skipTest("WeasyPrint's native libraries are not installed")

    def test_the_payslip_embeds_the_bundled_typefaces(self):
        from vs_finance.pdf import payslip_pdf

        pdf = payslip_pdf(payslip_context(self.line(self.solo_run, self.grace)))

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"WeasyPrint", pdf_bytes(pdf))
        self.assertIn(b"Hanken-Grotesk", pdf_bytes(pdf))

    def test_the_tax_summary_embeds_the_bundled_typefaces(self):
        from vs_finance.pdf import tax_summary_pdf

        pdf = tax_summary_pdf(tax_summary(self.solo_books, year=2026, salary=self.grace))

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"Hanken-Grotesk", pdf_bytes(pdf))
