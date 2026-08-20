from django.http import QueryDict
from django.test import TestCase

from vs_admin_console.models import ImpersonationSession
from vs_tenants.context import (
    clear_request_context,
    get_current_audit_identity,
    set_current_audit_identity,
)
from vs_tenants.models import Tenant
from vs_user.models import User

from .models import (
    AuditActionType,
    AuditActorType,
    AuditEvent,
    AuditModuleKey,
    AuditSeverity,
    AuditStatus,
)
from .serializers import AuditEventFilterSerializer
from .services import emit_audit_event
from .views import apply_audit_event_filters


class AuditEventFilterContractTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(
            email="procurement.auditor@example.test",
            password="Str0ng!pass123",
            first_name="Priya",
            last_name="Buyer",
            user_type="CX_STAFF",
            status="ACTIVE",
        )

        self.procurement_failed = AuditEvent.objects.create(
            module_key=AuditModuleKey.PROCUREMENT,
            action_type=AuditActionType.PROCUREMENT_ACTION,
            severity=AuditSeverity.WARNING,
            status=AuditStatus.FAILED,
            actor_type=AuditActorType.USER,
            actor_user=self.actor,
            entity_type="PurchaseOrder",
            entity_id="PO-404",
            entity_label="Missing purchase order",
            summary="Purchase order approval failed",
        )
        self.export_denied = AuditEvent.objects.create(
            module_key=AuditModuleKey.EXPORTS,
            action_type=AuditActionType.EXPORT_FILE_DOWNLOAD_REFUSED,
            severity=AuditSeverity.CRITICAL,
            status=AuditStatus.DENIED,
            actor_type=AuditActorType.SYSTEM,
            actor_label="Export worker",
            entity_type="ExportRun",
            entity_id="RUN-9",
            entity_label="Sensitive export",
            summary="Export download refused",
        )
        AuditEvent.objects.create(
            module_key=AuditModuleKey.FINANCE,
            action_type=AuditActionType.FINANCIAL_TRANSACTION,
            severity=AuditSeverity.INFO,
            status=AuditStatus.SUCCESS,
            actor_type=AuditActorType.SYSTEM,
            actor_label="Ledger worker",
            entity_type="JournalEntry",
            entity_id="JE-1",
            entity_label="Journal entry",
            summary="Journal posted",
        )

    def test_repeated_query_values_validate_as_lists_and_filter_with_or_semantics(self):
        query = QueryDict(
            "module_key=PROCUREMENT&module_key=EXPORTS&"
            "severity=WARNING&severity=CRITICAL&"
            "status=FAILED&status=DENIED"
        )
        serializer = AuditEventFilterSerializer(data=query)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        filters = serializer.validated_data
        self.assertEqual(filters["module_key"], ["PROCUREMENT", "EXPORTS"])
        self.assertEqual(filters["severity"], ["WARNING", "CRITICAL"])
        self.assertEqual(filters["status"], ["FAILED", "DENIED"])

        ids = set(
            apply_audit_event_filters(AuditEvent.objects.all(), filters)
            .values_list("id", flat=True)
        )
        self.assertEqual(ids, {self.procurement_failed.id, self.export_denied.id})

    def test_scalar_json_values_remain_backward_compatible_and_text_is_trimmed(self):
        serializer = AuditEventFilterSerializer(data={
            "module_key": "PROCUREMENT",
            "entity_type": "  PurchaseOrder  ",
            "search": "  approval  ",
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["module_key"], ["PROCUREMENT"])
        self.assertEqual(serializer.validated_data["entity_type"], "PurchaseOrder")
        self.assertEqual(serializer.validated_data["search"], "approval")

    def test_search_covers_action_code_and_actor_identity(self):
        for term in ("PROCUREMENT_ACTION", "procurement.auditor", "Priya Buyer"):
            with self.subTest(term=term):
                ids = list(
                    apply_audit_event_filters(
                        AuditEvent.objects.all(),
                        {"search": term},
                    ).values_list("id", flat=True)
                )
                self.assertEqual(ids, [self.procurement_failed.id])

    def test_combined_filter_groups_use_and_semantics(self):
        ids = list(
            apply_audit_event_filters(
                AuditEvent.objects.all(),
                {
                    "module_key": ["PROCUREMENT", "EXPORTS"],
                    "status": ["FAILED", "DENIED"],
                    "entity_type": "purchaseorder",
                },
            ).values_list("id", flat=True)
        )

        self.assertEqual(ids, [self.procurement_failed.id])

    def test_filter_enums_include_export_platform_and_procurement_action(self):
        self.assertIn("EXPORTS", AuditModuleKey.values)
        self.assertIn("PLATFORM", AuditModuleKey.values)
        self.assertIn("PROCUREMENT_ACTION", AuditActionType.values)


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


class OnboardingActionTypeRegistrationTests(TestCase):
    """The M9 vocabulary must exist before anything emits it.

    ``action_type`` is validated on save and ``emit_audit_event`` never raises,
    so an unregistered value is swallowed silently and the trail is simply
    absent. These assert the row exists, not that no exception was raised.
    """

    ONBOARDING_ACTION_TYPES = (
        "ONBOARDING_PROVISIONED",
        "ONBOARDING_TASK_COMPLETED",
        "ONBOARDING_TASK_SKIPPED",
        "ONBOARDING_TASK_REOPENED",
        "GO_LIVE_REQUESTED",
        "GO_LIVE_APPROVED",
        "GO_LIVE_REJECTED",
        "GO_LIVE_ACTIVATED",
        "GO_LIVE_FAILED",
    )

    def test_every_onboarding_action_type_is_registered(self):
        registered = set(AuditActionType.values)
        for value in self.ONBOARDING_ACTION_TYPES:
            self.assertIn(value, registered)

    def test_emitting_each_onboarding_action_type_writes_a_row(self):
        for index, value in enumerate(self.ONBOARDING_ACTION_TYPES):
            event = emit_audit_event(
                module_key=AuditModuleKey.ONBOARDING,
                action_type=value,
                entity_type="OnboardingProgress",
                entity_id=str(index + 1),
            )
            self.assertIsNotNone(event, f"{value} was swallowed by emit_audit_event")
            self.assertTrue(
                AuditEvent.objects.filter(
                    module_key=AuditModuleKey.ONBOARDING, action_type=value,
                ).exists(),
                f"No audit row written for {value}",
            )
