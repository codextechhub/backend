"""The receivables guards: what money may settle, how often, and who may say so.

Corona runs Ikeja, Lekki and Yaba. The Eze and Bello families are customers at
Ikeja; each has been billed. The tests follow the owner's decisions one at a time:

* money settles only a posted bill of its own customer, under a lock, and the
  database refuses a bill settled beyond its total;
* money moves between customers only through an approved credit transfer;
* credit a customer already holds pays each new bill as it posts, and dunning does
  not chase what credit or an on-time payment plan covers;
* a credit note naming a bill settles that bill first;
* a fee run cannot bill twice, bills optional items only to those who take them,
  and stamps a period that cannot change once posted;
* a customer who leaves is billed no more and still owes what they owe;
* reductions are weighed together for approval, and a large one needs a second
  person;
* a customer's source record and receivable account are fixed once money moved;
* opening balances arrive one bill at a time, dated as the originals.
"""
from __future__ import annotations

import datetime

from django.db import IntegrityError, transaction

from core.test_utils import TenantAPIClient
from vs_finance.constants import CreditNoteKind, DocumentStatus, FinanceAuditAction
from vs_finance.exceptions import PostingError, SettlementTargetError
from vs_finance.models import (
    Account,
    Concession,
    CreditNote,
    CreditNoteLine,
    Customer,
    CustomerCreditTransfer,
    FeeItem,
    FeeItemAssignment,
    FeeStructure,
    FinanceAuditLog,
    FinanceDocumentSettings,
    Invoice,
    InvoiceLine,
    Payment,
)

from .tests_branch_scope import _FinanceBranchFixture
from .tests_payroll_split import _SplitFixture

D = datetime.date


class _ArFixture(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        e = self.books
        self.income = Account.objects.get(entity=e, code="4100")
        self.bank = Account.objects.get(entity=e, code="1100")
        self.returns = Account.objects.get(entity=e, code="4900")
        self.eze = self.customer(e, "EZE", self.ikeja)
        self.bello = self.customer(e, "BELLO", self.ikeja)
        self.bursar = self.user_for(self.tenant, "okafor@corona.test")
        self.clerk = self.user_for(self.tenant, "eze.clerk@corona.test")

    # -- documents ----------------------------------------------------------- #

    def bill(self, customer, amount=150_000, *, day=10, branch=None, post=True):
        from vs_finance.receivables import post_invoice

        invoice = Invoice.objects.create(
            entity=self.books, customer=customer, branch=branch or self.ikeja,
            invoice_date=D(2026, 1, day), due_date=D(2026, 1, day + 5),
        )
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=amount,
            revenue_account=self.income,
        )
        if post:
            post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def receipt(self, customer, amount, *, day=12, branch=None, allocations=None, auto=False):
        from vs_finance.receivables import post_payment

        payment = Payment.objects.create(
            entity=self.books, customer=customer, branch=branch or self.ikeja,
            payment_date=D(2026, 1, day), amount=amount, deposit_account=self.bank,
        )
        if allocations is not None:
            post_payment(payment, allocations=allocations)
        else:
            post_payment(payment, auto_allocate=auto)
        payment.refresh_from_db()
        return payment

    def note(self, customer, amount, *, invoice=None, day=12, post=True, auto=False, by=None):
        from vs_finance.credit_notes import post_credit_note

        note = CreditNote.objects.create(
            entity=self.books, customer=customer, branch=self.ikeja,
            kind=CreditNoteKind.CREDIT, note_date=D(2026, 1, day), invoice=invoice,
            reason="Correction", created_by=by,
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=self.returns, quantity=1, unit_price=amount, line_no=1,
        )
        if post:
            post_credit_note(note, auto_allocate=auto)
        else:
            from vs_finance.credit_notes import price_credit_note

            price_credit_note(note)
        note.refresh_from_db()
        return note

    def concession(self, invoice, amount, *, by=None, day=12):
        return Concession.objects.create(
            entity=self.books, customer=invoice.customer, invoice=invoice,
            branch=invoice.branch, concession_date=D(2026, 1, day), amount=amount,
            created_by=by,
        )

    def settings(self, **values):
        FinanceDocumentSettings.objects.update_or_create(entity=self.books, defaults=values)

    def client_with(self, *keys, email="bursar.api@corona.test"):
        user = self.grant(self.user_for(self.tenant, email), *keys,
                          tenant=self.tenant, role_key=f"ar-{email}")
        return TenantAPIClient(user=user)


