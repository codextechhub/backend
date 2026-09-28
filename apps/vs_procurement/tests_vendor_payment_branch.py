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


class _VendorPaymentFixture(_FinanceBranchFixture):
    """One stationer with an open bill at Ikeja and at Lekki, and a bank at each."""

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


class VendorPaymentBranchTests(_VendorPaymentFixture):
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


class VendorAdvanceBranchTests(_VendorPaymentFixture):
    """Money a vendor was paid ahead of a bill settles bills of the payment's own branch.

    Ojo Stationers holds open bills at Ikeja, at Lekki and for the whole school, all
    due the same day. Ikeja paid Ojo ahead of any bill. Settling that advance
    automatically, oldest-first, must pick Ikeja's bills only: Ikeja's money settling
    Lekki's bill leaves Ikeja's books short and Lekki's still owing. A school-wide
    advance settles the school-wide bills.
    """

    def officer(self, *branches):
        """A procurement officer bound to ``branches``, who may apply advances."""
        n = next(_officers)
        user = self.user_for(self.tenant, f"advance-officer-{n}@corona.test")
        for branch in branches:
            self.grant(user, *KEYS, "procurement.vendor_payment.allocate", tenant=self.tenant,
                       role_key=f"advance-officer-{n}-{branch.pk}", branch=branch)
        return TenantAPIClient(user=user)

    def bursar(self):
        """A caller who covers the whole school, the only one who reaches a school-wide payment."""
        user = self.user_for(self.tenant, f"advance-bursar-{next(_officers)}@corona.test")
        self.grant(user, *KEYS, "procurement.vendor_payment.allocate", tenant=self.tenant,
                   role_key=f"advance-bursar-{user.pk}")
        return TenantAPIClient(user=user)

    def approved_draft(self, branch, bank, gross=30_000):
        return VendorPayment.objects.create(
            entity=self.books, vendor=self.vendor, branch=branch, payment_date=JAN,
            gross_amount=gross, wht_amount=0, net_amount=gross,
            payment_account=bank.gl_account, approval_state=ProcApprovalState.APPROVED,
        )

    def advance(self, branch, bank):
        """A posted payment of ``branch`` whose whole amount is still an advance."""
        from . import payables

        payment = self.approved_draft(branch, bank)
        payables.post_vendor_payment(payment, auto_allocate=False)
        payment.refresh_from_db()
        self.assertEqual(payment.advance_remaining, 30_000)
        return payment

    def paid(self):
        for bill in (self.ikeja_bill, self.lekki_bill, self.shared_bill):
            bill.refresh_from_db()
        return {"ikeja": self.ikeja_bill.amount_paid, "lekki": self.lekki_bill.amount_paid,
                "shared": self.shared_bill.amount_paid}

    def auto_allocate(self, client, payment):
        response = client.post(self.url(f"{payment.pk}/allocate/"),
                               {"auto_allocate": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        payment.refresh_from_db()
        return payment

    def test_an_ikeja_advance_settles_only_ikejas_bills(self):
        self.shared_bill = self.bill(None)
        payment = self.advance(self.ikeja, self.ikeja_bank)

        payment = self.auto_allocate(self.officer(self.ikeja, self.lekki), payment)
        self.assertEqual(self.paid(), {"ikeja": 10_000, "lekki": 0, "shared": 0})
        self.assertEqual(payment.advance_remaining, 20_000)

    def test_a_school_wide_advance_settles_only_school_wide_bills(self):
        self.shared_bill = self.bill(None)
        whole_school = self.bank("Head Office", None, "42")
        payment = self.advance(None, whole_school)

        payment = self.auto_allocate(self.bursar(), payment)
        self.assertEqual(self.paid(), {"ikeja": 0, "lekki": 0, "shared": 10_000})
        self.assertEqual(payment.advance_remaining, 20_000)

    def test_posting_with_automatic_settlement_settles_only_the_payments_branch(self):
        """The automatic plan a posting draws (a gateway payout's booking) holds the same rule."""
        from . import payables

        self.shared_bill = self.bill(None)
        payment = self.approved_draft(self.lekki, self.lekki_bank)
        payables.post_vendor_payment(payment)
        payment.refresh_from_db()
        self.assertEqual(self.paid(), {"ikeja": 0, "lekki": 10_000, "shared": 0})
        self.assertEqual(payment.advance_remaining, 20_000)
