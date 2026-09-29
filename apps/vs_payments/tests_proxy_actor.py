"""The transactions log says who really acted under a proxy.

Ada Obi proxies Chioma Okafor and initiates a payout. The gateway event runs in
Chioma's name, so ``actor_email`` stays Chioma's, and the event also records
Ada as ``proxied_by``: the log reads "Ada Obi for Chioma Okafor". An event
Chioma runs herself reads "Chioma Okafor".
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from vs_admin_console.models import ImpersonationSession
from vs_payments.audit import record
from vs_payments.constants import PaymentAuditAction
from vs_payments.serializers import PaymentEventSerializer
from vs_tenants.context import clear_request_context, set_current_audit_identity
from vs_tenants.models import Tenant

EXISTING_EVENT_KEYS = {
    "id", "entity_code", "provider", "action", "action_display", "reference",
    "succeeded", "message", "metadata", "actor_email", "created_at",
}


class PaymentEventProxyTests(TestCase):

    def setUp(self):
        tenant = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        User = get_user_model()
        self.chioma = User.objects.create_user(
            email="chioma-pay-proxy@test.com", tenant=tenant,
            first_name="Chioma", last_name="Okafor",
        )
        self.ada = User.objects.create_user(
            email="ada-pay-proxy@test.com", tenant=tenant,
            first_name="Ada", last_name="Obi",
        )
        self.session = ImpersonationSession.objects.create(
            staff_user=self.ada, tenant=tenant, target_user=self.chioma,
            justification="Covering payouts.",
        )
        self.action = PaymentAuditAction.values[0]

    def tearDown(self):
        clear_request_context()

    def test_a_proxied_event_records_and_shows_ada_for_chioma(self):
        set_current_audit_identity(
            actor_user=self.ada, effective_user=self.chioma,
            impersonation_session=self.session,
        )

        event = record(action=self.action, reference="PO-1", actor_user=self.chioma)
        clear_request_context()
        row = PaymentEventSerializer(event).data

        self.assertEqual(event.actor_user_id, self.chioma.pk)
        self.assertEqual(event.proxied_by_id, self.ada.pk)
        self.assertEqual(row["actor_email"], self.chioma.email)
        self.assertEqual(row["acted_label"], "Ada Obi for Chioma Okafor")
        self.assertEqual(row["real_actor_name"], "Ada Obi")
        self.assertEqual(row["proxied_user_name"], "Chioma Okafor")
        self.assertTrue(EXISTING_EVENT_KEYS <= set(row))

    def test_an_event_in_person_shows_only_the_actor(self):
        event = record(action=self.action, reference="PO-2", actor_user=self.chioma)
        row = PaymentEventSerializer(event).data

        self.assertIsNone(event.proxied_by_id)
        self.assertEqual(row["acted_label"], "Chioma Okafor")
        self.assertIsNone(row["real_actor_name"])
        self.assertIsNone(row["proxied_user_name"])
        self.assertTrue(EXISTING_EVENT_KEYS <= set(row))
