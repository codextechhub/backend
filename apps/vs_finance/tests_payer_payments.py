"""One payer, one payment, several customers' bills.

Mr Okafor pays for three children at Corona: Ada (N180,000, billed 5 January)
and Emeka (N150,000, billed 8 January) at Ikeja, and Chidi (N120,000, billed 10
January) at Lekki. He sends one transfer into Ikeja's bank. Ada's and Emeka's
shares become Ikeja receipts settling their own Ikeja bills; Chidi's is held at
Ikeja for Lekki and pays his Lekki bill once it is forwarded there. No receipt
settles another customer's bill, and no branch's receipt settles another
branch's.

When N400,000 is not enough for everything, the books' setting decides how it is
shared: oldest bill first across all three (the default), in proportion to what
each owes, or as the bursar enters it; the bursar may always type the amounts.
When N500,000 is more than everything, the rest is credit on the customer with
the newest bill (Chidi, at Lekki), or on Mr Okafor's own account.

Single Site has one branch, where nothing is ever held and the payment is only
receipts.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient

from .constants import (
    DocumentStatus,
    FinanceAuditAction,
    PayerPaymentSplit,
    PayerPaymentSurplus,
)
from .exceptions import InterBranchError, PayerPaymentError, PeriodClosedError, PostingError
from .inter_branch import (
    forward_held_receipt,
    post_inter_branch_transfer,
    split_by_weight,
    void_held_receipt,
    void_inter_branch_transfer,
)
from .models import (
    Account,
    Customer,
    FeeItem,
    FeeStructure,
    FinanceAuditLog,
    FinanceReceivablesPolicy,
    HeldForBranchReceipt,
    Invoice,
    InvoiceLine,
    Payment,
    PayerLink,
    PayerPayment,
)
from .payer_payments import link_customer, plan_payer_payment, record_payer_payment, void_payer_payment
from .receivables import post_invoice
from .receivables_policy import update_receivables_policy
from .tests_inter_branch import _InterBranchFixture, net
from .voids import void_payment

JAN_15 = datetime.date(2026, 1, 15)
FEB_10 = datetime.date(2026, 2, 10)

KEYS = (
    "finance.payment.view", "finance.payment.create", "finance.payment.reverse",
    "finance.customer.view", "finance.customer.update", "finance.interbranch.transfer",
)


class _PayerFixture(_InterBranchFixture):
    """Mr Okafor and his three children, and Mrs Bello at Single Site."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.okafor = cls.account(cls.books, "OKAFOR", "Mr Okafor", None)
        cls.ada = cls.account(cls.books, "ADA", "Ada Okafor", cls.ikeja)
        cls.emeka = cls.account(cls.books, "EMEKA", "Emeka Okafor", cls.ikeja)
        cls.chidi = cls.account(cls.books, "CHIDI", "Chidi Okafor", cls.lekki)
        for child in (cls.ada, cls.emeka, cls.chidi):
            link_customer(cls.okafor, child)
        cls.ada_bill = cls.bill(cls.books, cls.ada, cls.ikeja, 180_000_00, 5)
        cls.emeka_bill = cls.bill(cls.books, cls.emeka, cls.ikeja, 150_000_00, 8)
        cls.chidi_bill = cls.bill(cls.books, cls.chidi, cls.lekki, 120_000_00, 10)

    @classmethod
    def account(cls, books, code, name, branch):
        return Customer.objects.create(
            entity=books, code=code, name=name, branch=branch,
            receivable_account=Account.objects.get(entity=books, code="1200"),
        )

    @classmethod
    def bill(cls, books, customer, branch, amount, day):
        invoice = Invoice.objects.create(
            entity=books, customer=customer, branch=branch,
            invoice_date=datetime.date(2026, 1, day), due_date=datetime.date(2026, 1, day),
        )
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=amount,
            revenue_account=Account.objects.get(entity=books, code="4100"),
        )
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def pay(self, amount, **extra):
        return record_payer_payment(
            self.okafor, bank_account=self.ikeja_bank, amount=amount, payment_date=JAN_15, **extra,
        )

    def owing(self, invoice):
        invoice.refresh_from_db()
        return invoice.balance_due

    def shares(self, document):
        return {
            (s.customer.code, s.branch_id): (int(s.amount), int(s.surplus), "RECEIPT" if s.receipt_id else "HELD")
            for s in document.shares.select_related("customer")
        }

    def set_policy(self, **values):
        FinanceReceivablesPolicy.objects.update_or_create(entity=self.books, defaults=values)


