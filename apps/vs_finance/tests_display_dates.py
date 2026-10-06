"""Finance documents and refusals write dates the way the school reads them.

Corona Group writes dates DD/MM/YYYY. Its printed invoice, a statement's
period, the fiscal calendar warning and a backdated-posting refusal all say
"05/01/2026"; the structured fields a client reads stay ISO. A school that has
chosen nothing reads "5 Jan 2026".
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace

from vs_config.clock import forget_tenant_zone
from vs_config.display import DATE_FORMAT_KEY
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_finance.models import Invoice

from .tests_branch_scope import _FinanceBranchFixture

JAN_5 = datetime.date(2026, 1, 5)
FEB_4 = datetime.date(2026, 2, 4)


class FinanceDatesFollowTheSchoolTests(_FinanceBranchFixture):

    def setUp(self):
        super().setUp()
        set_value(
            definition=ConfigurationDefinition.objects.get(key=DATE_FORMAT_KEY),
            value="DD_MM_YYYY", actor=None, tenant=self.tenant,
        )
        forget_tenant_zone(self.tenant)

    def test_the_printed_invoice_writes_its_dates_the_schools_way(self):
        from vs_finance.documents import invoice_document_context

        customer = self.customer(self.books, "DISP1", self.ikeja)
        invoice = Invoice.objects.create(
            entity=self.books, customer=customer, branch=self.ikeja,
            invoice_date=JAN_5, due_date=FEB_4,
        )
        printed = invoice_document_context(invoice)["invoice"]
        self.assertEqual((printed["invoice_date"], printed["due_date"]), ("05/01/2026", "04/02/2026"))

    def test_a_closed_petty_cash_fund_names_its_closing_day_the_schools_way(self):
        from vs_finance.exceptions import PettyCashError
        from vs_finance.models import Account, PettyCashFund
        from vs_finance.petty_cash import _refuse_closed

        fund = PettyCashFund(
            entity=self.books, branch=self.ikeja, name="Front desk",
            gl_account=Account(entity=self.books, code="1099"), closed_on=JAN_5,
        )
        with self.assertRaises(PettyCashError) as refused:
            _refuse_closed(fund)
        self.assertIn("was closed on 05/01/2026.", str(refused.exception))

    def test_a_statement_period_names_both_ends_or_inception(self):
        from vs_finance.documents import statement_period

        self.assertEqual(
            statement_period(SimpleNamespace(start_date=JAN_5, end_date=FEB_4), self.tenant),
            "05/01/2026 to 04/02/2026",
        )
        self.assertEqual(
            statement_period(SimpleNamespace(start_date=None, end_date=FEB_4), self.solo_tenant),
            "inception to 4 Feb 2026",
        )

    def test_a_backdated_posting_refusal_names_the_days_and_keeps_iso_fields(self):
        from vs_finance.chronology import ensure_on_or_after
        from vs_finance.exceptions import BackdatedPostingError

        with self.assertRaises(BackdatedPostingError) as caught:
            ensure_on_or_after(
                subject="Write-off WO-1", subject_date=JAN_5,
                source="invoice INV-1", source_date=FEB_4,
                remedy="Date the write-off 04/02/2026 or later.", tenant=self.tenant,
            )
        error = caught.exception
        self.assertIn("is dated 05/01/2026, but invoice INV-1 only exists from 04/02/2026.", str(error.message))
        self.assertEqual(error.extra.get("subject_date"), "2026-01-05")

    def test_the_fiscal_calendar_warning_names_its_last_day_the_schools_way(self):
        from vs_finance.fiscal_calendar import _situation

        runway = {
            "calendar_end": datetime.date(2026, 12, 31),
            "first_uncovered_date": datetime.date(2027, 1, 1),
            "days_remaining": 10, "gaps": [],
        }
        self.assertIn("ends on 31/12/2026", _situation(runway, self.tenant))
        self.assertIn("ends on 31 Dec 2026", _situation(runway, self.solo_tenant))
