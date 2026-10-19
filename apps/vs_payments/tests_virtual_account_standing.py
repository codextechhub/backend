"""Dedicated accounts the provider no longer holds for their customer.

Acme Ltd's number is handed to another merchant's customer overnight. The daily
check finds it missing from the provider's list, confirms it on its own, switches
it off and issues Acme a new one; a number still Acme's is left alone, and a
provider that answers badly changes nothing.
"""
from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from rest_framework.exceptions import ValidationError

from vs_finance.models import Account, Customer

from . import services
from .constants import VirtualAccountStatus
from .exceptions import ProviderError
from .models import VirtualAccount
from .providers.paystack import PaystackProvider
from .tests import _PaymentsFixtureMixin
from .virtual_account_standing import check_virtual_accounts


class WithdrawnVirtualAccountTests(_PaymentsFixtureMixin, TestCase):
    def setUp(self):
        self.entity, self.customer, _ = self.build()
        self.fake.reports_virtual_accounts = True
        self.va = services.create_virtual_account(entity=self.entity, customer=self.customer, provider="FAKE")

    def more_customers(self, count):
        receivable = Account.objects.get(entity=self.entity, code="1200")
        return [
            services.create_virtual_account(
                entity=self.entity, provider="FAKE",
                customer=Customer.objects.create(
                    entity=self.entity, code=f"C{n}", name=f"Customer {n}",
                    billing_email=f"c{n}@acme.test", receivable_account=receivable),
            )
            for n in range(count)
        ]

    def test_a_number_still_held_for_its_customer_is_left_alone(self):
        summary = check_virtual_accounts()

        self.va.refresh_from_db()
        self.assertEqual((summary["checked"], summary["withdrawn"]), (1, []))
        self.assertEqual(self.va.status, VirtualAccountStatus.ACTIVE)

    def test_a_number_given_to_somebody_else_is_switched_off_and_replaced(self):
        self.fake.virtual_accounts[self.va.provider_reference] = "FAKE-CUS-a-stranger"

        summary = check_virtual_accounts()

        self.va.refresh_from_db()
        new = self.va.replaced_by
        self.assertEqual(summary["withdrawn"], [self.va.pk])
        self.assertEqual(summary["replaced"], [new.pk])
        self.assertEqual(self.va.status, VirtualAccountStatus.INACTIVE)
        self.assertEqual((new.status, new.customer_id), (VirtualAccountStatus.ACTIVE, self.customer.pk))
        self.assertNotEqual(new.provider_reference, self.va.provider_reference)
        self.assertEqual(new.raw["customer"]["email"], "ar@acme.test")

    def test_listeners_hear_of_the_replacement_once_it_commits(self):
        from .signals import virtual_account_replaced

        heard = []

        def listen(sender, old, new, **kwargs):
            heard.append((old.pk, new.pk if new else None))

        virtual_account_replaced.connect(listen, dispatch_uid="test.listen")
        self.addCleanup(virtual_account_replaced.disconnect, dispatch_uid="test.listen")
        self.fake.closed_virtual_accounts.add(self.va.provider_reference)

        with self.captureOnCommitCallbacks(execute=True):
            check_virtual_accounts()

        self.va.refresh_from_db()
        self.assertEqual(heard, [(self.va.pk, self.va.replaced_by_id)])

    def test_a_closed_number_is_replaced(self):
        self.fake.closed_virtual_accounts.add(self.va.provider_reference)

        check_virtual_accounts()

        self.va.refresh_from_db()
        self.assertEqual(self.va.status, VirtualAccountStatus.INACTIVE)
        self.assertIsNotNone(self.va.replaced_by)

    def test_a_replaced_number_cannot_be_switched_back_on(self):
        self.fake.closed_virtual_accounts.add(self.va.provider_reference)
        check_virtual_accounts()
        self.va.refresh_from_db()

        with self.assertRaises(ValidationError):
            services.set_virtual_account_status(self.va, status=VirtualAccountStatus.ACTIVE)

    def test_a_refused_replacement_still_switches_the_lost_number_off(self):
        self.fake.closed_virtual_accounts.add(self.va.provider_reference)

        with patch.object(self.fake, "create_virtual_account", side_effect=ProviderError("down")):
            summary = check_virtual_accounts()

        self.va.refresh_from_db()
        self.assertEqual((summary["withdrawn"], summary["replaced"]), ([self.va.pk], []))
        self.assertEqual(self.va.status, VirtualAccountStatus.INACTIVE)
        self.assertFalse(VirtualAccount.objects.filter(
            customer=self.customer, status=VirtualAccountStatus.ACTIVE).exists())

    def test_a_number_missing_from_the_list_but_confirmed_is_left_alone(self):
        with patch.object(self.fake, "open_virtual_accounts", return_value={}):
            summary = check_virtual_accounts()

        self.va.refresh_from_db()
        self.assertEqual(summary["withdrawn"], [])
        self.assertEqual(self.va.status, VirtualAccountStatus.ACTIVE)

    def test_a_provider_that_cannot_list_changes_nothing(self):
        with patch.object(self.fake, "open_virtual_accounts", side_effect=ProviderError("short")):
            summary = check_virtual_accounts()

        self.va.refresh_from_db()
        self.assertEqual(summary["unanswered"], ["FAKE"])
        self.assertEqual(self.va.status, VirtualAccountStatus.ACTIVE)

    def test_a_run_that_would_replace_too_many_replaces_none(self):
        others = self.more_customers(5)
        self.fake.closed_virtual_accounts.update(va.provider_reference for va in [self.va, *others])

        summary = check_virtual_accounts()

        self.assertEqual(summary["refused"], [{"provider": "FAKE", "suspects": 6, "of": 6}])
        self.assertEqual(summary["withdrawn"], [])
        self.assertEqual(VirtualAccount.objects.filter(status=VirtualAccountStatus.ACTIVE).count(), 6)

    def test_a_provider_that_does_not_report_is_not_checked(self):
        self.fake.reports_virtual_accounts = False

        self.assertEqual(check_virtual_accounts()["checked"], 0)


