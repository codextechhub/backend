"""The finance trail says who really acted when a colleague proxies another.

Ada Obi opens a proxy session as Chioma Okafor, Bright Star's bursar, and posts
a journal. The post is valid in Chioma's name, so the journal's ``posted_by``
is Chioma. The audit row names Ada as the actor and Chioma as the person she
acted for, and the trail reads "Ada Obi for Chioma Okafor". When Chioma posts
for herself the trail reads "Chioma Okafor" and nothing else.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus, FinanceAuditAction
from vs_finance.models import FinanceAuditLog, JournalEntry, JournalLine, Account
from vs_tenants.context import clear_request_context, set_current_audit_identity

from .tests_direct_post_confirmation import _DirectPostFixture

EXISTING_TRAIL_KEYS = {
    "id", "action", "action_display", "status", "actor", "target_type",
    "target_id", "document_number", "message", "before", "after", "created_at",
}


class _ProxyFixture(_DirectPostFixture):
    """Bright Star with Chioma the bursar and Ada, a colleague proxying her."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from vs_admin_console.models import ImpersonationSession
        from vs_user.tokens import CodeXRefreshToken

        super().setUp()
        self.bursar.first_name, self.bursar.last_name = "Chioma", "Okafor"
        self.bursar.save(update_fields=["first_name", "last_name"])
        self.ada = get_user_model().objects.create_user(
            email="ada-proxy@test.com", password="pw", status="ACTIVE",
            first_name="Ada", last_name="Obi", tenant=self.tenant, branch=self.bursar.branch,
        )
        self.session = ImpersonationSession.objects.create(
            staff_user=self.ada, tenant=self.tenant, target_user=self.bursar,
            justification="Covering the bursar's desk.",
        )
        self.proxy_client = TenantAPIClient(user=self.ada, tenant_slug=self.tenant.slug)
        self.proxy_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(self.ada).access_token}",
            HTTP_X_IMPERSONATION_SESSION=str(self.session.pk),
        )

    def tearDown(self):
        clear_request_context()
        super().tearDown()

    def draft(self):
        entry = JournalEntry.objects.create(
            entity=self.entity, date=datetime.date(2026, 1, 15), period=self.period,
            narration="Accrual", created_by=self.bursar,
        )
        JournalLine.objects.create(
            entry=entry, line_no=1, debit=50_000, credit=0,
            account=Account.objects.get(entity=self.entity, code="1100"))
        JournalLine.objects.create(
            entry=entry, line_no=2, debit=0, credit=50_000,
            account=Account.objects.get(entity=self.entity, code="4100"))
        return entry

    def post_journal(self, client, entry):
        return client.post(
            f"/v1/finance/journals/{entry.pk}/post/?entity={self.entity.code}",
            {}, format="json",
        )

    def posted_log(self, entry):
        return FinanceAuditLog.objects.get(
            action=FinanceAuditAction.JOURNAL_POSTED,
            target_type="JournalEntry", target_id=str(entry.pk),
        )

    def trail_row(self, log):
        response = self.client.get(f"/v1/finance/audit-logs/?entity={self.entity.code}")
        self.assertEqual(response.status_code, 200, response.content)
        rows = [row for row in response.json()["data"] if row["id"] == log.pk]
        self.assertEqual(len(rows), 1, response.content)
        return rows[0]


class ProxiedPostTests(_ProxyFixture):
    """A post made under a proxy records and shows both people."""

    def test_the_audit_row_names_the_real_person_and_whom_she_acted_for(self):
        entry = self.draft()

        response = self.post_journal(self.proxy_client, entry)

        self.assertEqual(response.status_code, 200, response.content)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.POSTED)
        self.assertEqual(entry.posted_by_id, self.bursar.pk)
        log = self.posted_log(entry)
        self.assertEqual(log.actor_id, self.ada.pk)
        self.assertEqual(log.effective_user_id, self.bursar.pk)

    def test_the_trail_reads_ada_for_chioma(self):
        entry = self.draft()
        self.post_journal(self.proxy_client, entry)

        row = self.trail_row(self.posted_log(entry))

        self.assertEqual(row["acted_label"], "Ada Obi for Chioma Okafor")
        self.assertEqual(row["real_actor_name"], "Ada Obi")
        self.assertEqual(row["proxied_user_name"], "Chioma Okafor")
        self.assertEqual(row["actor"], self.ada.email)
        self.assertTrue(EXISTING_TRAIL_KEYS <= set(row))
        self.assertNotIn("metadata", row)


class UnproxiedPostTests(_ProxyFixture):
    """A post made in person shows only the person who made it."""

    def test_the_trail_shows_only_the_actor(self):
        entry = self.draft()

        response = self.post_journal(self.client, entry)

        self.assertEqual(response.status_code, 200, response.content)
        log = self.posted_log(entry)
        self.assertEqual(log.actor_id, self.bursar.pk)
        self.assertIsNone(log.effective_user_id)
        row = self.trail_row(log)
        self.assertEqual(row["acted_label"], "Chioma Okafor")
        self.assertIsNone(row["real_actor_name"])
        self.assertIsNone(row["proxied_user_name"])
        self.assertEqual(row["actor"], self.bursar.email)
        self.assertTrue(EXISTING_TRAIL_KEYS <= set(row))


class RecordAttributionTests(_ProxyFixture):
    """``record`` stamps the pair only on rows the proxy itself produced."""

    def _record(self, actor_user):
        from vs_finance.audit import record

        return record(
            entity=self.entity, action=FinanceAuditAction.JOURNAL_POSTED,
            actor_user=actor_user, target_type="JournalEntry", target_id="0",
        )

    def test_a_row_stamped_with_a_third_party_is_not_attributed_to_the_proxy(self):
        from django.contrib.auth import get_user_model

        third = get_user_model().objects.create_user(
            email="third-proxy@test.com", password="pw", status="ACTIVE",
            first_name="Third", last_name="Party", tenant=self.tenant,
            branch=self.bursar.branch,
        )
        set_current_audit_identity(
            actor_user=self.ada, effective_user=self.bursar,
            impersonation_session=self.session,
        )

        log = self._record(third)

        self.assertEqual(log.actor_id, third.pk)
        self.assertIsNone(log.effective_user_id)

    def test_a_system_row_under_a_proxy_stays_a_system_row(self):
        set_current_audit_identity(
            actor_user=self.ada, effective_user=self.bursar,
            impersonation_session=self.session,
        )

        log = self._record(None)

        self.assertIsNone(log.actor_id)
        self.assertIsNone(log.effective_user_id)