class SplitAcrossBranchesTests(_PayerFixture):
    """N450,000 into Ikeja's bank covers all three bills."""

    def test_each_child_gets_their_own_receipt_and_lekkis_share_is_held_for_lekki(self):
        document = self.pay(450_000_00)

        self.assertEqual((document.status, document.branch_id), (DocumentStatus.POSTED, self.ikeja.pk))
        self.assertEqual(self.shares(document), {
            ("ADA", self.ikeja.pk): (180_000_00, 0, "RECEIPT"),
            ("EMEKA", self.ikeja.pk): (150_000_00, 0, "RECEIPT"),
            ("CHIDI", self.lekki.pk): (120_000_00, 0, "HELD"),
        })
        for share in document.shares.filter(receipt__isnull=False):
            self.assertEqual(share.receipt.customer_id, share.customer_id)
            self.assertEqual(share.receipt.branch_id, self.ikeja.pk)
            self.assertEqual(
                set(share.receipt.allocations.values_list("invoice__customer_id", flat=True)),
                {share.customer_id},
            )
        self.assertEqual((self.owing(self.ada_bill), self.owing(self.emeka_bill)), (0, 0))
        self.assertEqual(self.owing(self.chidi_bill), 120_000_00)
        self.assertEqual(net(self.ikeja_bank.gl_account, self.ikeja), 450_000_00)
        self.assertEqual(net(self.held, self.ikeja, self.lekki), -120_000_00)

        held = HeldForBranchReceipt.objects.get()
        self.assertEqual((held.customer, held.for_branch), (self.chidi, self.lekki))
        transfer = forward_held_receipt(held, to_bank_account=self.lekki_bank,
                                        transfer_date=datetime.date(2026, 1, 16))
        post_inter_branch_transfer(transfer)

        transfer.refresh_from_db()
        self.assertEqual((transfer.receipt.branch_id, transfer.receipt.customer_id),
                         (self.lekki.pk, self.chidi.pk))
        self.assertEqual(self.owing(self.chidi_bill), 0)
        self.assertEqual(net(self.held), 0)
        self.assertEqual(net(self.lekki_bank.gl_account, self.lekki), 120_000_00)

    def test_each_branch_audits_only_its_own_figures(self):
        document = self.pay(450_000_00)

        rows = {row.branch_id: row for row in FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.PAYER_PAYMENT_POSTED, target_id=str(document.pk))}
        self.assertEqual(set(rows), {self.ikeja.pk, self.lekki.pk})
        ikeja, lekki = rows[self.ikeja.pk].metadata, rows[self.lekki.pk].metadata
        self.assertEqual((ikeja["amount"], ikeja["received"], ikeja["held_for_other_branches"]),
                         (330_000_00, 450_000_00, 120_000_00))
        self.assertEqual(lekki["amount"], 120_000_00)
        self.assertNotIn("received", lekki)
        self.assertEqual([s["customer"] for s in lekki["shares"]], ["CHIDI"])
        self.assertEqual(ikeja["amount"] + lekki["amount"], document.amount)


class NotEnoughForEverythingTests(_PayerFixture):
    """N400,000 when the three bills come to N450,000."""

    def test_oldest_bill_first_across_every_child_is_the_default(self):
        document = self.pay(400_000_00)

        self.assertEqual(document.split, PayerPaymentSplit.OLDEST_FIRST)
        self.assertEqual(self.shares(document), {
            ("ADA", self.ikeja.pk): (180_000_00, 0, "RECEIPT"),
            ("EMEKA", self.ikeja.pk): (150_000_00, 0, "RECEIPT"),
            ("CHIDI", self.lekki.pk): (70_000_00, 0, "HELD"),
        })

    def test_proportional_shares_follow_what_each_child_owes(self):
        self.set_policy(payer_payment_split=PayerPaymentSplit.PROPORTIONAL)

        document = self.pay(400_000_00)

        expected = split_by_weight(400_000_00, {"ADA": 180_000_00, "EMEKA": 150_000_00, "CHIDI": 120_000_00})
        got = {code: amount for (code, _b), (amount, _s, _k) in self.shares(document).items()}
        self.assertEqual(got, expected)
        self.assertEqual(sum(got.values()), 400_000_00)
        self.assertEqual(self.owing(self.ada_bill), 180_000_00 - expected["ADA"])

    def test_as_entered_asks_for_the_amounts_and_takes_them(self):
        self.set_policy(payer_payment_split=PayerPaymentSplit.AS_ENTERED)

        with self.assertRaisesMessage(PayerPaymentError, "Give each customer's amount"):
            self.pay(400_000_00)
        document = self.pay(400_000_00, shares=[(self.chidi, 120_000_00), (self.emeka, 150_000_00),
                                                (self.ada, 130_000_00)])

        self.assertEqual(document.split, "EXPLICIT")
        self.assertEqual(self.owing(self.ada_bill), 50_000_00)
        self.assertEqual(self.shares(document)[("CHIDI", self.lekki.pk)], (120_000_00, 0, "HELD"))

    def test_the_bursar_overrides_and_what_is_left_follows_the_surplus_rule(self):
        document = self.pay(400_000_00, shares=[(self.ada, 100_000_00)])

        self.assertEqual(self.shares(document), {
            ("ADA", self.ikeja.pk): (100_000_00, 0, "RECEIPT"),
            ("CHIDI", self.lekki.pk): (300_000_00, 300_000_00, "HELD"),
        })
        self.assertEqual(self.owing(self.emeka_bill), 150_000_00)

    def test_amounts_entered_beyond_the_payment_are_refused(self):
        with self.assertRaisesMessage(PayerPaymentError, "more than the"):
            self.pay(100_000_00, shares=[(self.ada, 80_000_00), (self.emeka, 30_000_00)])
        self.assertFalse(Payment.objects.exists())


