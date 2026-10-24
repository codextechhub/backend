"""Payloads that restate a registered customer or inter-branch figure honour its switch.

The customer master registers a customer's balance and account status
(``finance.customer.balance``, ``finance.customer.account_status``), and the
inter-branch register its amount and status. Other receivables payloads add up
or repeat the same values under other names: the customer statement and its
export, the dunning buckets, the refund picker, the refunds and write-offs
screen, a credit note's remaining credit, the open items on a receipt, and the
income a credit note gave back to another branch. Each is read here as a role
that may open the screen but may not read the figure it restates, beside a role
that may, and beside a different school.

Corona has three branches; Single Site has one; Rival Group is a third school.
"""
from __future__ import annotations

import datetime
import itertools

from vs_finance.constants import CreditNoteKind
from vs_finance.credit_notes import post_credit_note
from vs_finance.models import (
    Account,
    CreditNote,
    CreditNoteLine,
    FinanceDocumentSettings,
    Invoice,
    InvoiceLine,
    Payment,
    TaxCode,
)
from vs_finance.receivables import post_invoice, post_payment
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .inter_branch import transfer_open_receivables
from .tests_inter_branch import JAN_15, JAN_20
from .tests_screen_reads import JAN_10, _ReadsFixture

_seq = itertools.count(1)
CUSTOMER = "finance.customer"
INTERBRANCH = "finance.interbranch"


def _k(resource, *names):
    return [f"{resource}.{name}" for name in names]


