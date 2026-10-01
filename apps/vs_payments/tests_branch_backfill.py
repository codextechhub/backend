"""The branch backfill on the payment gateway's records.

Every gateway record carries a branch, set when it is made; rows made before
that are blank, and the backfill fills them from the facts already on them. An
online payment for an invoice belongs to the invoice's branch, which is the
first thing a collection reads.
"""
from __future__ import annotations

from vs_finance.branch_derivation import apply_plan, plan_entity
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_payments.models import CollectionIntent, PayoutBatch, PayoutInstruction, VirtualAccount


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

    def test_a_batch_takes_its_lines_branch_only_when_they_agree(self):
        """A batch whose lines left two branches' banks is flagged, whatever account it names.

        BAT-ONE pays twice from Ikeja's bank and is Ikeja's. BAT-TWO names Ikeja's
        bank but one of its lines left Lekki's, so it stays blank for an
        administrator rather than showing Lekki's payout to Ikeja's clerk.
        """
        from vs_finance.models import Account, BankAccount

        cash_type = Account.objects.get(entity=self.books, code="1000").account_type
        banks = {}
        for n, branch in enumerate((self.ikeja, self.lekki)):
            banks[branch.pk] = Account.objects.create(
                entity=self.books, code=f"119{n}", name=f"Bank {n}",
                account_type=cash_type, is_postable=True)
            BankAccount.objects.create(entity=self.books, name=f"Bank {n}", branch=branch,
                                       gl_account=banks[branch.pk])

        def batch(ref, *branches):
            row = PayoutBatch.objects.create(entity=self.books, provider="FAKE", reference=ref,
                                             source_account=banks[self.ikeja.pk])
            for n, branch in enumerate(branches):
                PayoutInstruction.objects.create(
                    entity=self.books, provider="FAKE", reference=f"{ref}-{n}", amount=1_000,
                    beneficiary_name="Ojo Stationers", beneficiary_account_number="0123456789",
                    source_account=banks[branch.pk], batch=row)
            return row

        one = batch("BAT-ONE", self.ikeja, self.ikeja)
        two = batch("BAT-TWO", self.ikeja, self.lekki)
        target = self.target(plan_entity(self.books), PayoutBatch)
        self.assertEqual(target.assign[one.pk][0], self.ikeja.pk)
        self.assertNotIn(two.pk, target.assign)
        self.assertEqual([f.pk for f in target.flags], [two.pk])
