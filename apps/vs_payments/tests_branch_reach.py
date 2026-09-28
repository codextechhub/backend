"""The payments console names customers, invoices and vendors within the caller's branches.

Corona runs Ikeja, Lekki and Yaba. Ikeja's clerk works payments for Ikeja. She
must not raise a payment request or a virtual account for a Lekki family, or pay
out to a vendor Lekki keeps to itself, any more than the finance screens let
her. Each is answered exactly as a customer, invoice or vendor that does not
exist, while Ikeja's own and the school-wide ones resolve as before.
"""
from __future__ import annotations

import itertools

from core.test_utils import TenantAPIClient
from vs_finance.models import Account
from vs_finance.tests_branch_scope import _FinanceBranchFixture

from .models import CollectionIntent, PayoutBatch, VirtualAccount

_clerks = itertools.count(1)


class PaymentsNameOnlyWhatTheClerkReachesTests(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        e = self.books
        self.ikeja_customer = self.customer(e, "CIKJP", self.ikeja)
        self.lekki_customer = self.customer(e, "CLEKP", self.lekki)
        self.shared_customer = self.customer(e, "CALLP", None)

    def clerk(self, *keys):
        n = next(_clerks)
        return TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, f"clerk-{n}@corona.test"), *keys,
            tenant=self.tenant, role_key=f"clerk-{n}", branch=self.ikeja,
        ))

    def post(self, client, path, body, **extra):
        return client.post(f"/v1/payments/{path}?entity={self.books.code}", body,
                           format="json", **extra)

    def vendor(self, code, branch):
        from vs_procurement.models import Vendor

        return Vendor.objects.create(
            entity=self.books, code=code, name=f"Vendor {code}", branch=branch,
            payable_account=Account.objects.get(entity=self.books, code="2100"),
        )

    def test_a_payment_request_for_another_branchs_customer(self):
        client = self.clerk("payments.collection.create")
        refused = self.post(client, "collections/", {"amount": 5_000, "customer": "CLEKP"})
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("No customer 'CLEKP' in this entity.", str(refused.data))
        self.assertFalse(CollectionIntent.objects.filter(customer=self.lekki_customer).exists())
        for code in ("CIKJP", "CALLP"):
            with self.subTest(customer=code):
                accepted = self.post(client, "collections/", {"amount": 5_000, "customer": code})
                self.assertNotIn("No customer", str(accepted.data))

    def test_a_payment_request_against_another_branchs_invoice(self):
        lekki_invoice = self.invoice(self.books, self.lekki_customer, self.lekki)
        refused = self.post(self.clerk("payments.collection.create"), "collections/", {
            "amount": 5_000, "customer": "CALLP", "invoice": lekki_invoice.pk,
        })
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn(f"No invoice '{lekki_invoice.pk}' in this entity.", str(refused.data))

    def test_a_virtual_account_for_another_branchs_customer(self):
        refused = self.post(self.clerk("payments.virtual_account.create"),
                            "virtual-accounts/", {"customer": "CLEKP"})
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("No customer 'CLEKP' in this entity.", str(refused.data))
        self.assertFalse(VirtualAccount.objects.filter(customer=self.lekki_customer).exists())

    def test_a_payout_to_a_vendor_another_branch_keeps(self):
        lekki_vendor = self.vendor("VLEK", self.lekki)
        client = self.clerk("payments.payout.create")
        refused = self.post(client, "payouts/", {"amount": 5_000, "vendor": lekki_vendor.pk},
                            HTTP_IDEMPOTENCY_KEY="reach-single-1")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("No such vendor in this entity.", str(refused.data))
        for n, vendor in enumerate((self.vendor("VIKJ", self.ikeja), self.vendor("VALL", None))):
            with self.subTest(vendor=vendor.code):
                accepted = self.post(client, "payouts/", {"amount": 5_000, "vendor": vendor.pk},
                                     HTTP_IDEMPOTENCY_KEY=f"reach-single-ok-{n}")
                self.assertNotIn("No such vendor", str(accepted.data))

    def test_a_payout_batch_line_to_a_vendor_another_branch_keeps(self):
        lekki_vendor = self.vendor("VLEKB", self.lekki)
        before = PayoutBatch.objects.count()
        refused = self.post(
            self.clerk("payments.payout.create"), "payout-batches/",
            {"items": [{"amount": 5_000, "vendor": lekki_vendor.pk}]},
            HTTP_IDEMPOTENCY_KEY="reach-batch-1",
        )
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("No such vendor in this entity.", str(refused.data))
        self.assertEqual(PayoutBatch.objects.count(), before)
