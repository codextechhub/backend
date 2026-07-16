from django.test import TestCase

from vs_admin_console.models import ImpersonationSession
from vs_tenants.context import (
    clear_request_context,
    get_current_audit_identity,
    set_current_audit_identity,
)
from vs_tenants.models import Tenant
from vs_user.models import User

from .models import AuditActorType
from .services import emit_audit_event


class ProxiedAuditAttributionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        self.proxier = User.objects.create_user(
            email="audit-proxier@codex.test",
            password="Str0ng!pass123",
            first_name="Audit",
            last_name="Proxier",
            user_type="CX_STAFF",
            status="ACTIVE",
        )
        self.target = User.objects.create_user(
            email="audit-target@codex.test",
            password="Str0ng!pass123",
            first_name="Proxy",
            last_name="Target",
            user_type="CX_STAFF",
            status="ACTIVE",
        )
        self.third_party = User.objects.create_user(
            email="audit-third-party@codex.test",
            password="Str0ng!pass123",
            first_name="Third",
            last_name="Party",
            user_type="CX_STAFF",
            status="ACTIVE",
        )
        self.session = ImpersonationSession.objects.create(
            staff_user=self.proxier,
            tenant=self.tenant,
            target_user=self.target,
            justification="Audit attribution test",
        )

    def tearDown(self):
        clear_request_context()

    def _emit(self, actor_user):
        return emit_audit_event(
            module_key="CONFIG",
            action_type="UPDATE",
            entity_type="Setting",
            entity_id="timezone",
            entity_label="Timezone",
            actor_user=actor_user,
            tenant=self.tenant,
        )

    def test_effective_target_is_rewritten_to_real_proxier(self):
        set_current_audit_identity(
            actor_user=self.proxier,
            effective_user=self.target,
            impersonation_session=self.session,
        )

        event = self._emit(self.target)

        self.assertEqual(event.actor_user, self.proxier)
        self.assertEqual(event.effective_user, self.target)
        self.assertEqual(event.impersonation_session, self.session)
        self.assertIn(self.proxier.full_name, event.summary)
        self.assertNotIn(self.target.full_name, event.summary)

    def test_explicit_real_actor_receives_the_same_proxy_context(self):
        set_current_audit_identity(
            actor_user=self.proxier,
            effective_user=self.target,
            impersonation_session=self.session,
        )

        event = self._emit(self.proxier)

        self.assertEqual(event.actor_user, self.proxier)
        self.assertEqual(event.effective_user, self.target)
        self.assertEqual(event.impersonation_session, self.session)

    def test_authoritative_module_audit_uses_proxier_and_preserves_target_metadata(self):
        from vs_rbac.audit import record_rbac_audit

        set_current_audit_identity(
            actor_user=self.proxier,
            effective_user=self.target,
            impersonation_session=self.session,
        )

        log = record_rbac_audit(
            action_type="ROLE_CHANGED",
            entity_type="TenantRoleTemplate",
            entity_id="support-agent",
            actor_user=self.target,
            metadata={"source": "test"},
        )

        self.assertEqual(log.actor, self.proxier)
        self.assertEqual(log.metadata["effective_user_id"], self.target.pk)
        self.assertEqual(log.metadata["impersonation_session_id"], self.session.pk)
        mirrored = self.proxier.performed_audit_events.get(
            entity_type="TenantRoleTemplate", entity_id="support-agent",
        )
        self.assertEqual(mirrored.effective_user, self.target)
        self.assertEqual(mirrored.impersonation_session, self.session)

    def test_third_party_and_system_events_are_not_re_attributed(self):
        set_current_audit_identity(
            actor_user=self.proxier,
            effective_user=self.target,
            impersonation_session=self.session,
        )

        third_party_event = self._emit(self.third_party)
        system_event = self._emit(None)

        self.assertEqual(third_party_event.actor_user, self.third_party)
        self.assertIsNone(third_party_event.effective_user)
        self.assertIsNone(third_party_event.impersonation_session)
        self.assertEqual(system_event.actor_type, AuditActorType.SYSTEM)
        self.assertIsNone(system_event.actor_user)

    def test_clearing_request_context_removes_dual_identity(self):
        set_current_audit_identity(
            actor_user=self.proxier,
            effective_user=self.target,
            impersonation_session=self.session,
        )

        clear_request_context()

        self.assertEqual(get_current_audit_identity(), (None, None, None))