_HTTP = "vs_payments.providers.paystack.request_json"


class PaystackStandingTests(SimpleTestCase):
    """The Paystack adapter reads its list completely or not at all."""

    provider = PaystackProvider(secret_key="sk_test_x")

    def page(self, rows, page, page_count, total):
        return {"status": True, "data": rows, "meta": {"page": page, "pageCount": page_count, "total": total}}

    def test_every_page_is_read(self):
        replies = [
            self.page([{"id": 1, "customer": {"customer_code": "CUS_a"}}], 1, 2, 2),
            self.page([{"id": 2, "customer": {"customer_code": "CUS_b"}}], 2, 2, 2),
        ]
        with patch(_HTTP, side_effect=replies) as http:
            owners = self.provider.open_virtual_accounts()

        self.assertEqual(owners, {"1": "CUS_a", "2": "CUS_b"})
        self.assertIn("active=true", http.call_args_list[0].args[1])
        self.assertIn("page=2", http.call_args_list[1].args[1])

    def test_a_short_listing_is_refused(self):
        with patch(_HTTP, return_value=self.page([{"id": 1, "customer": {}}], 1, 1, 3)):
            with self.assertRaises(ProviderError):
                self.provider.open_virtual_accounts()

    def test_one_account_reads_its_owner(self):
        reply = {"status": True, "data": {"id": 7, "account_number": "1238472659", "active": True,
                                          "assigned": True, "customer": {"customer_code": "CUS_other"}}}
        with patch(_HTTP, return_value=reply):
            standing = self.provider.virtual_account_standing(provider_reference="7")

        self.assertEqual((standing.active, standing.customer_code), (True, "CUS_other"))

    def test_an_account_paystack_does_not_know_is_none(self):
        with patch(_HTTP, side_effect=ProviderError("gone", provider="PAYSTACK", provider_code="404")):
            self.assertIsNone(self.provider.virtual_account_standing(provider_reference="7"))

    def test_the_customer_on_a_stored_payload(self):
        raw = {"customer": {"customer_code": "CUS_jks", "email": "cxp-1@example.com"}}
        self.assertEqual(self.provider.virtual_account_customer(raw), ("CUS_jks", "cxp-1@example.com"))
        self.assertEqual(self.provider.virtual_account_customer({}), ("", ""))
