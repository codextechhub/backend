"""A procurement document's activity says who really acted under a proxy.

Ada Obi proxies Chioma Okafor and issues a request for quotation. The RFQ's
activity lists the event with ``actor_name`` "Ada Obi", as it always has, and
adds that she acted for Chioma: "Ada Obi for Chioma Okafor". An event Chioma
records herself reads "Chioma Okafor".
"""
from __future__ import annotations

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


class SourcingActivityProxyTests(TestCase):

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
        set_current_audit_identity(
            actor_user=self.ada, effective_user=self.chioma,
            impersonation_session=self.session,
        )

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