# --------------------------------------------------------------------------- #
# Settlement targets                                                           #
# --------------------------------------------------------------------------- #

class SettlementTargetTests(_ArFixture):
    """Money settles only a posted bill of its own customer."""

    def test_a_receipt_cannot_settle_a_voided_invoice(self):
        from vs_finance.voids import void_invoice

        bill = self.bill(self.eze)
        void_invoice(bill)

        with self.assertRaisesMessage(SettlementTargetError, "has been voided"):
            self.receipt(self.eze, 150_000, allocations=[(bill, 150_000)])

    def test_a_receipt_cannot_settle_another_customers_invoice(self):
        bellos = self.bill(self.bello)

        with self.assertRaisesMessage(SettlementTargetError, "belongs to BELLO, not to EZE"):
            self.receipt(self.eze, 150_000, allocations=[(bellos, 150_000)])
        bellos.refresh_from_db()
        self.assertEqual(bellos.amount_paid, 0)

    def test_stored_credit_cannot_be_applied_to_another_customers_invoice(self):
        from vs_finance.receivables import allocate_payment

        self.settings(auto_apply_customer_credit=False)
        bellos = self.bill(self.bello)
        receipt = self.receipt(self.eze, 150_000)

        with self.assertRaises(SettlementTargetError):
            allocate_payment(receipt, allocations=[(bellos, 150_000)])

    def test_a_credit_note_cannot_settle_another_customers_invoice(self):
        from vs_finance.credit_notes import allocate_credit_note

        self.settings(auto_apply_customer_credit=False)
        bellos = self.bill(self.bello)
        note = self.note(self.eze, 50_000)

        with self.assertRaises(SettlementTargetError):
            allocate_credit_note(note, allocations=[(bellos, 50_000)])

    def test_a_concession_on_another_customers_invoice_is_refused(self):
        from vs_finance.installments import post_concession

        bellos = self.bill(self.bello)
        concession = Concession.objects.create(
            entity=self.books, customer=self.eze, invoice=bellos, branch=self.ikeja,
            concession_date=D(2026, 1, 12), amount=10_000,
        )
        with self.assertRaises(SettlementTargetError):
            post_concession(concession)

    def test_a_concession_posts_once_however_often_it_is_asked(self):
        from vs_finance.installments import post_concession

        bill = self.bill(self.eze)
        concession = self.concession(bill, 10_000)
        post_concession(concession)
        with self.assertRaisesMessage(PostingError, "only a draft concession"):
            post_concession(concession)
        bill.refresh_from_db()
        self.assertEqual(bill.amount_credited, 10_000)

    def test_a_write_off_request_posts_once(self):
        from vs_finance.credit_notes import post_write_off_request
        from vs_finance.models import WriteOffRequest

        bill = self.bill(self.eze)
        request = WriteOffRequest.objects.create(
            entity=self.books, invoice=bill, branch=self.ikeja, amount=20_000,
            write_off_date=D(2026, 1, 20),
        )
        post_write_off_request(request)
        with self.assertRaisesMessage(PostingError, "only a draft or approved"):
            post_write_off_request(request)
        bill.refresh_from_db()
        self.assertEqual(bill.amount_credited, 20_000)

    def test_a_write_off_to_a_bank_account_is_refused(self):
        from vs_finance.credit_notes import write_off_invoice

        bill = self.bill(self.eze)
        with self.assertRaisesMessage(PostingError, "cannot be used here"):
            write_off_invoice(bill, write_off_account=self.bank, write_off_date=D(2026, 1, 20))


class SettlementConstraintTests(_ArFixture):
    """A settlement path that misses its lock fails instead of clearing a bill twice."""

    def test_an_invoice_cannot_be_settled_beyond_its_total(self):
        bill = self.bill(self.eze, 100_000)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Invoice.objects.filter(pk=bill.pk).update(amount_paid=60_000, amount_credited=50_000)

    def test_a_receipt_cannot_spend_more_than_it_brought(self):
        receipt = self.receipt(self.eze, 50_000)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.filter(pk=receipt.pk).update(allocated_amount=40_000, refunded_amount=20_000)


# --------------------------------------------------------------------------- #
# Customer credit transfers                                                    #
# --------------------------------------------------------------------------- #

