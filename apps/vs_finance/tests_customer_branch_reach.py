"""A customer's record shows only the documents in the reader's branches.

The Okafor family is one school-wide customer with a child at Ikeja and a child
at Lekki. Corona bills them at both branches and once for the school as a whole:

* Ikeja invoices 100,000 kobo and banks 60,000 against it;
* Lekki invoices 100,000 and banks 30,000 that is not yet applied (credit);
* the school raises a 100,000 invoice with no branch.

The customer is master data, shared across the school, so the Ikeja bursar can
open the family. The documents under it are transactions, and the Ikeja bursar
sees Ikeja's alone: its invoice, its receipt, and the 40,000 they leave owing.
Not Lekki's invoice or receipt, and not the school-wide invoice either, because
a school-wide transaction is not shared with a branch-bound reader. The head of
finance, who covers the whole school, sees all of it.

Every customer-level figure is checked, not only the record: the list's balance
column, the header cards and the statement of account, since each of them adds
the same documents up.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, InvoiceLine, Payment

from .tests_branch_scope import _FinanceBranchFixture

INVOICE = 100_000


class _FamilyFixture(_FinanceBranchFixture):
    KEYS = ("finance.customer.view", "finance.report.view")

    def setUp(self):
        from vs_finance.receivables import post_invoice, post_payment

        super().setUp()
        e = self.books
        income = Account.objects.get(entity=e, code="4100")
        bank = Account.objects.get(entity=e, code="1100")

        def posted_invoice(customer, branch):
            invoice = self.invoice(e, customer, branch)
            InvoiceLine.objects.filter(invoice=invoice).update(revenue_account=income)
            post_invoice(invoice)
            invoice.refresh_from_db()
            return invoice

        def receipt(customer, branch, amount, allocations=None):
            payment = Payment.objects.create(
                entity=e, customer=customer, branch=branch,
                payment_date=datetime.date(2026, 1, 15), amount=amount,
                deposit_account=bank,
            )
            if allocations is None:
                post_payment(payment, auto_allocate=False)
            else:
                post_payment(payment, allocations=allocations)
            payment.refresh_from_db()
            return payment

        self.family = self.customer(e, "OKAFOR", None)
        self.ikeja_invoice = posted_invoice(self.family, self.ikeja)
        self.lekki_invoice = posted_invoice(self.family, self.lekki)
        self.school_invoice = posted_invoice(self.family, None)
        self.ikeja_receipt = receipt(
            self.family, self.ikeja, 60_000, [(self.ikeja_invoice, 60_000)])
        self.lekki_receipt = receipt(self.family, self.lekki, 30_000)

        self.bursar = self.client_for(
            self.tenant, "ikeja.bursar@example.com", "reach-ikeja", branch=self.ikeja)
        self.head = self.client_for(self.tenant, "head@example.com", "reach-head")

        # The single-branch school: a bursar pinned to its only branch.
        self.solo_family = self.customer(self.solo_books, "SOLOFAM", self.solo_main)
        solo_invoice = self.invoice(self.solo_books, self.solo_family, self.solo_main)
        InvoiceLine.objects.filter(invoice=solo_invoice).update(
            revenue_account=Account.objects.get(entity=self.solo_books, code="4100"))
        post_invoice(solo_invoice)
        self.solo_invoice = solo_invoice
        self.solo_bursar = self.client_for(
            self.solo_tenant, "solo.bursar@example.com", "reach-solo",
            branch=self.solo_main)

    def client_for(self, tenant, email, role_key, *, branch=None):
        user = self.grant(
            self.user_for(tenant, email), *self.KEYS,
            tenant=tenant, role_key=role_key, branch=branch,
        )
        return TenantAPIClient(user=user)

    def fetch(self, client, path, entity, **params):
        query = "".join(f"&{k}={v}" for k, v in params.items())
        response = client.get(f"/v1/finance/{path}?entity={entity.code}{query}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]


class CustomerRecordTests(_FamilyFixture):
    """The customer drawer: open items, history, statement and summary."""

    def record(self, client, customer=None, entity=None):
        customer = customer or self.family
        return self.fetch(client, f"customers/{customer.code}/", entity or self.books)

    def test_a_branch_bursar_sees_only_her_branchs_open_invoices(self):
        data = self.record(self.bursar)

        self.assertEqual(
            [row["document_number"] for row in data["open_invoices"]],
            [self.ikeja_invoice.document_number],
        )

    def test_another_branchs_documents_are_absent_from_the_history(self):
        data = self.record(self.bursar)
        references = {row["reference"] for row in data["transactions"]}
        statement = " ".join(row["description"] for row in data["statement"])

        self.assertEqual(
            references,
            {self.ikeja_invoice.document_number, self.ikeja_receipt.document_number},
        )
        for foreign in (self.lekki_invoice, self.lekki_receipt, self.school_invoice):
            self.assertNotIn(foreign.document_number, statement)

    def test_the_summary_adds_up_the_documents_shown_and_no_others(self):
        summary = self.record(self.bursar)["summary"]

        self.assertEqual(summary["current_balance"]["kobo"], INVOICE - 60_000)
        self.assertEqual(summary["lifetime_paid"]["kobo"], 60_000)
        self.assertEqual(summary["open_invoice_count"], 1)

    def test_a_whole_school_reader_sees_the_whole_account(self):
        data = self.record(self.head)

        self.assertEqual(len(data["open_invoices"]), 3)
        # 240,000 still owed on the three invoices, less Lekki's 30,000 credit.
        self.assertEqual(data["summary"]["current_balance"]["kobo"], 210_000)
        self.assertEqual(data["summary"]["lifetime_paid"]["kobo"], 90_000)

    def test_a_single_branch_school_loses_nothing(self):
        data = self.record(self.solo_bursar, self.solo_family, self.solo_books)

        self.assertEqual(
            [row["document_number"] for row in data["open_invoices"]],
            [self.solo_invoice.document_number],
        )
        self.assertEqual(data["summary"]["current_balance"]["kobo"], INVOICE)


class CustomerAggregateTests(_FamilyFixture):
    """The list, its header cards and the statement add up the same documents."""

    def test_the_list_balance_is_the_branchs_balance(self):
        rows = self.fetch(self.bursar, "customers/", self.books)
        row = next(r for r in rows if r["code"] == self.family.code)

        self.assertEqual(row["balance"], INVOICE - 60_000)

    def test_the_header_cards_count_only_the_branchs_receivable(self):
        data = self.fetch(self.bursar, "customers/summary/", self.books)

        self.assertEqual(data["receivable"]["kobo"], INVOICE - 60_000)

    def test_the_statement_of_account_is_the_branchs_account(self):
        data = self.fetch(
            self.bursar, "reports/customer-statement/", self.books,
            customer=self.family.code,
        )

        self.assertEqual(
            {row["document_number"] for row in data["entries"]},
            {self.ikeja_invoice.document_number, self.ikeja_receipt.document_number},
        )
        self.assertEqual(data["closing_balance"]["kobo"], INVOICE - 60_000)
        self.assertEqual(sum(v["kobo"] for v in data["aging"].values()), INVOICE - 60_000)

    def test_a_whole_school_reader_gets_the_whole_statement(self):
        data = self.fetch(
            self.head, "reports/customer-statement/", self.books,
            customer=self.family.code,
        )

        self.assertEqual(len(data["entries"]), 5)
        self.assertEqual(data["closing_balance"]["kobo"], 3 * INVOICE - 90_000)