class _RestatingFixture(_ReadsFixture):
    """Corona's books with the Okafor family (billed at Ikeja, credit at Lekki) and Tunde (moved)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields(CUSTOMER, INTERBRANCH)
        cls.revenue = Account.objects.get(entity=cls.books, code="4100")
        cls.vat = TaxCode.objects.get(entity=cls.books, code="VAT-STD")

        cls.family = cls.customer(cls.books, "OKAFOR", None)
        cls.ikeja_bill = cls.posted_invoice(cls.family, cls.ikeja, 100_000)
        cls.lekki_credit = Payment.objects.create(
            entity=cls.books, branch=cls.lekki, customer=cls.family,
            payment_date=datetime.date(2026, 1, 12), amount=30_000,
            deposit_account=cls.lekki_bank.gl_account,
        )
        post_payment(cls.lekki_credit, auto_allocate=False)
        cls.ikeja_receipt = Payment.objects.create(
            entity=cls.books, branch=cls.ikeja, customer=cls.family,
            payment_date=datetime.date(2026, 1, 13), amount=20_000,
            deposit_account=cls.ikeja_bank.gl_account,
        )
        post_payment(cls.ikeja_receipt, auto_allocate=False)

        cls.solo_family = cls.customer(cls.solo_books, "SOLOFAM", cls.solo_main)
        cls.solo_bill = cls.solo_posted_invoice(cls.solo_family)
        cls.rival_family = cls.customer(cls.rival_books, "RIVALFAM", None)

    @classmethod
    def solo_posted_invoice(cls, customer):
        invoice = Invoice.objects.create(
            entity=cls.solo_books, customer=customer, branch=cls.solo_main,
            invoice_date=JAN_10, due_date=datetime.date(2026, 1, 25),
        )
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=50_000,
            revenue_account=Account.objects.get(entity=cls.solo_books, code="4100"),
        )
        post_invoice(invoice)
        return invoice

    @classmethod
    def staff(cls, *keys, hide=(), branch=None, tenant=None):
        """A user holding ``keys``, with the registered ``hide`` keys switched off for their role."""
        tenant = tenant or cls.tenant
        name = f"restate-{next(_seq)}"
        user = cls.grant(
            cls.user_for(tenant, f"{name}@restate.test"), *keys,
            tenant=tenant, role_key=name, branch=branch,
        )
        if hide:
            role = TenantRoleTemplate.objects.get(tenant=tenant, key=name)
            set_field_access(role, *hide, read=False, write=False)
        return user

    def get(self, user, path, books=None, **params):
        query = "".join(f"&{k}={v}" for k, v in params.items())
        return self.as_(user).get(self.url(path, books) + query)

    def data(self, user, path, books=None, **params):
        return self.rows(self.get(user, path, books, **params))


class CustomerStatementAccessTests(_RestatingFixture):
    """The customer statement serves a figure only to a role that may read what it restates."""

    STATEMENT = ("finance.report.view",)
    BALANCE_KEYS = ("opening_balance", "total_debits", "total_credits", "closing_balance")

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(*cls.STATEMENT)
        cls.no_balance = cls.staff(*cls.STATEMENT, hide=_k(CUSTOMER, "balance"))
        cls.no_status = cls.staff(*cls.STATEMENT, hide=_k(CUSTOMER, "account_status"))
        cls.no_lines = cls.staff(*cls.STATEMENT, hide=_k(CUSTOMER, "statement"))
        cls.ikeja_no_balance = cls.staff(
            *cls.STATEMENT, hide=_k(CUSTOMER, "balance"), branch=cls.ikeja)
        cls.solo_no_balance = cls.staff(
            *cls.STATEMENT, hide=_k(CUSTOMER, "balance"), tenant=cls.solo_tenant,
            branch=cls.solo_main)
        cls.solo_open = cls.staff(*cls.STATEMENT, tenant=cls.solo_tenant, branch=cls.solo_main)
        cls.rival_open = cls.staff(*cls.STATEMENT, tenant=cls.rival_tenant)

    def statement(self, user, customer=None, books=None, **params):
        return self.data(
            user, "reports/customer-statement/", books,
            customer=(customer or self.family).code, **params)

    def test_an_open_role_reads_the_whole_statement(self):
        data = self.statement(self.open)

        for name in self.BALANCE_KEYS:
            self.assertIn(name, data)
        self.assertIn("aging", data)
        self.assertTrue(data["entries"])
        for entry in data["entries"]:
            self.assertIn("balance", entry)

    def test_a_hidden_balance_leaves_every_figure_that_adds_up_to_it(self):
        data = self.statement(self.no_balance)

        for name in self.BALANCE_KEYS:
            self.assertNotIn(name, data)
        self.assertNotIn("aging", data)
        self.assertTrue(data["entries"])
        for entry in data["entries"]:
            self.assertNotIn("balance", entry)
            self.assertIn("debit", entry)
        self.assertEqual(data["customer"]["code"], "OKAFOR")

    def test_a_hidden_account_status_takes_the_age_analysis_and_nothing_else(self):
        data = self.statement(self.no_status)

        self.assertNotIn("aging", data)
        for name in self.BALANCE_KEYS:
            self.assertIn(name, data)

    def test_hidden_statement_lines_leave_the_entries_but_not_the_balances(self):
        data = self.statement(self.no_lines)

        self.assertNotIn("entries", data)
        self.assertIn("closing_balance", data)
        self.assertIn("aging", data)

    def test_the_export_drops_the_balance_column_and_the_total_row(self):
        hidden = self.get(
            self.no_balance, "reports/customer-statement/", customer="OKAFOR", export="csv",
        )
        shown = self.get(self.open, "reports/customer-statement/", customer="OKAFOR", export="csv")

        self.assertEqual(hidden.status_code, 200)
        text = hidden.content.decode()
        self.assertNotIn("Balance", text)
        self.assertNotIn("TOTAL", text)
        self.assertNotIn("opening", text)
        self.assertIn("Debit", text)
        self.assertIn("Balance", shown.content.decode())
        self.assertIn("TOTAL", shown.content.decode())

    def test_a_branch_reader_with_a_hidden_balance_gets_their_branchs_lines_only(self):
        data = self.statement(self.ikeja_no_balance)

        self.assertNotIn("closing_balance", data)
        self.assertNotIn("aging", data)
        numbers = {entry["document_number"] for entry in data["entries"]}
        self.assertIn(self.ikeja_bill.document_number, numbers)
        self.assertNotIn(self.lekki_credit.document_number, numbers)

    def test_a_single_branch_school_follows_the_same_switches(self):
        hidden = self.statement(self.solo_no_balance, self.solo_family, self.solo_books)
        shown = self.statement(self.solo_open, self.solo_family, self.solo_books)

        self.assertNotIn("closing_balance", hidden)
        self.assertEqual(shown["closing_balance"]["kobo"], 50_000)

    def test_one_schools_switch_does_not_reach_another_school(self):
        data = self.statement(self.rival_open, self.rival_family, self.rival_books)

        self.assertIn("closing_balance", data)
        self.assertEqual(data["closing_balance"]["kobo"], 0)

    def test_a_school_cannot_read_another_schools_statement(self):
        response = self.get(
            self.open, "reports/customer-statement/", self.rival_books, customer="RIVALFAM")

        self.assertIn(response.status_code, (403, 404))


class DunningSummaryAccessTests(_RestatingFixture):
    """The dunning buckets are an age analysis of what customers owe."""

    KEYS = ("finance.dunning.view",)
    BUCKETS = ("due_soon", "overdue_1_30", "overdue_31_60", "overdue_60_plus")

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(*cls.KEYS)
        cls.no_balance = cls.staff(*cls.KEYS, hide=_k(CUSTOMER, "balance"))
        cls.no_status = cls.staff(*cls.KEYS, hide=_k(CUSTOMER, "account_status"))
        cls.lekki_open = cls.staff(*cls.KEYS, branch=cls.lekki)
        cls.solo_no_balance = cls.staff(
            *cls.KEYS, hide=_k(CUSTOMER, "balance"), tenant=cls.solo_tenant, branch=cls.solo_main)

    def summary(self, user, books=None):
        return self.data(user, "dunning/summary/", books)

    def test_an_open_role_reads_every_bucket_with_its_amount(self):
        data = self.summary(self.open)

        self.assertEqual(set(data), set(self.BUCKETS))
        owed = sum(bucket["amount"] for bucket in data.values())
        self.assertEqual(owed, 100_000)

    def test_a_hidden_balance_keeps_the_counts_and_drops_the_amounts(self):
        data = self.summary(self.no_balance)

        self.assertEqual(set(data), set(self.BUCKETS))
        for bucket in data.values():
            self.assertEqual(set(bucket), {"count"})
        self.assertEqual(sum(bucket["count"] for bucket in data.values()), 1)

    def test_a_hidden_account_status_serves_no_buckets(self):
        response = self.get(self.no_status, "dunning/summary/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"], {})

    def test_a_branch_reader_counts_only_their_branchs_bills(self):
        data = self.summary(self.lekki_open)

        self.assertEqual(sum(bucket["count"] for bucket in data.values()), 0)

    def test_a_single_branch_school_follows_the_same_switch(self):
        data = self.summary(self.solo_no_balance, self.solo_books)

        self.assertEqual(sum(bucket["count"] for bucket in data.values()), 1)
        for bucket in data.values():
            self.assertNotIn("amount", bucket)


class CustomerCreditAccessTests(_RestatingFixture):
    """What a customer holds in credit is their balance seen through the refund screens."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.list_keys = ("finance.customer.view",)
        cls.open_list = cls.staff(*cls.list_keys)
        cls.no_balance_list = cls.staff(*cls.list_keys, hide=_k(CUSTOMER, "balance"))
        cls.open_refunds = cls.staff("finance.refund.create", "finance.refund.view")
        cls.no_balance_refunds = cls.staff(
            "finance.refund.create", "finance.refund.view", hide=_k(CUSTOMER, "balance"))
        cls.lekki_no_balance = cls.staff(
            "finance.refund.create", "finance.refund.view", hide=_k(CUSTOMER, "balance"),
            branch=cls.lekki)
        cls.lekki_open = cls.staff("finance.refund.create", "finance.refund.view", branch=cls.lekki)
        cls.credit_open = cls.staff("finance.creditnote.view")
        cls.credit_no_balance = cls.staff("finance.creditnote.view", hide=_k(CUSTOMER, "balance"))
        cls.note = CreditNote.objects.create(
            entity=cls.books, customer=cls.family, branch=cls.ikeja,
            kind=CreditNoteKind.CREDIT, note_date=JAN_20, reason="Overbilled",
        )
        CreditNoteLine.objects.create(
            note=cls.note, line_no=1, quantity=1, unit_price=10_000, revenue_account=cls.revenue)
        post_credit_note(cls.note)

    def test_the_customer_list_balance_follows_the_balance_switch(self):
        shown = next(r for r in self.data(self.open_list, "customers/") if r["code"] == "OKAFOR")
        hidden = next(r for r in self.data(self.no_balance_list, "customers/")
                      if r["code"] == "OKAFOR")

        self.assertIn("balance", shown)
        self.assertNotIn("balance", hidden)
        self.assertNotIn("balance_naira", hidden)

    def test_refund_availability_offers_credit_only_to_a_role_that_may_read_balances(self):
        shown = self.data(self.open_refunds, "refunds/availability/")
        hidden = self.get(self.no_balance_refunds, "refunds/availability/")

        self.assertEqual([row["refundable_credit"] for row in shown], [30_000, 30_000])
        self.assertEqual(hidden.status_code, 200)
        self.assertEqual(hidden.data["data"], [])

    def test_a_branch_reader_is_offered_their_own_branchs_credit_only(self):
        shown = self.data(self.lekki_open, "refunds/availability/")
        hidden = self.get(self.lekki_no_balance, "refunds/availability/")

        self.assertEqual([row["refundable_credit"] for row in shown], [30_000])
        self.assertEqual(hidden.data["data"], [])

    def test_the_adjustments_total_credit_follows_the_balance_switch(self):
        shown = self.get(self.open_refunds, "ar-adjustments/").data["kpis"]
        hidden = self.get(self.no_balance_refunds, "ar-adjustments/").data["kpis"]

        self.assertEqual(shown["refundable_credit"], 60_000)
        self.assertNotIn("refundable_credit", hidden)
        self.assertIn("pending", hidden)

    def test_a_branch_reader_totals_their_branchs_credit_only(self):
        shown = self.get(self.lekki_open, "ar-adjustments/").data["kpis"]

        self.assertEqual(shown["refundable_credit"], 30_000)

    def test_a_credit_notes_remaining_credit_follows_the_balance_switch(self):
        shown = self.data(self.credit_open, "credit-notes/")
        hidden = self.data(self.credit_no_balance, "credit-notes/")
        detail_hidden = self.data(self.credit_no_balance, f"credit-notes/{self.note.pk}/")

        self.assertIn("credit_remaining", shown[0])
        self.assertNotIn("credit_remaining", hidden[0])
        self.assertNotIn("credit_remaining", detail_hidden)
        self.assertIn("total", hidden[0])