class CreditTransferTests(_ArFixture):
    """Mrs Eze's overpayment pays the Bello bill only through an approved transfer."""

    def setUp(self):
        super().setUp()
        self.bellos = self.bill(self.bello, 100_000)
        self.overpaid = self.receipt(self.eze, 120_000, day=11)

    def transfer(self, amount=100_000, status=DocumentStatus.APPROVED):
        return CustomerCreditTransfer.objects.create(
            entity=self.books, branch=self.ikeja, from_customer=self.eze,
            to_customer=self.bello, transfer_date=D(2026, 1, 15), amount=amount,
            status=status, created_by=self.bursar,
        )

    def test_an_approved_transfer_moves_credit_and_pays_the_destinations_bill(self):
        from vs_finance.credit_transfers import post_customer_credit_transfer
        from vs_finance.receivables import customer_credit_balance

        transfer = post_customer_credit_transfer(self.transfer(), actor_user=self.bursar)

        self.overpaid.refresh_from_db()
        self.bellos.refresh_from_db()
        self.assertEqual(transfer.status, DocumentStatus.POSTED)
        self.assertEqual(self.overpaid.transferred_amount, 100_000)
        self.assertEqual(customer_credit_balance(self.eze), 20_000)
        self.assertEqual(self.bellos.balance_due, 0)
        self.assertEqual(transfer.receipt.method, "CREDIT_TRANSFER")
        self.assertTrue(FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.CREDIT_TRANSFER_POSTED, target_id=str(transfer.pk)).exists())

    def test_a_transfer_journal_moves_no_money_through_the_bank(self):
        from vs_finance.credit_transfers import post_customer_credit_transfer

        transfer = post_customer_credit_transfer(self.transfer())
        accounts = set(transfer.receipt.journal.lines.values_list("account__code", flat=True))
        self.assertNotIn("1100", accounts)

    def test_only_an_approved_transfer_posts(self):
        from vs_finance.credit_transfers import post_customer_credit_transfer

        with self.assertRaisesMessage(PostingError, "only an approved transfer"):
            post_customer_credit_transfer(self.transfer(status=DocumentStatus.DRAFT))

    def test_a_transfer_beyond_the_sources_credit_is_refused(self):
        from vs_finance.credit_transfers import post_customer_credit_transfer

        with self.assertRaisesMessage(PostingError, "less than"):
            post_customer_credit_transfer(self.transfer(amount=150_000))

    def test_a_transfer_is_submitted_not_posted(self):
        from vs_finance.approvals import ensure_tenant_approval_templates
        from vs_workflow.services.submission import submit_for_approval

        ensure_tenant_approval_templates(self.tenant)
        transfer = self.transfer(status=DocumentStatus.DRAFT)
        submit_for_approval(transfer, requested_by=self.bursar)

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, DocumentStatus.PENDING_APPROVAL)
        self.assertIsNone(transfer.receipt_id)

    def test_voiding_the_transfer_gives_the_credit_back(self):
        from vs_finance.credit_transfers import (
            post_customer_credit_transfer, void_customer_credit_transfer,
        )

        transfer = post_customer_credit_transfer(self.transfer())
        void_customer_credit_transfer(transfer)

        self.overpaid.refresh_from_db()
        self.bellos.refresh_from_db()
        self.assertEqual(self.overpaid.transferred_amount, 0)
        self.assertEqual(self.bellos.balance_due, 100_000)

    def test_the_source_receipt_cannot_be_voided_while_the_transfer_stands(self):
        from vs_finance.credit_transfers import post_customer_credit_transfer
        from vs_finance.voids import void_payment

        post_customer_credit_transfer(self.transfer())
        with self.assertRaisesMessage(PostingError, "void that transfer first"):
            void_payment(self.overpaid)

    def test_the_transfer_receipt_is_voided_only_through_its_transfer(self):
        from vs_finance.credit_transfers import post_customer_credit_transfer
        from vs_finance.voids import void_payment

        transfer = post_customer_credit_transfer(self.transfer())
        with self.assertRaisesMessage(PostingError, "void the transfer instead"):
            void_payment(transfer.receipt)

    def test_the_api_creates_a_draft_and_has_no_post_route(self):
        client = self.client_with("finance.credittransfer.create", "finance.credittransfer.view")
        response = client.post(
            f"/v1/finance/credit-transfers/?entity={self.books.code}",
            {"from_customer": "EZE", "to_customer": "BELLO", "amount": 100_000,
             "transfer_date": "2026-01-15"}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["status"], DocumentStatus.DRAFT)
        missing = client.post(
            f"/v1/finance/credit-transfers/{response.data['data']['id']}/post/"
            f"?entity={self.books.code}", {}, format="json",
        )
        self.assertEqual(missing.status_code, 404)

    def test_a_caller_without_the_key_cannot_raise_one(self):
        client = self.client_with("finance.credittransfer.view", email="viewer@corona.test")
        response = client.post(
            f"/v1/finance/credit-transfers/?entity={self.books.code}",
            {"from_customer": "EZE", "to_customer": "BELLO", "amount": 1_000,
             "transfer_date": "2026-01-15"}, format="json",
        )
        self.assertEqual(response.status_code, 403)