class MoreThanEverythingTests(_PayerFixture):
    """N500,000 against N450,000 of bills."""

    def test_the_rest_is_credit_on_the_child_with_the_newest_bill(self):
        document = self.pay(500_000_00)

        self.assertEqual(self.shares(document)[("CHIDI", self.lekki.pk)], (170_000_00, 50_000_00, "HELD"))

    def test_the_rest_may_be_left_on_the_payers_own_account(self):
        self.set_policy(payer_payment_surplus=PayerPaymentSurplus.PAYER)

        document = self.pay(500_000_00)

        share = document.shares.get(customer=self.okafor)
        self.assertEqual((share.branch_id, int(share.amount), int(share.surplus)),
                         (self.ikeja.pk, 50_000_00, 50_000_00))
        self.assertEqual(share.receipt.credit_remaining, 50_000_00)


class WhoMayBePaidForTests(_PayerFixture):
    def test_a_customer_the_payer_does_not_pay_for_is_refused(self):
        stranger = self.account(self.books, "BELLO", "Tunde Bello", self.ikeja)
        self.bill(self.books, stranger, self.ikeja, 90_000_00, 3)

        with self.assertRaisesMessage(PayerPaymentError, "does not pay for"):
            self.pay(90_000_00, shares=[(stranger, 90_000_00)])
        plan = plan_payer_payment(self.okafor, bank_account=self.ikeja_bank, amount=900_000_00,
                                  payment_date=JAN_15)
        self.assertNotIn(stranger.pk, {customer_id for customer_id, _b in plan.shares})

    def test_an_ended_link_stops_the_payer_paying_for_that_child(self):
        PayerLink.objects.filter(customer=self.chidi).update(is_active=False)

        document = self.pay(330_000_00)

        self.assertEqual({code for code, _b in self.shares(document)}, {"ADA", "EMEKA"})
        self.assertEqual(self.owing(self.chidi_bill), 120_000_00)


class VoidTests(_PayerFixture):
    def test_voiding_undoes_every_receipt_and_the_held_money_together(self):
        document = self.pay(450_000_00)

        void_payer_payment(document)

        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.REVERSED)
        self.assertEqual(self.owing(self.ada_bill), 180_000_00)
        self.assertEqual(self.owing(self.emeka_bill), 150_000_00)
        self.assertEqual(set(Payment.objects.values_list("status", flat=True)), {DocumentStatus.REVERSED})
        self.assertEqual(HeldForBranchReceipt.objects.get().status, DocumentStatus.REVERSED)
        self.assertEqual(net(self.ikeja_bank.gl_account), 0)
        self.assertEqual(net(self.held), 0)
        voided = FinanceAuditLog.objects.filter(action=FinanceAuditAction.PAYER_PAYMENT_VOIDED)
        self.assertEqual(set(voided.values_list("branch_id", flat=True)), {self.ikeja.pk, self.lekki.pk})

    def test_no_receipt_of_it_is_voided_on_its_own(self):
        document = self.pay(450_000_00)
        receipt = document.shares.filter(receipt__isnull=False).first().receipt
        held = document.shares.get(held_receipt__isnull=False).held_receipt

        with self.assertRaisesMessage(PostingError, f"Void {document.document_number} instead"):
            void_payment(receipt)
        with self.assertRaisesMessage(InterBranchError, f"Void {document.document_number} instead"):
            void_held_receipt(held)

    def test_a_forwarded_share_must_be_brought_back_first(self):
        document = self.pay(450_000_00)
        held = HeldForBranchReceipt.objects.get()
        transfer = forward_held_receipt(held, to_bank_account=self.lekki_bank,
                                        transfer_date=datetime.date(2026, 1, 16))
        post_inter_branch_transfer(transfer)

        with self.assertRaisesMessage(PostingError, "void that transfer first"):
            void_payer_payment(document)
        void_inter_branch_transfer(transfer)
        void_payer_payment(document)

        self.assertEqual(self.owing(self.chidi_bill), 120_000_00)
        self.assertEqual(net(self.held), 0)


