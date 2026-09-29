"""A gateway payout is booked to the branch whose bank account the money left.

Corona pays Ojo Stationers by Paystack. Ojo has an open bill at Ikeja and one at
Lekki. A payout sent from Ikeja's bank books an Ikeja vendor payment: its journal
is Ikeja's and it settles the Ikeja bill. A payout from Lekki's bank books to
Lekki and settles the Lekki bill. The same holds for each line of a batch, whose
lines may be paid from different banks.
"""
from __future__ import annotations

import datetime

from vs_config.clock import tenant_today
from vs_finance.constants import DocumentStatus
from vs_finance.models import Account, BankAccount, FiscalPeriod, FiscalYear
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_procurement.constants import ProcApprovalState, VendorKycStatus
from vs_procurement.models import Vendor, VendorInvoice, VendorPayment

from . import services
from .constants import PayoutStatus
from .models import PayoutBatch, PayoutInstruction

BILL = 10_000


class PayoutBranchTests(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        e = self.books
        self.open_the_year(tenant_today(self.tenant))
        self.vendor = Vendor.objects.create(
            entity=e, code="OJO", name="Ojo Stationers",
            payable_account=Account.objects.get(entity=e, code="2100"),
            kyc_status=VendorKycStatus.VERIFIED,
        )
        billed = datetime.date(tenant_today(self.tenant).year, 1, 5)
        self.bills = {
            name: VendorInvoice.objects.create(
                entity=e, vendor=self.vendor, branch=branch, invoice_date=billed,
                due_date=billed, total=BILL, subtotal=BILL,
                status=DocumentStatus.POSTED, approval_state=ProcApprovalState.APPROVED)
            for name, branch in (("ikeja", self.ikeja), ("lekki", self.lekki))
        }
        cash_type = Account.objects.get(entity=e, code="1000").account_type
        self.banks = {}
        for n, (name, branch) in enumerate((("ikeja", self.ikeja), ("lekki", self.lekki))):
            gl = Account.objects.create(entity=e, code=f"119{n}", name=f"Bank {name}",
                                        account_type=cash_type, is_postable=True)
            BankAccount.objects.create(entity=e, name=f"Bank {name}", branch=branch,
                                       gl_account=gl)
            self.banks[name] = gl

    def open_the_year(self, today):
        """Every month of the year the payouts are booked in, open for posting."""
        year, _ = FiscalYear.objects.get_or_create(
            entity=self.books, year=today.year, defaults={
                "start_date": datetime.date(today.year, 1, 1),
                "end_date": datetime.date(today.year, 12, 31)})
        for month in range(1, 13):
            start = datetime.date(today.year, month, 1)
            end = (datetime.date(today.year + (month == 12), month % 12 + 1, 1)
                   - datetime.timedelta(days=1))
            FiscalPeriod.objects.get_or_create(
                entity=self.books, fiscal_year=year, period_no=month,
                defaults={"name": f"{today.year}-{month:02d}",
                          "start_date": start, "end_date": end})

    def paid_out(self, reference, bank, *, batch=None):
        """A payout line sent from ``bank`` and confirmed paid by the provider."""
        payout = PayoutInstruction.objects.create(
            entity=self.books, provider="PAYSTACK", reference=reference,
            provider_reference=f"TR-{reference}", amount=BILL,
            beneficiary_name=self.vendor.name, beneficiary_account_number="0123456789",
            source_account=self.banks[bank], batch=batch, status=PayoutStatus.PROCESSING,
            vendor_source_type="vs_procurement.Vendor", vendor_source_id=str(self.vendor.pk))
        payout = services.confirm_payout(payout, status=PayoutStatus.PAID)
        self.assertEqual(payout.status, PayoutStatus.PAID)
        return VendorPayment.objects.select_related("journal").get(pk=payout.vendor_payment_id)

    def paid(self):
        return {name: VendorInvoice.objects.get(pk=bill.pk).amount_paid
                for name, bill in self.bills.items()}

    def test_a_payout_from_ikejas_bank_is_ikejas_and_settles_ikejas_bill(self):
        payment = self.paid_out("PAY-IKJ", "ikeja")

        self.assertEqual(payment.branch_id, self.ikeja.pk)
        self.assertEqual(payment.journal.branch_id, self.ikeja.pk)
        self.assertEqual(self.paid(), {"ikeja": BILL, "lekki": 0})

    def test_each_batch_line_books_to_the_branch_of_its_own_bank(self):
        batch = PayoutBatch.objects.create(entity=self.books, provider="PAYSTACK",
                                           reference="BAT-TWO")
        lekki = self.paid_out("PAY-B-LEK", "lekki", batch=batch)
        ikeja = self.paid_out("PAY-B-IKJ", "ikeja", batch=batch)

        self.assertEqual((lekki.branch_id, lekki.journal.branch_id),
                         (self.lekki.pk, self.lekki.pk))
        self.assertEqual((ikeja.branch_id, ikeja.journal.branch_id),
                         (self.ikeja.pk, self.ikeja.pk))
        self.assertEqual(self.paid(), {"ikeja": BILL, "lekki": BILL})
