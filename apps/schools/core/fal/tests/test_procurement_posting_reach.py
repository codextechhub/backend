"""Posting through the FAL asks the questions the procurement screen asks.

Corona runs Ikeja and Lekki and buys from one stationer for both. A vendor
payment posted from the procurement screen is checked for its saved plan, the
reach of its bills, its branch and its bank account before any money is
booked. The school's own posting path names its acting user, and that user is
held to exactly the same checks, not to fewer.
"""

from __future__ import annotations

import datetime

from schools.core.fal.adapters.django_finance import DjangoProcurementActionAdapter
from schools.core.fal.contracts import ProcDocRef, ProcDocType
from schools.core.fal.exceptions import CrossBranchError, ProcurementStateError

from .base import FALFixture

PAID_ON = datetime.date(2026, 10, 20)
BILLED_ON = datetime.date(2026, 10, 8)


class PostingThroughTheFalTests(FALFixture):
    def setUp(self):
        super().setUp()
        from vs_procurement.constants import VendorKycStatus
        from vs_procurement.models import Vendor

        self.port = DjangoProcurementActionAdapter()
        self.entity_id = self.corona_books.entity_ref
        self.vendor = Vendor.objects.create(
            entity_id=self.entity_id, name="Ojo Stationers",
            kyc_status=VendorKycStatus.VERIFIED,
            payable_account=self.account(self.entity_id, "2100"),
            default_expense_account=self.account(self.entity_id, "5200"),
        )
        self.ikeja_bill = self.bill(self.ikeja)
        self.lekki_bill = self.bill(self.lekki)
        self.ikeja_bank = self.bank("Ikeja Collections", self.ikeja, "81")
        self.lekki_bank = self.bank("Lekki Collections", self.lekki, "82")

    # ----- rows ------------------------------------------------------------ #
    def bill(self, branch, *, status="POSTED"):
        from vs_procurement.constants import ProcApprovalState
        from vs_procurement.models import VendorInvoice

        return VendorInvoice.objects.create(
            entity_id=self.entity_id, vendor=self.vendor, branch=branch,
            invoice_date=BILLED_ON, due_date=BILLED_ON, total=10_000, subtotal=10_000,
            status=status, approval_state=ProcApprovalState.APPROVED,
        )

    def bank(self, name, branch, tag):
        from vs_finance.models import Account, BankAccount

        gl = Account.objects.create(
            entity_id=self.entity_id, code=f"11{tag}", name=f"Cash {tag}",
            account_type=self.account(self.entity_id, "1000").account_type,
            is_postable=True,
        )
        return BankAccount.objects.create(
            entity_id=self.entity_id, name=name, branch=branch, gl_account=gl)

    def payment(self, branch, bank, *bills):
        """An approved draft, settling ``bills``, as older or other code may have written it."""
        from vs_procurement.constants import ProcApprovalState
        from vs_procurement.models import VendorPayment, VendorPaymentAllocation

        payment = VendorPayment.objects.create(
            entity_id=self.entity_id, vendor=self.vendor, branch=branch,
            payment_date=PAID_ON, gross_amount=10_000, wht_amount=0, net_amount=10_000,
            payment_account=bank.gl_account, approval_state=ProcApprovalState.APPROVED,
        )
        for bill in bills:
            VendorPaymentAllocation.objects.create(
                payment=payment, vendor_invoice=bill, amount=10_000)
        return payment

    def post(self, document, doc_type, actor):
        ref = ProcDocRef(doc_type=doc_type, doc_ref=document.pk,
                         entity_ref=self.entity_id, branch_ref=None)
        return self.port.post_to_ledger(ref, actor_ref=actor.pk)

    def assertNothingSettled(self, payment):
        payment.refresh_from_db()
        self.assertEqual(payment.status, "DRAFT")
        self.assertIsNone(payment.journal_id)
        for bill in (self.ikeja_bill, self.lekki_bill):
            bill.refresh_from_db()
            self.assertEqual(bill.amount_paid, 0)

    # ----- the plan -------------------------------------------------------- #
    def test_a_payment_with_no_saved_plan_is_refused_rather_than_settled_oldest_first(self):
        payment = self.payment(self.ikeja, self.ikeja_bank)

        with self.assertRaisesMessage(ProcurementStateError, "allocation plan is required"):
            self.post(payment, ProcDocType.VENDOR_PAYMENT, self.bursar)
        self.assertNothingSettled(payment)

    # ----- reach ----------------------------------------------------------- #
    def test_a_lekki_bursar_cannot_post_an_ikeja_payment(self):
        payment = self.payment(self.ikeja, self.ikeja_bank, self.ikeja_bill)

        with self.assertRaises(CrossBranchError):
            self.post(payment, ProcDocType.VENDOR_PAYMENT, self.lekki_bursar)
        self.assertNothingSettled(payment)

    def test_a_lekki_bursar_cannot_post_an_ikeja_bill(self):
        bill = self.bill(self.ikeja, status="DRAFT")

        with self.assertRaises(CrossBranchError):
            self.post(bill, ProcDocType.VENDOR_INVOICE, self.lekki_bursar)
        bill.refresh_from_db()
        self.assertEqual(bill.status, "DRAFT")

    # ----- branch and bank ------------------------------------------------- #
    def test_an_ikeja_payment_whose_plan_names_a_lekki_bill_is_refused(self):
        payment = self.payment(self.ikeja, self.ikeja_bank, self.lekki_bill)

        with self.assertRaisesMessage(ProcurementStateError, "now belong to another branch"):
            self.post(payment, ProcDocType.VENDOR_PAYMENT, self.bursar)
        self.assertNothingSettled(payment)

    def test_an_ikeja_payment_from_lekkis_bank_is_refused_and_posts_from_its_own(self):
        payment = self.payment(self.ikeja, self.lekki_bank, self.ikeja_bill)

        with self.assertRaisesMessage(
            ProcurementStateError,
            "This vendor payment belongs to Ikeja. Pay it from an Ikeja account.",
        ):
            self.post(payment, ProcDocType.VENDOR_PAYMENT, self.bursar)
        self.assertNothingSettled(payment)

        from vs_procurement.models import VendorPayment

        VendorPayment.objects.filter(pk=payment.pk).update(
            payment_account=self.ikeja_bank.gl_account)
        posted = self.post(payment, ProcDocType.VENDOR_PAYMENT, self.bursar).unwrap()
        self.assertEqual(posted.status, "POSTED")
        self.ikeja_bill.refresh_from_db()
        self.assertEqual(self.ikeja_bill.amount_paid, 10_000)