class ReceiptOpenItemsAccessTests(_RestatingFixture):
    """A receipt offers the customer's open items under the customer's own switches."""

    KEYS = ("finance.payment.view",)

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(*cls.KEYS)
        cls.no_invoices = cls.staff(*cls.KEYS, hide=_k(CUSTOMER, "open_invoices"))
        cls.no_notes = cls.staff(*cls.KEYS, hide=_k(CUSTOMER, "open_debit_notes"))
        cls.no_balance = cls.staff(*cls.KEYS, hide=_k(CUSTOMER, "balance"))
        cls.ikeja_open = cls.staff(*cls.KEYS, branch=cls.ikeja)
        cls.lekki_open = cls.staff(*cls.KEYS, branch=cls.lekki)
        cls.debit_note = CreditNote.objects.create(
            entity=cls.books, customer=cls.family, branch=cls.ikeja,
            kind=CreditNoteKind.DEBIT, note_date=JAN_20, reason="Lab fee",
        )
        CreditNoteLine.objects.create(
            note=cls.debit_note, line_no=1, quantity=1, unit_price=5_000,
            revenue_account=cls.revenue)
        FinanceDocumentSettings.objects.update_or_create(  # Credit held would pay the note as it posts.
            entity=cls.books, defaults={"auto_apply_customer_credit": False})
        post_credit_note(cls.debit_note)
        FinanceDocumentSettings.objects.filter(entity=cls.books).update(
            auto_apply_customer_credit=True)
        lekki_bill = cls.posted_invoice(cls.family, cls.lekki, 70_000)
        cls.lekki_bill_number = lekki_bill.document_number

    def receipt(self, user):
        return self.data(user, f"payments/{self.ikeja_receipt.pk}/")

    def test_an_open_role_is_offered_both_lists_with_balances(self):
        data = self.receipt(self.open)

        self.assertEqual(len(data["open_invoices"]), 2)
        self.assertEqual(len(data["open_debit_notes"]), 1)
        self.assertIn("balance", data["open_invoices"][0])

    def test_each_list_follows_its_own_switch(self):
        no_invoices = self.receipt(self.no_invoices)
        no_notes = self.receipt(self.no_notes)

        self.assertNotIn("open_invoices", no_invoices)
        self.assertIn("open_debit_notes", no_invoices)
        self.assertNotIn("open_debit_notes", no_notes)
        self.assertIn("open_invoices", no_notes)

    def test_a_hidden_balance_leaves_the_lists_without_their_balances(self):
        data = self.receipt(self.no_balance)

        self.assertEqual(len(data["open_invoices"]), 2)
        for row in (*data["open_invoices"], *data["open_debit_notes"]):
            self.assertNotIn("balance", row)
            self.assertIn("document_number", row)

    def test_a_branch_reader_is_offered_their_own_branchs_items_only(self):
        data = self.receipt(self.ikeja_open)

        numbers = {row["document_number"] for row in data["open_invoices"]}
        self.assertIn(self.ikeja_bill.document_number, numbers)
        self.assertNotIn(self.lekki_bill_number, numbers)


