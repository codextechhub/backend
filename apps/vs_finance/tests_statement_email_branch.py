"""A statement emailed by a branch bursar covers their branches' documents only.

The Okafor family is shared by every branch at Corona: Ikeja bills them 1,000 and
Lekki 1,000. Mrs Adeyemi, the Ikeja bursar, reads their statement on screen as
Ikeja's 1,000, and the statement they email them says the same. Mr Eze, the
whole-school bursar, sends the whole account: 2,000.
"""
from __future__ import annotations

import datetime

from vs_finance.constants import (
    FinanceDeliveryDocument, FinanceDeliverySource, FinanceDeliveryStatus,
)
from vs_finance.models import FinanceDocumentDelivery

from .tests_branch_scope import _FinanceBranchFixture


class StatementEmailFollowsTheSendersBranchesTests(_FinanceBranchFixture):

    def setUp(self):
        from vs_finance.models import Account, InvoiceLine
        from vs_finance.receivables import post_invoice

        super().setUp()
        self.okafor = self.customer(self.books, "COKAF", None)
        income = Account.objects.get(entity=self.books, code="4100")
        for branch in (self.ikeja, self.lekki):
            invoice = self.invoice(self.books, self.okafor, branch)
            InvoiceLine.objects.filter(invoice=invoice).update(revenue_account=income)
            post_invoice(invoice)
        self.adeyemi = self.grant(
            self.user_for(self.tenant, "adeyemi@corona.test"), "finance.customer.email_statement",
            tenant=self.tenant, role_key="stmt-ikeja", branch=self.ikeja)
        self.eze = self.grant(
            self.user_for(self.tenant, "eze@corona.test"), "finance.customer.email_statement",
            tenant=self.tenant, role_key="stmt-hq")

    def rendered(self, sender):
        from vs_finance.document_email import _render

        delivery = FinanceDocumentDelivery.objects.create(
            entity=self.books, customer=self.okafor,
            document_type=FinanceDeliveryDocument.STATEMENT,
            period_end=datetime.date(2026, 1, 31), source=FinanceDeliverySource.MANUAL,
            status=FinanceDeliveryStatus.PENDING, requested_by=sender,
            recipients=["okafor@example.com"],
        )
        _pdf, context, _name, branch = _render(delivery)
        return context, branch

    def test_a_branch_bursars_statement_is_their_branchs_account(self):
        context, branch = self.rendered(self.adeyemi)

        self.assertEqual(context["total_charges"], "₦1,000.00")
        self.assertEqual(context["entry_count"], 1)
        self.assertEqual(branch, self.ikeja)

    def test_a_whole_school_senders_statement_is_the_whole_account(self):
        context, branch = self.rendered(self.eze)

        self.assertEqual(context["total_charges"], "₦2,000.00")
        self.assertEqual(context["entry_count"], 2)
        self.assertIsNone(branch)

    def test_an_automatic_statement_is_the_whole_account(self):
        context, _branch = self.rendered(None)

        self.assertEqual(context["total_charges"], "₦2,000.00")
