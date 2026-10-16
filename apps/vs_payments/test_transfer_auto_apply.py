"""A bank transfer into a virtual account settles the customer's open bills.

Acme owes two bills: ₦30,000.00 due first and ₦20,000.00 due a week later. Its
bursar's mother transfers money into Acme's virtual account with no bill named.
By default the transfer settles the open bills in the school's receipt
allocation order as it is booked, and anything over waits as credit. A school
that turns "Apply bank transfers to open bills automatically" off keeps every
transfer as credit for somebody to allocate.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from vs_finance.models import Account, FinanceBankingSettings, PaymentAllocation

from . import services
from .tests import _PaymentsFixtureMixin


class TransferAutoApplyTests(_PaymentsFixtureMixin, TestCase):
    def setUp(self):
        self.entity, self.customer, _ = self.build()
        self.va = services.create_virtual_account(
            entity=self.entity, customer=self.customer, provider="FAKE",
            deposit_account=Account.objects.get(entity=self.entity, code="1110"),
        )
        self.first = self.make_posted_invoice(self.entity, self.customer, amount=3_000_000)
        self.second = self.make_posted_invoice(self.entity, self.customer, amount=2_000_000)
        type(self.second).objects.filter(pk=self.second.pk).update(
            due_date=self.first.due_date + datetime.timedelta(days=7),
        )

    def transfer(self, amount, reference="DEP-1"):
        intent, _ = services.record_virtual_account_deposit(
            virtual_account=self.va, reference=reference, amount=amount,
        )
        self.fake.forced_status[reference] = "SUCCEEDED"
        self.fake.forced_amount[reference] = amount
        return services.confirm_collection(intent)

    def applied(self, invoice):
        return sum(
            PaymentAllocation.objects.filter(invoice=invoice).values_list("amount", flat=True)
        )

    def set_policy(self, **values):
        FinanceBankingSettings.objects.update_or_create(entity=self.entity, defaults=values)

    def test_a_transfer_settles_the_bill_due_first_then_the_next(self):
        self.transfer(4_000_000)
        self.assertEqual(self.applied(self.first), 3_000_000)
        self.assertEqual(self.applied(self.second), 1_000_000)

    def test_a_transfer_over_the_bills_settles_both_and_keeps_the_rest_as_credit(self):
        self.transfer(6_000_000)
        self.assertEqual(self.applied(self.first), 3_000_000)
        self.assertEqual(self.applied(self.second), 2_000_000)

    def test_the_largest_balance_first_order_is_followed(self):
        self.set_policy(default_receipt_allocation_strategy="largest")
        self.transfer(2_000_000)
        self.assertEqual(self.applied(self.first), 2_000_000)
        self.assertEqual(self.applied(self.second), 0)

    def test_a_school_that_turns_it_off_keeps_the_transfer_as_credit(self):
        self.set_policy(apply_transfers_automatically=False)
        self.transfer(4_000_000)
        self.assertEqual(self.applied(self.first), 0)
        self.assertEqual(self.applied(self.second), 0)
