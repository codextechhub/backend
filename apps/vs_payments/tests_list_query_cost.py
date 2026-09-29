"""A payments list costs the same for a long page as for a short one.

Every row of the collections, virtual accounts, payouts and payout batches lists
names its books, and most name a customer and a ledger account. Each is read with
the rows, so Corona's bursar paging through a busy term's payouts waits on one
page of queries, not one per payout. Each list is measured at two lengths, so the
property held is that the cost does not grow per row, not a count that moves
whenever the view changes.
"""
from __future__ import annotations

import itertools

from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.test_utils import TenantAPIClient
from vs_finance.models import Account
from vs_finance.tests_branch_scope import _FinanceBranchFixture

from .constants import PayoutStatus
from .models import CollectionIntent, PayoutBatch, PayoutInstruction, VirtualAccount

_rows = itertools.count(1)


class PaymentsListQueryCostTests(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        self.bank_type = Account.objects.get(entity=self.books, code="1000").account_type
        user = self.grant(
            self.user_for(self.tenant, "bursar-cost@corona.test"),
            "payments.collection.view", "payments.virtual_account.view",
            "payments.payout.view", tenant=self.tenant, role_key="bursar-cost")
        self.client = TenantAPIClient(user=user)

    def ledger(self, n):
        return Account.objects.create(
            entity=self.books, code=f"18{n:02d}", name=f"Bank {n}",
            account_type=self.bank_type, is_postable=True)

    def collection(self):
        n = next(_rows)
        CollectionIntent.objects.create(
            entity=self.books, provider="PAYSTACK", reference=f"COL-{n}", amount=5_000,
            customer=self.customer(self.books, f"CC{n}", self.ikeja),
            deposit_account=self.ledger(n))

    def virtual_account(self):
        n = next(_rows)
        VirtualAccount.objects.create(
            entity=self.books, provider="PAYSTACK",
            customer=self.customer(self.books, f"CV{n}", self.ikeja),
            deposit_account=self.ledger(n),
            account_number=f"90{n:08d}", provider_reference=f"VA-{n}")

    def payout(self):
        n = next(_rows)
        PayoutInstruction.objects.create(
            entity=self.books, provider="PAYSTACK", reference=f"PAY-{n}", amount=2_000,
            beneficiary_name="Supplier Ltd", beneficiary_account_number="0123456789",
            source_account=self.ledger(n), status=PayoutStatus.PENDING)

    def batch(self):
        PayoutBatch.objects.create(
            entity=self.books, provider="PAYSTACK", reference=f"BAT-{next(_rows)}")

    def cost(self, path, model, make, rows):
        url = f"/v1/payments/{path}?entity={self.books.code}"
        while model.objects.filter(entity=self.books).count() < rows:
            make()
        self.client.get(url)  # warm the permission and content-type caches
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["data"]), rows)
        return len(captured.captured_queries)

    def test_each_list_costs_the_same_for_a_long_page(self):
        for path, model, make in (
            ("collections/", CollectionIntent, self.collection),
            ("virtual-accounts/", VirtualAccount, self.virtual_account),
            ("payouts/", PayoutInstruction, self.payout),
            ("payout-batches/", PayoutBatch, self.batch),
        ):
            with self.subTest(path=path):
                short = self.cost(path, model, make, 2)
                self.assertEqual(self.cost(path, model, make, 5), short)
