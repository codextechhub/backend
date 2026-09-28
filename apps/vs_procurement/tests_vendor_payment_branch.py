"""A vendor payment keeps both branch rules when it is edited and when it is posted.

Corona runs Ikeja, Lekki and Yaba, and buys from one stationer for all of them.
A payment belongs to the branch of the bills it settles and is paid from that
branch's bank account or a school-wide one. Those rules hold when a draft is
written, and they must still hold when it is edited to settle different bills,
and when it is finally posted, whatever changed in between.
"""
from __future__ import annotations

import datetime
import itertools

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus
from vs_finance.models import Account, BankAccount
from vs_finance.tests_branch_scope import _FinanceBranchFixture

from .constants import ProcApprovalState
from .models import Vendor, VendorInvoice, VendorPayment

JAN = datetime.date(2026, 1, 12)
KEYS = (
    "procurement.vendor_payment.create", "procurement.vendor_payment.update",
    "procurement.vendor_payment.view", "procurement.vendor_payment.post",
)
UNKNOWN_BILL = "Every invoice must be posted and belong to the selected vendor."
_officers = itertools.count(1)


class VendorPaymentBranchTests(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        e = self.books
        self.vendor = Vendor.objects.create(
            entity=e, code="STAT", name="Stationer",
            payable_account=Account.objects.get(entity=e, code="2100"), kyc_status="VERIFIED",
        )
        self.ikeja_bill = self.bill(self.ikeja)
        self.lekki_bill = self.bill(self.lekki)
        self.ikeja_bank = self.bank("Ikeja Collections", self.ikeja, "40")
        self.lekki_bank = self.bank("Lekki Collections", self.lekki, "41")

    def bill(self, branch):
        return VendorInvoice.objects.create(
            entity=self.books, vendor=self.vendor, branch=branch,
            invoice_date=JAN, due_date=JAN, total=10_000, subtotal=10_000,
            status=DocumentStatus.POSTED, approval_state=ProcApprovalState.APPROVED,
        )

    def bank(self, name, branch, tag):
        gl = Account.objects.create(
            entity=self.books, code=f"11{tag}", name=f"Cash {tag}",
            account_type=Account.objects.get(entity=self.books, code="1000").account_type,
            is_postable=True,
        )
        return BankAccount.objects.create(entity=self.books, name=name, branch=branch, gl_account=gl)

    def officer(self, *branches):
        """A procurement officer bound to ``branches``."""
        n = next(_officers)
        user = self.user_for(self.tenant, f"officer-{n}@corona.test")
        for branch in branches:
            self.grant(user, *KEYS, tenant=self.tenant,
                       role_key=f"officer-{n}-{branch.pk}", branch=branch)
        return TenantAPIClient(user=user)

    def url(self, suffix=""):
        return f"/v1/procurement/vendor-payments/{suffix}?entity={self.books.code}"

    def body(self, bill, bank):
        return {"vendor": self.vendor.pk, "payment_date": JAN.isoformat(),
                "bank_account": bank.pk,
                "allocations": [{"vendor_invoice": bill.pk, "amount": 10_000}]}

    def draft(self, client, bill, bank):
        response = client.post(self.url(), self.body(bill, bank), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return VendorPayment.objects.get(pk=response.data["data"]["id"])

    def settles(self, payment):
        return list(payment.allocations.values_list("vendor_invoice_id", flat=True))

    # -- edit ----------------------------------------------------------------- #

    def test_a_lekki_bill_is_unknown_to_ikeja_on_create_and_on_edit(self):
        ikeja = self.officer(self.ikeja)
        refused = ikeja.post(self.url(), self.body(self.lekki_bill, self.ikeja_bank), format="json")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn(UNKNOWN_BILL, str(refused.data))

        payment = self.draft(ikeja, self.ikeja_bill, self.ikeja_bank)
        refused = ikeja.patch(self.url(f"{payment.pk}/"),
                              self.body(self.lekki_bill, self.ikeja_bank), format="json")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn(UNKNOWN_BILL, str(refused.data))
        payment.refresh_from_db()
        self.assertEqual(self.settles(payment), [self.ikeja_bill.pk])
        self.assertEqual(payment.branch_id, self.ikeja.pk)

    def test_an_edit_moves_the_payment_to_its_bills_branch_and_checks_the_bank_there(self):
        okafor = self.officer(self.ikeja, self.lekki)
        payment = self.draft(okafor, self.ikeja_bill, self.ikeja_bank)
        self.assertEqual(payment.branch_id, self.ikeja.pk)

        refused = okafor.patch(self.url(f"{payment.pk}/"),
                               self.body(self.lekki_bill, self.ikeja_bank), format="json")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn(
            "This vendor payment belongs to Lekki Branch. "
            "Pay it from a Lekki Branch account or a school-wide one.", str(refused.data))
        payment.refresh_from_db()
        self.assertEqual((payment.branch_id, self.settles(payment)),
                         (self.ikeja.pk, [self.ikeja_bill.pk]))

        accepted = okafor.patch(self.url(f"{payment.pk}/"),
                                self.body(self.lekki_bill, self.lekki_bank), format="json")
        self.assertEqual(accepted.status_code, 200, accepted.data)
        payment.refresh_from_db()
        self.assertEqual((payment.branch_id, self.settles(payment)),
                         (self.lekki.pk, [self.lekki_bill.pk]))
        self.assertEqual(payment.payment_account_id, self.lekki_bank.gl_account_id)

    # -- post ----------------------------------------------------------------- #

    def approved(self, payment):
        VendorPayment.objects.filter(pk=payment.pk).update(
            approval_state=ProcApprovalState.APPROVED)

    def assertNotPosted(self, payment):
        payment.refresh_from_db()
        self.assertEqual(payment.status, DocumentStatus.DRAFT)
        self.assertIsNone(payment.journal_id)
        self.lekki_bill.refresh_from_db()
        self.ikeja_bill.refresh_from_db()
        self.assertEqual((self.ikeja_bill.amount_paid, self.lekki_bill.amount_paid), (0, 0))

    def test_posting_a_draft_whose_bank_moved_to_another_branch_is_refused(self):
        okafor = self.officer(self.ikeja, self.lekki)
        payment = self.draft(okafor, self.ikeja_bill, self.ikeja_bank)
        self.approved(payment)
        BankAccount.objects.filter(pk=self.ikeja_bank.pk).update(branch=self.lekki)

        refused = okafor.post(self.url(f"{payment.pk}/post/"), {}, format="json")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("This vendor payment belongs to Ikeja Branch.", str(refused.data))
        self.assertNotPosted(payment)

        # Moved back, the same draft still holds and posts.
        BankAccount.objects.filter(pk=self.ikeja_bank.pk).update(branch=self.ikeja)
        posted = okafor.post(self.url(f"{payment.pk}/post/"), {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        payment.refresh_from_db()
        self.assertEqual(payment.status, DocumentStatus.POSTED)

    def test_posting_a_draft_that_settles_another_branchs_bill_is_refused(self):
        """A draft written before the edit rules held: Ikeja's payment, a Lekki bill."""
        ikeja = self.officer(self.ikeja)
        payment = self.draft(ikeja, self.ikeja_bill, self.ikeja_bank)
        payment.allocations.update(vendor_invoice=self.lekki_bill)
        self.approved(payment)

        refused = ikeja.post(self.url(f"{payment.pk}/post/"), {}, format="json")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn(UNKNOWN_BILL, str(refused.data))
        self.assertNotPosted(payment)

        okafor = self.officer(self.ikeja, self.lekki)
        refused = okafor.post(self.url(f"{payment.pk}/post/"), {}, format="json")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("now belong to another branch", str(refused.data))
        self.assertNotPosted(payment)