class ClosedPeriodTests(_PayerFixture):
    def test_a_closed_month_refuses_the_whole_payment(self):
        with self.assertRaises(PeriodClosedError):
            record_payer_payment(self.okafor, bank_account=self.ikeja_bank, amount=450_000_00,
                                 payment_date=FEB_10)
        self.assertFalse(PayerPayment.objects.exists())
        self.assertFalse(Payment.objects.exists())


class OneBranchTests(_PayerFixture):
    """Mrs Bello pays for two children at Single Site, which has one branch."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bello = cls.account(cls.solo_books, "BELLO", "Mrs Bello", None)
        cls.kemi = cls.account(cls.solo_books, "KEMI", "Kemi Bello", None)
        cls.tobi = cls.account(cls.solo_books, "TOBI", "Tobi Bello", cls.solo_main)
        for child in (cls.kemi, cls.tobi):
            link_customer(cls.bello, child)
        cls.kemi_bill = cls.bill(cls.solo_books, cls.kemi, None, 60_000_00, 4)
        cls.tobi_bill = cls.bill(cls.solo_books, cls.tobi, cls.solo_main, 40_000_00, 6)

    def test_every_share_is_a_receipt_and_nothing_is_held(self):
        document = record_payer_payment(self.bello, bank_account=self.solo_bank,
                                        amount=100_000_00, payment_date=JAN_15)

        self.assertEqual({k for _key, (_a, _s, k) in self.shares(document).items()}, {"RECEIPT"})
        self.assertEqual((self.owing(self.kemi_bill), self.owing(self.tobi_bill)), (0, 0))
        self.assertFalse(HeldForBranchReceipt.objects.exists())


class PayerPaymentApiTests(_PayerFixture):
    """Who may record, read and void a payer's payment."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ikeja_bursar = cls.bursar(cls.ikeja, keys=KEYS)
        cls.lekki_bursar = cls.bursar(cls.lekki, keys=KEYS)
        cls.yaba_bursar = cls.bursar(cls.yaba, keys=KEYS)
        cls.reader = cls.bursar(cls.ikeja, keys=("finance.payment.view",))
        cls.rival = cls.bursar(None, tenant=cls.rival_tenant, keys=KEYS)

    def body(self, **extra):
        return {"payer": "OKAFOR", "bank_account": self.ikeja_bank.pk, "amount": 450_000_00,
                "payment_date": "2026-01-15", **extra}

    def record(self, user, path="payer-payments/", **extra):
        return TenantAPIClient(user=user).post(self.url(path), self.body(**extra), format="json")

    def test_without_the_create_key_it_is_refused(self):
        response = self.record(self.reader)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(PayerPayment.objects.exists())

    def test_ikejas_bursar_records_it_and_lekki_sees_only_chidis_share(self):
        response = self.record(self.ikeja_bursar)

        self.assertEqual(response.status_code, 201, response.data)
        pk = response.data["data"]["id"]
        lekki = TenantAPIClient(user=self.lekki_bursar).get(self.url(f"payer-payments/{pk}/"))
        self.assertEqual(lekki.status_code, 200, lekki.data)
        self.assertEqual([s["customer"]["code"] for s in lekki.data["data"]["shares"]], ["CHIDI"])
        self.assertEqual(lekki.data["data"]["amount"], 120_000_00)
        yaba = TenantAPIClient(user=self.yaba_bursar).get(self.url(f"payer-payments/{pk}/"))
        self.assertEqual(yaba.status_code, 404)
        self.assertEqual(TenantAPIClient(user=self.yaba_bursar).get(
            self.url("payer-payments/")).data["data"], [])

    def test_lekkis_bursar_cannot_use_ikejas_bank_or_void_ikejas_payment(self):
        self.assertEqual(self.record(self.lekki_bursar).status_code, 404)
        pk = self.record(self.ikeja_bursar).data["data"]["id"]

        response = TenantAPIClient(user=self.lekki_bursar).post(
            self.url(f"payer-payments/{pk}/void/"), {}, format="json")

        self.assertEqual(response.status_code, 403, response.data)
        voided = TenantAPIClient(user=self.ikeja_bursar).post(
            self.url(f"payer-payments/{pk}/void/"), {}, format="json")
        self.assertEqual(voided.status_code, 200, voided.data)
        self.assertEqual(voided.data["data"]["status"], DocumentStatus.REVERSED)

    def test_the_preview_shows_lekkis_bills_only_to_someone_who_reaches_lekki(self):
        response = self.record(self.ikeja_bursar, path="payer-payments/preview/", amount=400_000_00)

        self.assertEqual(response.status_code, 200, response.data)
        rows = {row["customer"]["code"]: row for row in response.data["data"]["shares"]}
        self.assertEqual(rows["ADA"]["bills"][0]["document_number"], self.ada_bill.document_number)
        self.assertEqual((rows["CHIDI"]["kind"], rows["CHIDI"]["amount"]), ("HELD", 70_000_00))
        self.assertNotIn("bills", rows["CHIDI"])
        self.assertFalse(PayerPayment.objects.exists())

    def test_another_tenant_cannot_reach_these_books(self):
        response = self.record(self.rival)

        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(PayerPayment.objects.exists())

    def test_a_child_the_payer_does_not_pay_for_is_a_400(self):
        response = self.record(self.ikeja_bursar, shares=[{"customer": "NOBODY", "amount": 1}])

        self.assertEqual(response.status_code, 400, response.data)

    def test_links_are_written_and_read_by_someone_who_can_see_both_accounts(self):
        """Each bursar sees the links to their own branches' children and the shared payer."""
        newborn = self.account(self.books, "OBI", "Obi Okafor", self.ikeja)
        ikeja = TenantAPIClient(user=self.ikeja_bursar)
        lekki = TenantAPIClient(user=self.lekki_bursar)

        self.assertEqual(lekki.post(self.url("payer-links/"), {"payer": "OKAFOR", "customer": "OBI"},
                                    format="json").status_code, 404)
        created = ikeja.post(self.url("payer-links/"), {"payer": "OKAFOR", "customer": "OBI"}, format="json")

        self.assertEqual(created.status_code, 201, created.data)
        self.assertTrue(PayerLink.objects.filter(payer=self.okafor, customer=newborn, is_active=True).exists())
        listed = ikeja.get(self.url("payer-links/?payer=OKAFOR"))
        self.assertEqual({row["customer"]["code"] for row in listed.data["data"]},
                         {"ADA", "EMEKA", "OBI"})
        self.assertEqual({row["customer"]["code"] for row in lekki.get(
            self.url("payer-links/?payer=OKAFOR")).data["data"]}, {"CHIDI"})


