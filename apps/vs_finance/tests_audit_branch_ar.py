"""The receivables paths' audit entries carry their document's branch.

Corona's Eze family is billed at Ikeja and the Adeyemi and Okoro families at
Lekki. Ngozi keeps Lekki's books, pinned to Lekki, with the audit-view key.
Every entry the receivables paths write is about a document that names a
branch, and :func:`vs_finance.audit.record` files it there: a receipt settling a
bill, credit applying itself to a new bill, a credit transfer (which moves
credit within one branch's books, so it is one entry under that branch), an
opening invoice, and a customer taken off the roll. Ngozi reads Lekki's and
never Ikeja's. A customer every branch shares has no branch, so the entry about
deactivating one is read by whole-school readers only.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus, FinanceAuditAction
from vs_finance.models import CustomerCreditTransfer, FinanceAuditLog

from .tests_ar_guards import D, _ArFixture


class _ReaderFixture(_ArFixture):

    def setUp(self):
        super().setUp()
        self.adeyemi = self.customer(self.books, "ADEYEMI", self.lekki)
        self.okoro = self.customer(self.books, "OKORO", self.lekki)
        user = self.grant(self.user_for(self.tenant, "ngozi.audit@corona.test"), "finance.audit.view",
                          tenant=self.tenant, role_key="lekki-audit", branch=self.lekki)
        self.ngozi = TenantAPIClient(user=user)

    def entries(self, action):
        return {e.branch_id: e for e in FinanceAuditLog.objects.filter(entity=self.books, action=action)}

    def seen(self, action):
        response = self.ngozi.get(
            f"/v1/finance/audit-logs/?entity={self.books.code}&action={action}&page_size=100")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in response.data["data"]}

    def assert_lekkis_only(self, action):
        by_branch = self.entries(action)
        self.assertEqual(set(by_branch), {self.ikeja.pk, self.lekki.pk})
        self.assertEqual(self.seen(action), {by_branch[self.lekki.pk].pk})


class SettlementEntriesTests(_ReaderFixture):

    def test_a_settlement_is_its_receipts_branchs(self):
        from vs_finance.receivables import allocate_payment

        self.settings(auto_apply_customer_credit=False)
        for customer, branch in ((self.eze, self.ikeja), (self.adeyemi, self.lekki)):
            bill = self.bill(customer, 100_000, branch=branch)
            receipt = self.receipt(customer, 100_000, branch=branch)
            allocate_payment(receipt, allocations=[(bill, 100_000)])

        self.assert_lekkis_only(FinanceAuditAction.PAYMENT_ALLOCATED)

    def test_credit_applying_itself_is_the_credits_branchs(self):
        for customer, branch in ((self.eze, self.ikeja), (self.adeyemi, self.lekki)):
            self.receipt(customer, 150_000, day=3, branch=branch)
            bill = self.bill(customer, 150_000, branch=branch)
            self.assertEqual(bill.balance_due, 0)

        self.assert_lekkis_only(FinanceAuditAction.PAYMENT_ALLOCATED)


class CreditTransferEntriesTests(_ReaderFixture):

    def test_a_transfer_is_one_entry_under_its_branch(self):
        from vs_finance.credit_transfers import post_customer_credit_transfer

        for source, destination, branch in ((self.eze, self.bello, self.ikeja),
                                            (self.adeyemi, self.okoro, self.lekki)):
            self.bill(destination, 100_000, branch=branch)
            self.receipt(source, 120_000, day=11, branch=branch)
            post_customer_credit_transfer(CustomerCreditTransfer.objects.create(
                entity=self.books, branch=branch, from_customer=source, to_customer=destination,
                transfer_date=D(2026, 1, 15), amount=100_000, status=DocumentStatus.APPROVED,
            ))

        self.assert_lekkis_only(FinanceAuditAction.CREDIT_TRANSFER_POSTED)


class CustomerEntriesTests(_ReaderFixture):

    def test_deactivating_a_customer_is_their_branchs_or_whole_school(self):
        from vs_finance.customers import set_customer_active

        shared = self.customer(self.books, "SHARED", None)
        for customer in (self.eze, self.adeyemi, shared):
            set_customer_active(customer, False, reason="Left the school.")

        by_branch = self.entries(FinanceAuditAction.CUSTOMER_DEACTIVATED)
        self.assertEqual(set(by_branch), {self.ikeja.pk, self.lekki.pk, None})
        self.assertEqual(self.seen(FinanceAuditAction.CUSTOMER_DEACTIVATED), {by_branch[self.lekki.pk].pk})

    def test_an_opening_invoice_is_its_customers_branchs(self):
        client = self.client_with("finance.customer.import_opening")

        response = client.post(f"/v1/finance/customers/opening/?entity={self.books.code}", {"invoices": [
            {"customer": "EZE", "invoice_date": "2022-09-10", "amount": 400_000},
            {"customer": "ADEYEMI", "invoice_date": "2022-09-10", "amount": 300_000},
        ]}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assert_lekkis_only(FinanceAuditAction.CUSTOMER_OPENING_POSTED)
