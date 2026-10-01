"""The branch backfill on the payment gateway's records.

Every gateway record carries a branch, set when it is made; rows made before
that are blank, and the backfill fills them from the facts already on them. An
online payment for an invoice belongs to the invoice's branch, which is the
first thing a collection reads.
"""
from __future__ import annotations

from vs_finance.branch_derivation import apply_plan, plan_entity
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_payments.models import CollectionIntent, VirtualAccount


class PaymentsBackfillReportTests(_FinanceBranchFixture):
    def target(self, plan, model):
        return next(p for p in plan.targets if p.target.model_label == model._meta.label)

    def test_a_collection_reads_its_invoice_before_its_customer(self):
        customer = self.customer(self.books, "C1", self.yaba)
        invoice = self.invoice(self.books, customer, self.lekki)
        intent = CollectionIntent.objects.create(
            entity=self.books, provider="FAKE", reference="CX-1", customer=customer, invoice=invoice,
        )
        by_customer = CollectionIntent.objects.create(
            entity=self.books, provider="FAKE", reference="CX-2", customer=customer,
        )
        plan = plan_entity(self.books)
        target = self.target(plan, CollectionIntent)
        self.assertTrue(target.target.has_branch_column)
        self.assertEqual(target.blank, 2)
        self.assertEqual(target.assign[intent.pk], (self.lekki.pk, "the invoice"))
        self.assertEqual(target.assign[by_customer.pk], (self.yaba.pk, "the customer"))

    def test_a_virtual_account_without_a_placed_customer_needs_an_administrator(self):
        account = VirtualAccount.objects.create(
            entity=self.books, provider="FAKE", account_number="0123456789",
            customer=self.customer(self.books, "C1", None),
        )
        target = self.target(plan_entity(self.books), VirtualAccount)
        self.assertEqual([f.pk for f in target.flags], [account.pk])
        self.assertEqual(target.flags[0].reason, "no branch on the customer")

    def test_a_blank_collection_is_given_its_derived_branch(self):
        customer = self.customer(self.books, "C1", self.lekki)
        intent = CollectionIntent.objects.create(
            entity=self.books, provider="FAKE", reference="CX-1", customer=customer)
        result = apply_plan(plan_entity(self.books))
        self.assertEqual(result.written["vs_payments.CollectionIntent"], 1)
        intent.refresh_from_db()
        self.assertEqual(intent.branch_id, self.lekki.pk)
