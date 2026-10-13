"""A bursar at a branch desk starts a payment the parent scans and pays on their phone.

Tola keeps Lekki's books. Mrs Adeyemi's Tunde owes a Lekki invoice; Tola opens it,
presses Collect payment, and the screen draws the checkout link as a code. Mrs
Adeyemi pays on her phone and lands on her own school's page, not the staff
Console. The screen polls the collection until it reads paid, and the money books
to Tunde's invoice in Lekki. At Single Site, which runs one branch, the same
flow books to its only branch.
"""
from __future__ import annotations

import itertools
from unittest.mock import patch

from django.test import override_settings

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, InvoiceLine
from vs_finance.receivables import post_invoice
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .constants import CollectionOrigin, CollectionStatus
from .models import CollectionIntent
from .tests_custody import _CustodyFixture

_clerks = itertools.count(1)
_KEYS = ("payments.collection.create", "payments.collection.view")


class DeskCollectionTests(_CustodyFixture):
    def clerk(self, *keys, tenant=None, branch=None):
        n = next(_clerks)
        tenant = tenant or self.tenant
        return TenantAPIClient(user=self.grant(
            self.user_for(tenant, f"desk-{n}@corona.test"), *(keys or _KEYS),
            tenant=tenant, role_key=f"desk-{n}", branch=branch))

    def posted_invoice(self, entity, customer, branch):
        invoice = self.invoice(entity, customer, branch)
        InvoiceLine.objects.filter(invoice=invoice).update(
            revenue_account=Account.objects.get(entity=entity, code="4100"))
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def start(self, client, entity, invoice, **extra):
        return client.post(f"/v1/payments/collections/?entity={entity.code}", {
            "amount": invoice.balance_due, "invoice": invoice.pk, "origin": "DESK", **extra,
        }, format="json")

    def pay(self, intent):
        self.fake.forced_status[intent.reference] = CollectionStatus.SUCCEEDED
        self.fake.forced_amount[intent.reference] = intent.amount
        self.fake.forced_fee[intent.reference] = 0

    def test_a_desk_collection_books_to_the_invoice_and_its_branch(self):
        invoice = self.posted_invoice(self.books, self.adeyemi, self.lekki)
        tola = self.clerk(branch=self.lekki)

        created = self.start(tola, self.books, invoice, payer_email="mum@adeyemi.test")

        self.assertEqual(created.status_code, 201, created.data)
        data = created.data["data"]
        self.assertTrue(data["checkout_url"].startswith("https://"))
        self.assertEqual((data["origin"], data["status"], data["payer_email"]),
                         ("DESK", "PROCESSING", "mum@adeyemi.test"))
        intent = CollectionIntent.objects.get(pk=data["id"])
        self.assertEqual((intent.branch_id, intent.origin, intent.invoice_id),
                         (self.lekki.pk, CollectionOrigin.DESK, invoice.pk))

        self.pay(intent)
        polled = tola.get(f"/v1/payments/collections/{intent.pk}/?verify=1&entity={self.books.code}")
        self.assertEqual(polled.data["data"]["status"], "SUCCEEDED")
        intent.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(intent.payment.branch_id, self.lekki.pk)
        self.assertEqual(invoice.balance_due, 0)

    def test_at_a_one_branch_school_the_desk_collection_books_to_its_branch(self):
        invoice = self.posted_invoice(self.solo_books, self.solo_parent, self.solo_main)
        clerk = self.clerk(tenant=self.solo_tenant)

        created = self.start(clerk, self.solo_books, invoice)

        self.assertEqual(created.status_code, 201, created.data)
        intent = CollectionIntent.objects.get(pk=created.data["data"]["id"])
        self.pay(intent)
        clerk.get(f"/v1/payments/collections/{intent.pk}/?verify=1&entity={self.solo_books.code}")
        intent.refresh_from_db()
        self.assertEqual((intent.status, intent.payment.branch_id),
                         (CollectionStatus.SUCCEEDED, self.solo_main.pk))

    def test_a_direct_tenant_settles_the_desk_payment_to_the_branchs_bank(self):
        self.settle_directly()
        invoice = self.posted_invoice(self.books, self.adeyemi, self.lekki)

        created = self.start(self.clerk(branch=self.lekki), self.books, invoice)

        self.assertEqual(created.status_code, 201, created.data)
        reference = created.data["data"]["reference"]
        self.assertEqual(self.fake.subaccounts_named[reference], "ACCT_LEK")

    def test_without_the_create_permission_it_is_refused(self):
        invoice = self.posted_invoice(self.books, self.adeyemi, self.lekki)
        refused = self.start(self.clerk("payments.collection.view", branch=self.lekki),
                             self.books, invoice)
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_another_tenants_user_cannot_start_a_collection_in_these_books(self):
        invoice = self.posted_invoice(self.books, self.adeyemi, self.lekki)
        refused = self.start(self.clerk(tenant=self.rival_tenant), self.books, invoice)
        self.assertIn(refused.status_code, (403, 404), refused.data)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_a_branch_clerk_cannot_collect_another_branchs_invoice(self):
        invoice = self.posted_invoice(self.books, self.okafor, self.ikeja)
        refused = self.start(self.clerk(branch=self.lekki), self.books, invoice)
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_the_payer_email_is_checked_and_sent_to_the_provider(self):
        invoice = self.posted_invoice(self.books, self.adeyemi, self.lekki)
        tola = self.clerk(branch=self.lekki)

        refused = self.start(tola, self.books, invoice, payer_email="not-an-email")
        self.assertEqual(refused.status_code, 400, refused.data)

        with patch.object(self.fake, "create_checkout",
                          wraps=self.fake.create_checkout) as checkout:
            self.start(tola, self.books, invoice, payer_email="mum@adeyemi.test")
        self.assertEqual(checkout.call_args.kwargs["customer_email"], "mum@adeyemi.test")
        self.assertEqual(checkout.call_args.kwargs["metadata"]["origin"], "DESK")

    def test_an_unknown_origin_is_refused(self):
        invoice = self.posted_invoice(self.books, self.adeyemi, self.lekki)
        refused = self.start(self.clerk(branch=self.lekki), self.books, invoice, origin="KIOSK")
        self.assertEqual(refused.status_code, 400, refused.data)

    def test_the_list_tells_desk_payments_apart(self):
        tola = self.clerk(branch=self.lekki)
        desk = self.start(tola, self.books, self.posted_invoice(self.books, self.adeyemi, self.lekki))
        other = self.start(tola, self.books, self.posted_invoice(self.books, self.adeyemi, self.lekki),
                           origin="STANDARD")
        self.assertEqual(other.data["data"]["origin"], "STANDARD")

        listed = tola.get(f"/v1/payments/collections/?origin=DESK&entity={self.books.code}")
        self.assertEqual([row["id"] for row in listed.data["data"]], [desk.data["data"]["id"]])

    def test_a_role_that_may_not_read_the_origin_never_sees_it(self):
        install_declared_fields("payments.collection")
        invoice = self.posted_invoice(self.books, self.adeyemi, self.lekki)
        tola = self.clerk(branch=self.lekki)
        role = TenantRoleTemplate.objects.get(tenant=self.tenant, key=f"desk-{next(_clerks) - 1}")
        set_field_access(role, "payments.collection.origin", read=False, write=False)

        created = self.start(tola, self.books, invoice)

        self.assertEqual(created.status_code, 201, created.data)
        self.assertNotIn("origin", created.data["data"])
        self.assertEqual(CollectionIntent.objects.get().origin, CollectionOrigin.DESK)


