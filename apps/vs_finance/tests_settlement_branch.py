"""A receipt or credit note settles only documents of its own branch.

The Okafor family is one school-wide customer with a child at Lekki and a child
at Ikeja. Each branch has invoiced them 100,000 kobo, Lekki first, and the school
has raised one 100,000 invoice of its own with no branch.

The settling journal is booked to the receipt's branch while each invoice's
receivable sits on the invoice's branch. Ikeja's 60,000 receipt settling Lekki's
invoice, the oldest one open, would clear Lekki's debt out of Ikeja's books, so:

* automatic allocation of Ikeja money settles Ikeja's invoice and nothing else;
* naming another branch's invoice is refused with a 400 naming both branches,
  whoever asks, because the rule belongs to the money and not to the caller;
* a school-wide receipt settles school-wide invoices only, and a branch receipt
  never settles a school-wide invoice.

The same holds for a credit note, posted with allocation or applied later.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.constants import CreditNoteKind
from vs_finance.exceptions import SettlementBranchError
from vs_finance.models import Account, CreditNote, CreditNoteLine, Invoice, InvoiceLine, Payment

from .tests_branch_scope import _FinanceBranchFixture

INVOICE = 100_000


class _SettlementFixture(_FinanceBranchFixture):
    def setUp(self):
        from vs_finance.receivables import post_invoice

        super().setUp()
        e = self.books
        self.income = Account.objects.get(entity=e, code="4100")
        self.bank = Account.objects.get(entity=e, code="1100")

        def posted_invoice(branch, day):
            invoice = self.invoice(e, self.family, branch)
            Invoice.objects.filter(pk=invoice.pk).update(
                invoice_date=datetime.date(2026, 1, day),
                due_date=datetime.date(2026, 1, day + 10),
            )
            InvoiceLine.objects.filter(invoice=invoice).update(revenue_account=self.income)
            invoice.refresh_from_db()
            post_invoice(invoice)
            invoice.refresh_from_db()
            return invoice

        self.family = self.customer(e, "OKAFOR", None)
        self.lekki_invoice = posted_invoice(self.lekki, 3)
        self.school_invoice = posted_invoice(None, 4)
        self.ikeja_invoice = posted_invoice(self.ikeja, 5)

    def receipt(self, branch, amount=60_000, *, allocations=None, auto=True):
        from vs_finance.receivables import post_payment

        payment = Payment.objects.create(
            entity=self.books, customer=self.family, branch=branch,
            payment_date=datetime.date(2026, 1, 20), amount=amount,
            deposit_account=self.bank,
        )
        if allocations is not None:
            post_payment(payment, allocations=allocations)
        else:
            post_payment(payment, auto_allocate=auto)
        payment.refresh_from_db()
        return payment

    def credit_note(self, branch, amount=60_000, *, auto=False):
        from vs_finance.credit_notes import post_credit_note

        note = CreditNote.objects.create(
            entity=self.books, customer=self.family, branch=branch,
            kind=CreditNoteKind.CREDIT, note_date=datetime.date(2026, 1, 20),
            reason="Sibling discount",
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=Account.objects.get(entity=self.books, code="4900"),
            quantity=1, unit_price=amount, line_no=1,
        )
        post_credit_note(note, auto_allocate=auto)
        note.refresh_from_db()
        return note

    def settled(self):
        """Kobo cleared on each invoice so far, by branch."""
        out = {}
        for key, invoice in (("lekki", self.lekki_invoice), ("school", self.school_invoice),
                             ("ikeja", self.ikeja_invoice)):
            invoice.refresh_from_db()
            out[key] = invoice.amount_paid + invoice.amount_credited
        return out

    def head(self, *keys):
        user = self.grant(
            self.user_for(self.tenant, "head.bursar@example.com"), *keys,
            tenant=self.tenant, role_key="settle-head",
        )
        return TenantAPIClient(user=user)


class ReceiptAutoAllocationTests(_SettlementFixture):
    def test_a_branch_receipt_settles_its_own_branchs_invoice_only(self):
        receipt = self.receipt(self.ikeja)

        self.assertEqual(self.settled(), {"lekki": 0, "school": 0, "ikeja": 60_000})
        self.assertEqual(receipt.allocated_amount, 60_000)

    def test_money_beyond_the_branchs_invoices_parks_as_credit(self):
        receipt = self.receipt(self.ikeja, 150_000)

        self.assertEqual(self.settled(), {"lekki": 0, "school": 0, "ikeja": INVOICE})
        self.assertEqual(receipt.credit_remaining, 50_000)

    def test_a_school_wide_receipt_settles_school_wide_invoices_only(self):
        self.receipt(None)

        self.assertEqual(self.settled(), {"lekki": 0, "school": 60_000, "ikeja": 0})

    def test_stored_credit_applied_later_follows_the_same_rule(self):
        from vs_finance.receivables import allocate_payment

        receipt = self.receipt(self.ikeja, auto=False)
        allocate_payment(receipt)

        self.assertEqual(self.settled(), {"lekki": 0, "school": 0, "ikeja": 60_000})


class ReceiptExplicitAllocationTests(_SettlementFixture):
    def test_naming_another_branchs_invoice_at_posting_is_refused(self):
        with self.assertRaisesMessage(
            SettlementBranchError,
            f"This receipt belongs to Ikeja Branch and invoice "
            f"{self.lekki_invoice.document_number} belongs to Lekki Branch. "
            f"Apply it to an Ikeja Branch invoice.",
        ):
            self.receipt(self.ikeja, allocations=[(self.lekki_invoice, 60_000)])

        self.assertEqual(self.settled(), {"lekki": 0, "school": 0, "ikeja": 0})

    def test_a_branch_receipt_cannot_settle_a_school_wide_invoice(self):
        with self.assertRaisesMessage(SettlementBranchError, "is school-wide"):
            self.receipt(self.ikeja, allocations=[(self.school_invoice, 60_000)])

    def test_a_school_wide_receipt_cannot_settle_a_branch_invoice(self):
        with self.assertRaisesMessage(
            SettlementBranchError,
            f"This receipt is school-wide and invoice "
            f"{self.ikeja_invoice.document_number} belongs to Ikeja Branch. "
            f"Apply it to a school-wide invoice.",
        ):
            self.receipt(None, allocations=[(self.ikeja_invoice, 60_000)])

    def test_naming_the_receipts_own_branch_invoice_settles_it(self):
        self.receipt(self.ikeja, allocations=[(self.ikeja_invoice, 60_000)])

        self.assertEqual(self.settled()["ikeja"], 60_000)

    def test_a_whole_school_caller_is_refused_with_a_400_naming_both_branches(self):
        """The rule is the receipt's, so covering both branches does not lift it."""
        receipt = self.receipt(self.ikeja, auto=False)
        client = self.head("finance.payment.allocate")

        response = client.post(
            f"/v1/finance/payments/{receipt.pk}/allocate/?entity={self.books.code}",
            {"allocations": [{"invoice": self.lekki_invoice.document_number,
                              "amount": 60_000}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["code"], "SETTLEMENT_BRANCH")
        self.assertIn("Ikeja Branch", response.data["message"])
        self.assertIn("Lekki Branch", response.data["message"])
        self.assertEqual(self.settled(), {"lekki": 0, "school": 0, "ikeja": 0})


class CreditNoteSettlementTests(_SettlementFixture):
    def test_a_branch_note_posted_with_allocation_settles_its_own_branch_only(self):
        self.credit_note(self.ikeja, auto=True)

        self.assertEqual(self.settled(), {"lekki": 0, "school": 0, "ikeja": 60_000})

    def test_a_branch_notes_stored_credit_settles_its_own_branch_only(self):
        from vs_finance.credit_notes import allocate_credit_note

        note = self.credit_note(self.ikeja)
        allocate_credit_note(note)

        self.assertEqual(self.settled(), {"lekki": 0, "school": 0, "ikeja": 60_000})

    def test_applying_a_note_to_another_branchs_invoice_is_refused(self):
        from vs_finance.credit_notes import allocate_credit_note

        note = self.credit_note(self.ikeja)

        with self.assertRaisesMessage(
            SettlementBranchError,
            f"This credit note belongs to Ikeja Branch and invoice "
            f"{self.lekki_invoice.document_number} belongs to Lekki Branch.",
        ):
            allocate_credit_note(note, allocations=[(self.lekki_invoice, 60_000)])

        self.assertEqual(self.settled(), {"lekki": 0, "school": 0, "ikeja": 0})

    def test_the_allocate_endpoint_answers_400(self):
        note = self.credit_note(self.ikeja)
        client = self.head("finance.creditnote.allocate")

        response = client.post(
            f"/v1/finance/credit-notes/{note.pk}/allocate/?entity={self.books.code}",
            {"allocations": [{"invoice": self.lekki_invoice.document_number,
                              "amount": 60_000}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["code"], "SETTLEMENT_BRANCH")
        self.assertEqual(self.settled(), {"lekki": 0, "school": 0, "ikeja": 0})