# --------------------------------------------------------------------------- #
# Credit pays new bills                                                        #
# --------------------------------------------------------------------------- #

class CreditAutoApplyTests(_ArFixture):
    """Mr Chukwu paid in August; September's bill is paid when it is raised."""

    def test_an_advance_payment_pays_the_next_bill_as_it_posts(self):
        advance = self.receipt(self.eze, 150_000, day=3)
        bill = self.bill(self.eze, 150_000, day=10)

        advance.refresh_from_db()
        self.assertEqual(bill.balance_due, 0)
        self.assertEqual(advance.credit_remaining, 0)

    def test_oldest_credit_is_used_first(self):
        first = self.receipt(self.eze, 50_000, day=2)
        second = self.receipt(self.eze, 50_000, day=4)
        self.bill(self.eze, 70_000, day=10)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.credit_remaining, second.credit_remaining), (0, 30_000))

    def test_an_unapplied_credit_note_pays_the_next_bill(self):
        self.note(self.eze, 40_000, day=5)
        bill = self.bill(self.eze, 100_000, day=10)
        self.assertEqual(bill.balance_due, 60_000)

    def test_the_entity_can_keep_credit_on_account(self):
        self.settings(auto_apply_customer_credit=False)
        self.receipt(self.eze, 150_000, day=3)
        bill = self.bill(self.eze, 150_000, day=10)
        self.assertEqual(bill.balance_due, 150_000)

    def test_credit_of_another_branch_is_not_used(self):
        self.receipt(self.eze, 150_000, day=3, branch=self.lekki)
        bill = self.bill(self.eze, 150_000, day=10)
        self.assertEqual(bill.balance_due, 150_000)

    def test_one_branch_books_use_credit_not_yet_given_a_branch(self):
        from vs_finance.receivables import post_invoice, post_payment

        e = self.solo_books
        family = self.customer(e, "SOLO", None)
        receipt = Payment.objects.create(
            entity=e, customer=family, branch=None, payment_date=D(2026, 1, 3),
            amount=80_000, deposit_account=Account.objects.get(entity=e, code="1100"),
        )
        post_payment(receipt, auto_allocate=False)
        invoice = Invoice.objects.create(
            entity=e, customer=family, branch=self.solo_main,
            invoice_date=D(2026, 1, 10), due_date=D(2026, 1, 20),
        )
        InvoiceLine.objects.create(invoice=invoice, line_no=1, quantity=1, unit_price=80_000,
                                   revenue_account=Account.objects.get(entity=e, code="4100"))
        post_invoice(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.balance_due, 0)


class DunningCoverTests(_ArFixture):
    """Nobody is chased for what their credit, or an on-time plan, covers."""

    def chased(self, as_of=D(2026, 1, 30)):
        from vs_finance.dunning import ensure_default_policy, generate_dunning

        ensure_default_policy(self.books)
        return {n.invoice_id for n in generate_dunning(self.books, as_of=as_of)}

    def test_an_invoice_covered_by_credit_is_not_chased(self):
        self.settings(auto_apply_customer_credit=False)
        self.receipt(self.eze, 150_000, day=3)
        covered = self.bill(self.eze, 150_000, day=5)
        owing = self.bill(self.bello, 150_000, day=5)

        chased = self.chased()
        self.assertNotIn(covered.pk, chased)
        self.assertIn(owing.pk, chased)

    def test_an_invoice_on_an_on_time_plan_is_not_chased(self):
        from vs_finance.installments import activate_payment_plan, build_installments
        from vs_finance.models import PaymentPlan

        bill = self.bill(self.eze, 150_000, day=5)
        plan = PaymentPlan.objects.create(
            entity=self.books, customer=self.eze, invoice=bill, branch=self.ikeja,
            start_date=D(2026, 2, 1), installment_count=3, total_amount=150_000,
        )
        build_installments(plan)
        activate_payment_plan(plan)

        self.assertNotIn(bill.pk, self.chased())

    def test_an_invoice_with_an_overdue_instalment_is_chased(self):
        from vs_finance.installments import activate_payment_plan, build_installments
        from vs_finance.models import PaymentPlan

        bill = self.bill(self.eze, 150_000, day=5)
        plan = PaymentPlan.objects.create(
            entity=self.books, customer=self.eze, invoice=bill, branch=self.ikeja,
            start_date=D(2026, 1, 15), installment_count=3, total_amount=150_000,
        )
        build_installments(plan)
        activate_payment_plan(plan)

        self.assertIn(bill.pk, self.chased())


