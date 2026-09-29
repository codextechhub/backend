"""A procurement document's activity says who really acted under a proxy.

Ada Obi proxies Chioma Okafor and issues a request for quotation, or posts a
vendor payment. The document's activity lists the event with ``actor_name``
"Ada Obi", as it always has, and adds that she acted for Chioma: "Ada Obi for
Chioma Okafor". An event Chioma records herself reads "Chioma Okafor".
"""
from __future__ import annotations

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase

from vs_admin_console.models import ImpersonationSession
from vs_finance.audit import record
from vs_finance.constants import FinanceAuditAction
from vs_finance.models import LedgerEntity
from vs_finance.seed import seed_currencies
from vs_procurement.serializers import _sourcing_activity
from vs_tenants.context import clear_request_context, set_current_audit_identity
from vs_tenants.models import Tenant

EXISTING_ACTIVITY_KEYS = {"id", "action", "message", "status", "actor_name", "created_at"}


class _ProxyFixture(TestCase):
    """Books, Chioma Okafor, and Ada Obi holding a proxy session for her."""

    def setUp(self):
        seed_currencies()
        tenant = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        self.entity = LedgerEntity.objects.create(
            name="Proxy Books", code="PRXBK", kind=LedgerEntity.Kind.TENANT, tenant=tenant,
        )
        User = get_user_model()
        self.chioma = User.objects.create_user(
            email="chioma-proc-proxy@test.com", tenant=tenant,
            first_name="Chioma", last_name="Okafor",
        )
        self.ada = User.objects.create_user(
            email="ada-proc-proxy@test.com", tenant=tenant,
            first_name="Ada", last_name="Obi",
        )
        self.session = ImpersonationSession.objects.create(
            staff_user=self.ada, tenant=tenant, target_user=self.chioma,
            justification="Covering purchasing.",
        )

    def tearDown(self):
        clear_request_context()

    def proxy_as_chioma(self):
        set_current_audit_identity(
            actor_user=self.ada, effective_user=self.chioma,
            impersonation_session=self.session,
        )


class SourcingActivityProxyTests(_ProxyFixture):

    def issue(self, actor_user):
        record(
            entity=self.entity, action=FinanceAuditAction.JOURNAL_POSTED,
            actor_user=actor_user, target_type="RequestForQuotation", target_id="7",
            message="Issued the request for quotation.", mirror=False,
        )
        clear_request_context()
        rows = _sourcing_activity(self.entity, "RequestForQuotation", 7)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_a_proxied_event_reads_ada_for_chioma(self):
        self.proxy_as_chioma()

        row = self.issue(self.chioma)

        self.assertEqual(row["actor_name"], "Ada Obi")
        self.assertEqual(row["acted_label"], "Ada Obi for Chioma Okafor")
        self.assertEqual(row["real_actor_name"], "Ada Obi")
        self.assertEqual(row["proxied_user_name"], "Chioma Okafor")
        self.assertTrue(EXISTING_ACTIVITY_KEYS <= set(row))

    def test_an_event_in_person_reads_only_the_actor(self):
        row = self.issue(self.chioma)

        self.assertEqual(row["actor_name"], "Chioma Okafor")
        self.assertEqual(row["acted_label"], "Chioma Okafor")
        self.assertIsNone(row["real_actor_name"])
        self.assertIsNone(row["proxied_user_name"])
        self.assertTrue(EXISTING_ACTIVITY_KEYS <= set(row))


class VendorPaymentActivityProxyTests(_ProxyFixture):
    """The vendor payment drawer's activity, read through the detail overlay."""

    def setUp(self):
        super().setUp()
        from vs_finance.models import Account
        from vs_finance.seed import seed_chart_of_accounts
        from vs_procurement.models import Vendor, VendorPayment

        seed_chart_of_accounts(self.entity)
        vendor = Vendor.objects.create(
            entity=self.entity, code="SUPP1", name="Supplier Ltd",
            payable_account=Account.objects.get(entity=self.entity, code="2100"),
        )
        self.payment = VendorPayment.objects.create(
            entity=self.entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=30_000, wht_amount=0, net_amount=30_000,
            payment_account=Account.objects.get(entity=self.entity, code="1100"),
        )

    def post(self, actor_user):
        from vs_procurement.views.vendor_payments import _serialize_detail

        record(
            entity=self.entity, action=FinanceAuditAction.JOURNAL_POSTED,
            actor_user=actor_user, target_type="VendorPayment", target_id=str(self.payment.pk),
            message="Posted the vendor payment.", mirror=False,
        )
        clear_request_context()
        rows = _serialize_detail(self.payment)["activity"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_a_proxied_event_reads_ada_for_chioma(self):
        self.proxy_as_chioma()

        row = self.post(self.chioma)

        self.assertEqual(row["actor_name"], "Ada Obi")
        self.assertEqual(row["acted_label"], "Ada Obi for Chioma Okafor")
        self.assertEqual(row["real_actor_name"], "Ada Obi")
        self.assertEqual(row["proxied_user_name"], "Chioma Okafor")
        self.assertTrue(EXISTING_ACTIVITY_KEYS <= set(row))

    def test_an_event_in_person_reads_only_the_actor(self):
        row = self.post(self.chioma)

        self.assertEqual(row["actor_name"], "Chioma Okafor")
        self.assertEqual(row["acted_label"], "Chioma Okafor")
        self.assertIsNone(row["proxied_user_name"])
        self.assertTrue(EXISTING_ACTIVITY_KEYS <= set(row))