@override_settings(SCHOOL_APP_BASE_URL="https://xvs.codexng.com", PAYMENTS_CALLBACK_URL="",
                   FRONTEND_BASE_URL="https://console.codexng.com")
class PayerReturnUrlTests(_CustodyFixture):
    """The checkout returns a parent to their own school's host, never the staff Console."""

    def return_url(self, entity, **kwargs):
        with patch.object(self.fake, "create_checkout",
                          wraps=self.fake.create_checkout) as checkout:
            from . import services

            services.initiate_collection(entity=entity, amount=5_000, **kwargs)
        return checkout.call_args.kwargs["callback_url"]

    def test_a_staff_checkout_for_an_invoice_returns_to_the_tenants_school_host(self):
        invoice = self.invoice(self.books, self.adeyemi, self.lekki)
        InvoiceLine.objects.filter(invoice=invoice).update(
            revenue_account=Account.objects.get(entity=self.books, code="4100"))
        post_invoice(invoice)
        invoice.refresh_from_db()
        self.assertEqual(self.return_url(self.books, invoice=invoice),
                         "https://fin-multi.xvs.codexng.com/payments/return")

    def test_a_checkout_naming_only_a_customer_returns_to_that_tenants_host_too(self):
        self.assertEqual(self.return_url(self.solo_books, customer=self.solo_parent),
                         "https://fin-solo.xvs.codexng.com/payments/return")

    def test_an_explicit_return_address_wins(self):
        self.assertEqual(
            self.return_url(self.books, customer=self.adeyemi, callback_url="https://x.test/back"),
            "https://x.test/back")

    @override_settings(SCHOOL_APP_BASE_URL="", PAYMENTS_CALLBACK_URL="https://pinned.test/return")
    def test_with_no_school_host_the_environments_pinned_address_applies(self):
        self.assertEqual(self.return_url(self.books, customer=self.adeyemi),
                         "https://pinned.test/return")

    @override_settings(SCHOOL_APP_BASE_URL="")
    def test_with_nothing_else_it_is_the_staff_frontend(self):
        self.assertEqual(self.return_url(self.books, customer=self.adeyemi),
                         "https://console.codexng.com/payments/return")