# --------------------------------------------------------------------------- #
# Credit notes naming a bill                                                   #
# --------------------------------------------------------------------------- #

class CreditNoteNamedBillTests(_ArFixture):
    """Kemi's N80,000 correction clears INV-0450, not her oldest arrear."""

    def test_a_note_naming_a_bill_settles_that_bill_first(self):
        older = self.bill(self.eze, 100_000, day=3)
        named = self.bill(self.eze, 100_000, day=8)
        self.note(self.eze, 80_000, invoice=named, auto=True)

        older.refresh_from_db()
        named.refresh_from_db()
        self.assertEqual((older.amount_credited, named.amount_credited), (0, 80_000))

    def test_the_remainder_stays_as_customer_credit(self):
        named = self.bill(self.eze, 50_000, day=8)
        note = self.note(self.eze, 80_000, invoice=named)

        named.refresh_from_db()
        self.assertEqual(named.balance_due, 0)
        self.assertEqual(note.credit_remaining, 30_000)

    def test_an_approved_note_settles_the_named_bill(self):
        from vs_finance.workflow_handlers import CreditNoteHandler

        named = self.bill(self.eze, 100_000, day=8)
        note = self.note(self.eze, 80_000, invoice=named, post=False)
        CreditNote.objects.filter(pk=note.pk).update(status=DocumentStatus.APPROVED)
        note.refresh_from_db()
        handler = CreditNoteHandler()
        handler._mark_approved(note)
        handler.post(note, actor_user=self.bursar)

        named.refresh_from_db()
        self.assertEqual(named.amount_credited, 80_000)

    def test_a_note_naming_another_customers_bill_is_refused_at_create(self):
        bellos = self.bill(self.bello)
        client = self.client_with("finance.creditnote.create")
        response = client.post(
            f"/v1/finance/credit-notes/?entity={self.books.code}",
            {"customer": "EZE", "invoice": bellos.pk, "note_date": "2026-01-12",
             "lines": [{"revenue_account": "4900", "unit_price": 10_000}]}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)


# --------------------------------------------------------------------------- #
# Fee runs                                                                     #
# --------------------------------------------------------------------------- #