class PayerSettingsTests(_PayerFixture):
    def test_the_settings_take_only_listed_values(self):
        from rest_framework.exceptions import ValidationError

        update_receivables_policy(entity=self.books, data={"payer_payment_split": "proportional"},
                                  actor_user=None)
        self.assertEqual(FinanceReceivablesPolicy.objects.get(entity=self.books).payer_payment_split,
                         PayerPaymentSplit.PROPORTIONAL)
        with self.assertRaises(ValidationError):
            update_receivables_policy(entity=self.books, data={"payer_payment_surplus": "ELDEST"},
                                      actor_user=None)


class FeeRunLeavesPayersOutTests(_PayerFixture):
    def test_billing_every_active_customer_does_not_bill_the_payer(self):
        structure = FeeStructure.objects.create(entity=self.books, code="T2", name="Term 2", branch=None)
        FeeItem.objects.create(structure=structure, line_no=1, description="Tuition",
                               revenue_account=Account.objects.get(entity=self.books, code="4100"),
                               amount=10_000_00)
        user = self.bursar(None, keys=("finance.feestructure.generate",))

        response = TenantAPIClient(user=user).post(
            self.url(f"fee-structures/{structure.code}/generate/"),
            {"all_active": True, "invoice_date": "2026-01-15", "branch": self.ikeja.pk}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        billed = set(Invoice.objects.filter(reference="FEE:T2").values_list("customer__code", flat=True))
        self.assertIn("ADA", billed)
        self.assertNotIn("OKAFOR", billed)