class IncomeGivenBackAccessTests(_RestatingFixture):
    """Tunde's 100k textbook bill moves from Ikeja to Lekki, and Lekki then credits 40k of it."""

    KEYS = ("finance.creditnote.view", "finance.concession.view")

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tunde = cls.customer(cls.books, "TUNDE", cls.ikeja)
        cls.textbooks = Invoice.objects.create(
            entity=cls.books, customer=cls.tunde, branch=cls.ikeja,
            invoice_date=JAN_10, due_date=datetime.date(2026, 1, 25),
        )
        InvoiceLine.objects.create(
            invoice=cls.textbooks, line_no=1, quantity=1, unit_price=100_000,
            revenue_account=cls.revenue, tax_code=cls.vat)
        post_invoice(cls.textbooks)
        transfer_open_receivables(cls.tunde, cls.ikeja, cls.lekki, None, move_date=JAN_15)
        cls.note = CreditNote.objects.create(
            entity=cls.books, customer=cls.tunde, branch=cls.lekki,
            kind=CreditNoteKind.CREDIT, note_date=JAN_20,
            invoice=Invoice.objects.get(pk=cls.textbooks.pk), reason="Never delivered",
        )
        CreditNoteLine.objects.create(
            note=cls.note, line_no=1, quantity=1, unit_price=40_000,
            revenue_account=cls.revenue, tax_code=cls.vat)
        post_credit_note(cls.note)
        cls.open = cls.staff(*cls.KEYS, branch=cls.lekki)
        cls.no_amount = cls.staff(*cls.KEYS, hide=_k(INTERBRANCH, "amount"), branch=cls.lekki)
        cls.no_status = cls.staff(*cls.KEYS, hide=_k(INTERBRANCH, "status"), branch=cls.lekki)
        cls.no_both = cls.staff(
            *cls.KEYS, hide=_k(INTERBRANCH, "amount", "status"), branch=cls.lekki)
        cls.solo_no_amount = cls.staff(
            *cls.KEYS, hide=_k(INTERBRANCH, "amount"), tenant=cls.solo_tenant,
            branch=cls.solo_main)

    def given_back(self, user):
        return self.data(user, f"credit-notes/{self.note.pk}/")["income_given_back"]

    def test_an_open_role_reads_the_amount_and_status(self):
        (row,) = self.given_back(self.open)

        self.assertEqual((row["amount"], row["status"]), (43_000, "POSTED"))

    def test_a_hidden_amount_leaves_the_status_and_the_identity(self):
        (row,) = self.given_back(self.no_amount)

        self.assertNotIn("amount", row)
        self.assertEqual(row["status"], "POSTED")
        self.assertEqual(row["to_branch_name"], "Ikeja Branch")
        self.assertTrue(row["document_number"])

    def test_a_hidden_status_leaves_the_amount(self):
        (row,) = self.given_back(self.no_status)

        self.assertNotIn("status", row)
        self.assertEqual(row["amount"], 43_000)

    def test_the_list_filters_each_note_the_same_way(self):
        (listed,) = [r for r in self.data(self.no_both, "credit-notes/") if r["id"] == self.note.pk]

        (row,) = listed["income_given_back"]
        self.assertNotIn("amount", row)
        self.assertNotIn("status", row)
        self.assertIn("to_branch_id", row)

    def test_a_concession_filters_its_rows_too(self):
        from vs_finance.installments import post_concession
        from vs_finance.models import Concession
        from vs_finance.voids import void_concession

        concession = Concession.objects.create(
            entity=self.books, customer=self.tunde,
            invoice=Invoice.objects.get(pk=self.textbooks.pk),
            branch=self.lekki, concession_date=JAN_20, amount=30_000,
        )
        post_concession(concession)
        void_concession(concession, date=JAN_20)

        (row,) = self.data(self.no_amount, f"concessions/{concession.pk}/")["income_given_back"]
        (listed,) = self.data(self.no_amount, "concessions/")[0]["income_given_back"],
        self.assertNotIn("amount", row)
        self.assertEqual(row["status"], "REVERSED")
        self.assertNotIn("amount", listed[0])