class FeeRunTests(_ArFixture):
    def setUp(self):
        super().setUp()
        self.structure = FeeStructure.objects.create(
            entity=self.books, code="YEAR1-FEES", name="Year 1 Fees")
        FeeItem.objects.create(structure=self.structure, line_no=1, description="Year 1 fees",
                               revenue_account=self.income, amount=150_000)
        self.bus = FeeItem.objects.create(
            structure=self.structure, line_no=2, description="Bus route",
            revenue_account=self.income, amount=45_000, is_optional=True)

    def bill_run(self, customers, **kwargs):
        from vs_finance.fees import generate_invoices

        return generate_invoices(self.structure, customers, invoice_date=D(2026, 1, 10),
                                 due_date=D(2026, 1, 25), **kwargs)

    def test_a_second_run_bills_nobody_twice(self):
        self.bill_run([self.eze, self.bello])
        self.assertEqual(self.bill_run([self.eze, self.bello]), [])
        self.assertEqual(Invoice.objects.filter(reference="FEE:YEAR1-FEES").count(), 2)

    def test_the_database_refuses_a_second_live_bill_for_one_key(self):
        self.bill_run([self.eze])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Invoice.objects.create(
                entity=self.books, customer=self.eze, branch=self.ikeja,
                invoice_date=D(2026, 1, 11), billing_key="FEE:YEAR1-FEES",
            )

    def test_an_optional_item_is_billed_only_to_those_who_take_it(self):
        FeeItemAssignment.objects.create(item=self.bus, customer=self.eze)
        by_customer = {inv.customer_id: inv.total for inv in self.bill_run([self.eze, self.bello])}
        self.assertEqual(by_customer, {self.eze.pk: 195_000, self.bello.pk: 150_000})

    def test_an_inactive_customer_is_not_billed(self):
        from vs_finance.customers import set_customer_active

        set_customer_active(self.bello, False)
        billed = {inv.customer_id for inv in self.bill_run([self.eze, self.bello])}
        self.assertEqual(billed, {self.eze.pk})

    def test_each_period_bills_once(self):
        self.bill_run([self.eze], billing_period="S1-T1", billing_period_label="First Term")
        self.assertEqual(self.bill_run([self.eze], billing_period="S1-T1"), [])
        second = self.bill_run([self.eze], billing_period="S1-T2", billing_period_label="Second Term")
        self.assertEqual(len(second), 1)

    def test_a_run_naming_no_period_does_not_rebill_a_period_run(self):
        self.bill_run([self.eze], billing_period="S1-T1")
        self.assertEqual(self.bill_run([self.eze]), [])

    def test_the_period_of_a_posted_invoice_cannot_change(self):
        invoice = self.bill_run([self.eze], billing_period="S1-T1", billing_period_label="First Term")[0]
        invoice.billing_period = "S1-T2"
        with self.assertRaisesMessage(PostingError, "cannot be changed"):
            invoice.save()

    def test_bill_all_active_leaves_inactive_customers_out(self):
        from vs_finance.customers import set_customer_active

        set_customer_active(self.bello, False)
        client = self.client_with("finance.feestructure.generate")
        response = client.post(
            f"/v1/finance/fee-structures/YEAR1-FEES/generate/?entity={self.books.code}",
            {"all_active": True, "invoice_date": "2026-01-10", "due_date": "2026-01-25"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        codes = {row["customer_code"] for row in response.data["data"]["invoices"]}
        self.assertEqual(codes, {"EZE"})

    def test_optional_items_are_assigned_over_the_api(self):
        client = self.client_with("finance.feestructure.edit", "finance.feestructure.view")
        path = (f"/v1/finance/fee-structures/YEAR1-FEES/items/{self.bus.pk}/assignments/"
                f"?entity={self.books.code}")
        response = client.post(path, {"customers": ["EZE"]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([c["code"] for c in response.data["data"]["customers"]], ["EZE"])


# --------------------------------------------------------------------------- #
# Deactivation                                                                 #
# --------------------------------------------------------------------------- #

class DeactivationTests(_ArFixture):
    def test_a_deactivated_customer_keeps_their_debt(self):
        from vs_finance.customers import set_customer_active

        bill = self.bill(self.eze)
        set_customer_active(self.eze, False, actor_user=self.bursar)

        bill.refresh_from_db()
        self.eze.refresh_from_db()
        self.assertFalse(self.eze.is_active)
        self.assertEqual(bill.balance_due, 150_000)
        self.assertTrue(FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.CUSTOMER_DEACTIVATED, target_id=str(self.eze.pk)).exists())


# --------------------------------------------------------------------------- #
# Cumulative approval and the second person                                    #
# --------------------------------------------------------------------------- #

class CumulativeApprovalTests(_ArFixture):
    """Mr Eze's three N49,000 discounts meet the step one N147,000 waiver would."""

    def setUp(self):
        from vs_finance.approvals import ensure_tenant_approval_templates

        super().setUp()
        ensure_tenant_approval_templates(self.tenant)  # Steps at N50,000.
        self.bill_ = self.bill(self.eze, 15_000_000)

    def test_small_concessions_are_weighed_together(self):
        from vs_finance.approvals import approval_required
        from vs_finance.installments import post_concession

        first = self.concession(self.bill_, 4_900_000, by=self.clerk)
        self.assertFalse(approval_required(first))
        post_concession(first, actor_user=self.bursar)
        second = self.concession(self.bill_, 4_900_000, by=self.clerk)
        self.assertEqual(second.cumulative_amount, 9_800_000)
        self.assertTrue(approval_required(second))

    def test_credit_notes_and_concessions_share_the_running_total(self):
        from vs_finance.approvals import approval_required
        from vs_finance.installments import post_concession

        post_concession(self.concession(self.bill_, 3_000_000, by=self.clerk), actor_user=self.bursar)
        note = self.note(self.eze, 3_000_000, invoice=self.bill_, post=False)
        self.assertEqual(note.cumulative_amount, 6_000_000)
        self.assertTrue(approval_required(note))

    def test_a_voided_concession_no_longer_counts(self):
        from vs_finance.installments import post_concession
        from vs_finance.voids import void_concession

        first = self.concession(self.bill_, 4_000_000, by=self.clerk)
        post_concession(first, actor_user=self.bursar)
        void_concession(first)
        self.assertEqual(self.concession(self.bill_, 1_000).cumulative_amount, 1_000)

    def test_the_author_of_a_large_concession_cannot_post_it(self):
        from vs_finance.installments import post_concession

        concession = self.concession(self.bill_, 1_500_000, by=self.clerk)
        with self.assertRaisesMessage(PostingError, "Ask a colleague"):
            post_concession(concession, actor_user=self.clerk)
        post_concession(concession, actor_user=self.bursar)

    def test_a_small_concession_posts_in_one_step(self):
        from vs_finance.installments import post_concession

        concession = self.concession(self.bill_, 900_000, by=self.clerk)
        post_concession(concession, actor_user=self.clerk)
        concession.refresh_from_db()
        self.assertEqual(concession.status, DocumentStatus.POSTED)

    def test_the_second_person_limit_is_cumulative_and_configurable(self):
        from vs_finance.installments import post_concession

        post_concession(self.concession(self.bill_, 900_000, by=self.clerk), actor_user=self.clerk)
        second = self.concession(self.bill_, 200_000, by=self.clerk)
        with self.assertRaises(PostingError):
            post_concession(second, actor_user=self.clerk)
        self.settings(concession_second_person_threshold=2_000_000)
        post_concession(second, actor_user=self.clerk)

    def test_seeded_steps_weigh_the_running_total(self):
        from vs_workflow.models import WorkflowStage

        fields = set(
            WorkflowStage.objects.filter(
                template__tenant=self.tenant,
                template__document_type__in=("finance.concession", "finance.credit_note"),
            ).values_list("inclusion_condition__field", flat=True)
        )
        self.assertEqual(fields, {"cumulative_amount"})

    def test_steps_seeded_before_weigh_the_running_total_after_the_migration(self):
        import importlib

        migration = importlib.import_module("vs_finance.migrations.0041_ar_guards_data")
        before = {"all": [{"op": "gte", "field": "amount", "value": 5_000_000},
                          {"op": "eq", "field": "kind", "value": "WAIVER"}]}
        after = migration._rewrite(before, "amount", "cumulative_amount")
        self.assertEqual(after["all"][0]["field"], "cumulative_amount")
        self.assertEqual(after["all"][1]["field"], "kind")


# --------------------------------------------------------------------------- #
# The customer master record                                                   #
# --------------------------------------------------------------------------- #

class CustomerMasterTests(_ArFixture):
    """Ada's arrears cannot be moved to another child's account by a mistyped number."""

    def setUp(self):
        super().setUp()
        Customer.objects.filter(pk=self.eze.pk).update(source_type="app.Record", source_id="5121")
        self.client_ = self.client_with("finance.customer.update", "finance.customer.view",
                                        "finance.customer.create")
        self.path = f"/v1/finance/customers/EZE/?entity={self.books.code}"

    def test_the_source_record_is_fixed_once_money_has_moved(self):
        self.bill(self.eze)
        response = self.client_.patch(self.path, {"source_id": "5123"}, format="json")
        self.assertEqual(response.status_code, 422, response.data)
        self.eze.refresh_from_db()
        self.assertEqual(self.eze.source_id, "5121")

    def test_the_source_record_may_be_corrected_before_any_activity(self):
        response = self.client_.patch(self.path, {"source_id": "5123"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.CUSTOMER_UPDATED, target_id=str(self.eze.pk)).exists())

    def test_two_accounts_cannot_claim_one_record(self):
        response = self.client_.patch(
            f"/v1/finance/customers/BELLO/?entity={self.books.code}",
            {"source_type": "app.Record", "source_id": "5121"}, format="json",
        )
        self.assertEqual(response.status_code, 422, response.data)

    def test_a_bank_account_is_not_a_receivable_account(self):
        response = self.client_.patch(self.path, {"receivable_account": "1100"}, format="json")
        self.assertEqual(response.status_code, 422, response.data)

    def test_the_receivable_account_is_fixed_once_money_has_moved(self):
        self.bill(self.eze)
        other = Account.objects.create(
            entity=self.books, code="1210", name="Other receivables",
            account_type="ASSET", normal_balance="DEBIT", is_postable=True,
        )
        response = self.client_.patch(self.path, {"receivable_account": other.code}, format="json")
        self.assertEqual(response.status_code, 422, response.data)

    def test_the_opening_balance_cannot_be_changed_after_creation(self):
        response = self.client_.patch(self.path, {"opening_balance": 250_000}, format="json")
        self.assertEqual(response.status_code, 400, response.data)


# --------------------------------------------------------------------------- #
# Opening balances                                                             #
# --------------------------------------------------------------------------- #

class OpeningInvoiceImportTests(_ArFixture):
    """Corona's 2022 arrears age as 2022 arrears."""

    def setUp(self):
        super().setUp()
        self.client_ = self.client_with("finance.customer.import_opening")
        self.path = f"/v1/finance/customers/opening/?entity={self.books.code}"

    def test_each_unpaid_bill_arrives_dated_as_the_original(self):
        response = self.client_.post(self.path, {"invoices": [
            {"customer": "EZE", "invoice_date": "2022-09-10", "due_date": "2022-10-01",
             "amount": 400_000, "period_label": "First Term 2022/2023"},
            {"customer": "EZE", "invoice_date": "2023-01-10", "amount": 300_000},
        ]}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        invoices = Invoice.objects.filter(customer=self.eze, source="OPENING").order_by("invoice_date")
        self.assertEqual([(i.invoice_date, i.due_date) for i in invoices],
                         [(D(2022, 9, 10), D(2022, 10, 1)), (D(2023, 1, 10), D(2023, 1, 10))])
        journal = invoices[0].journal
        self.assertEqual(journal.source, "OPENING")
        self.assertEqual(journal.date, D(2026, 1, 1))
        credited = journal.lines.get(credit__gt=0).account
        self.assertEqual(credited.account_type, "EQUITY")
        self.assertEqual(invoices[0].branch_id, self.ikeja.pk)

    def test_a_bill_dated_after_the_books_went_live_is_refused(self):
        self.bill(self.bello, day=10)
        response = self.client_.post(self.path, {"invoices": [
            {"customer": "EZE", "invoice_date": "2026-01-12", "amount": 10_000},
        ]}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(Invoice.objects.filter(customer=self.eze, source="OPENING").exists())

    def test_the_import_needs_its_own_key(self):
        client = self.client_with("finance.customer.create", email="clerk.api@corona.test")
        response = client.post(self.path, {"invoices": [
            {"customer": "EZE", "invoice_date": "2022-09-10", "amount": 10_000}]}, format="json")
        self.assertEqual(response.status_code, 403)


# --------------------------------------------------------------------------- #
# Collections, gateway parking and payroll journals                           #
# --------------------------------------------------------------------------- #

class CollectionsDefinitionTests(_ArFixture):
    def test_billed_is_net_of_reductions_and_collected_is_what_was_applied(self):
        from vs_finance.collected import billed_and_collected
        from vs_finance.installments import post_concession

        bill = self.bill(self.eze, 300_000)
        post_concession(self.concession(bill, 60_000))
        self.receipt(self.eze, 500_000, allocations=[(bill, 500_000)])

        self.assertEqual(billed_and_collected(Invoice.objects.filter(pk=bill.pk)),
                         (240_000, 240_000))


class GatewayParkingTests(_ArFixture):
    def test_a_voided_invoice_is_not_paid_by_a_late_confirmation(self):
        from types import SimpleNamespace

        from vs_finance.voids import void_invoice
        from vs_payments.services import _unsettlable_reason

        bill = self.bill(self.eze)
        void_invoice(bill)
        intent = SimpleNamespace(invoice_id=bill.pk, invoice=bill, customer_id=self.eze.pk)
        self.assertIn("can no longer be paid", _unsettlable_reason(intent))

    def test_another_customers_invoice_is_not_paid(self):
        from types import SimpleNamespace

        from vs_payments.services import _unsettlable_reason

        bill = self.bill(self.bello)
        intent = SimpleNamespace(invoice_id=bill.pk, invoice=bill, customer_id=self.eze.pk)
        self.assertIn("another customer", _unsettlable_reason(intent))


class PayrollShareJournalTests(_SplitFixture):
    """A branch's payroll journal is reversed by cancelling its run, and says so."""

    def test_a_hand_reversal_names_the_run_and_its_cancel_route(self):
        from vs_finance.posting import reverse_journal

        run = self.posted_run()
        share = run.branch_shares.first()
        with self.assertRaisesMessage(PostingError, f"PayrollRun {run.document_number}"):
            reverse_journal(share.journal)
        with self.assertRaisesMessage(PostingError, f"/finance/payroll-runs/{run.pk}/cancel/"):
            reverse_journal(share.journal)
