import csv
import io
from datetime import timedelta
from unittest import mock

from django.core.files.storage import default_storage
from django.core.management import call_command
from django.http import QueryDict
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import StoredFile
from core.test_utils import TenantAPIClient
from vs_admin_console.models import ImpersonationSession
from vs_tenants.context import (
    clear_request_context,
    get_current_audit_identity,
    set_current_audit_identity,
    set_current_tenant,
)
from vs_tenants.models import Tenant
from vs_user.models import User

from .models import (
    AuditActionType,
    AuditActorType,
    AuditEvent,
    AuditExportJob,
    AuditModuleKey,
    AuditSeverity,
    AuditStatus,
    ExportFormat,
    ExportJobStatus,
)
from .serializers import NO_TENANT, AuditEventFilterSerializer
from .services import emit_audit_event
from .views import (
    EXPORT_CSV_HEADER,
    ExportTooLarge,
    apply_audit_event_filters,
    write_audit_export_file,
)


def _platform_tenant():
    """The one PLATFORM tenant, seeded by vs_tenants migration 0002.

    Being platform staff IS being on this tenant - there is no persona column
    standing in for it any more - so a fixture that wants a CX account names
    the tenant, exactly as production code does.
    """
    from vs_tenants.models import Tenant

    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


class AuditEventFilterContractTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(tenant=_platform_tenant(), 
            email="procurement.auditor@example.test",
            password="Str0ng!pass123",
            first_name="Priya",
            last_name="Buyer",
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


class AuditEventTenantFilterTests(TestCase):
    """Narrowing the trail to one customer - and finding what belongs to none.

    Bright Star and Greenfield both have events; a nightly sweep has one that
    belongs to neither. All three have to be reachable, and none of them may
    leak into another's answer.
    """

    def setUp(self):
        self.bright_star = Tenant.objects.create(
            name="Bright Star School", slug="bright-star",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        self.greenfield = Tenant.objects.create(
            name="Greenfield Academy", slug="greenfield",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        self.bright_star_event = self._event(self.bright_star, "SCH-1")
        self.greenfield_event = self._event(self.greenfield, "SCH-2")
        self.platform_event = self._event(None, "SWEEP-1")

    def _event(self, tenant, entity_id):
        return AuditEvent.objects.create(
            module_key=AuditModuleKey.SCHOOL,
            action_type=AuditActionType.UPDATE,
            actor_type=AuditActorType.SYSTEM,
            actor_label="Sweep worker",
            tenant=tenant,
            entity_type="School",
            entity_id=entity_id,
        )

    def _filtered(self, value):
        serializer = AuditEventFilterSerializer(data={"tenant_slug": value})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return set(
            apply_audit_event_filters(
                AuditEvent.objects.all(), serializer.validated_data,
            ).values_list("id", flat=True)
        )

    def test_the_filter_narrows_to_one_tenant_and_excludes_the_others(self):
        self.assertEqual(self._filtered("bright-star"), {self.bright_star_event.id})
        self.assertEqual(self._filtered("greenfield"), {self.greenfield_event.id})

    def test_events_belonging_to_no_tenant_are_reachable(self):
        self.assertEqual(self._filtered(NO_TENANT), {self.platform_event.id})

    def test_an_unknown_slug_is_refused_rather_than_answered_with_nothing(self):
        # The trap this filter exists to remove: a near-miss slug that quietly
        # reports "nothing happened at Bright Star".
        serializer = AuditEventFilterSerializer(data={"tenant_slug": "bright-star-school"})

        self.assertFalse(serializer.is_valid())
        self.assertIn("tenant_slug", serializer.errors)

    def test_a_slug_is_matched_case_and_padding_insensitively(self):
        self.assertEqual(
            self._filtered("  BRIGHT-STAR  "), {self.bright_star_event.id},
        )

    def test_omitting_the_filter_still_returns_every_tenant(self):
        serializer = AuditEventFilterSerializer(data={})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        ids = set(
            apply_audit_event_filters(
                AuditEvent.objects.all(), serializer.validated_data,
            ).values_list("id", flat=True)
        )
        self.assertEqual(
            ids,
            {self.bright_star_event.id, self.greenfield_event.id, self.platform_event.id},
        )


class AmbientTenantInheritanceTests(TestCase):
    """Where an event's tenant comes from when the caller does not say.

    The filter above is only worth having if the column is populated, and only
    trustworthy if it is populated correctly. These pin both halves: what is
    inherited, and what deliberately is not.
    """

    def setUp(self):
        self.bright_star = Tenant.objects.create(
            name="Bright Star School", slug="bright-star",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        self.greenfield = Tenant.objects.create(
            name="Greenfield Academy", slug="greenfield",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        self.codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)

    def tearDown(self):
        clear_request_context()

    def _emit(self, **kwargs):
        return emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.ROLE_ASSIGNED,
            entity_type="TenantRoleTemplate",
            entity_id="bursar",
            **kwargs,
        )

    def test_an_event_emitted_in_a_tenant_context_carries_it_with_no_caller_change(self):
        # vs_rbac's signals never pass a tenant and are not being edited; the
        # role Bright Star's bursar is granted must still be findable under
        # Bright Star.
        set_current_tenant(self.bright_star)

        event = self._emit()

        self.assertEqual(event.tenant_id, self.bright_star.pk)

    def test_an_explicit_tenant_beats_the_ambient_one(self):
        # The shape that makes this rule load-bearing: a Codex staffer creates
        # Bright Star while asserting ?tenant=codex, and SchoolCreateSerializer
        # names the new school's tenant on the event.
        set_current_tenant(self.codex)

        event = self._emit(tenant=self.bright_star)

        self.assertEqual(event.tenant_id, self.bright_star.pk)

    def test_an_explicit_tenant_beats_a_different_ambient_tenant(self):
        set_current_tenant(self.greenfield)

        event = self._emit(tenant=self.bright_star)

        self.assertEqual(event.tenant_id, self.bright_star.pk)

    def test_with_no_ambient_tenant_the_event_stays_null(self):
        # A Celery task or a management command: nothing to inherit, and
        # inventing one would be worse than leaving it empty.
        clear_request_context()

        event = self._emit()

        self.assertIsNone(event.tenant_id)

    def test_the_platform_tenant_is_not_inherited(self):
        # Asserting ?tenant=codex says who is acting, not whose data is being
        # touched. Stamping codex on it would hide the row from the Bright Star
        # filter AND show it under Codex, which is worse than null.
        set_current_tenant(self.codex)

        event = self._emit()

        self.assertIsNone(event.tenant_id)

    def test_an_inherited_tenant_is_findable_through_the_filter(self):
        set_current_tenant(self.bright_star)
        event = self._emit()
        clear_request_context()

        serializer = AuditEventFilterSerializer(data={"tenant_slug": "bright-star"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        ids = set(
            apply_audit_event_filters(
                AuditEvent.objects.all(), serializer.validated_data,
            ).values_list("id", flat=True)
        )

        self.assertIn(event.id, ids)


class ProxiedAuditAttributionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        self.proxier = User.objects.create_user(tenant=_platform_tenant(), 
            email="audit-proxier@codex.test",
            password="Str0ng!pass123",
            first_name="Audit",
            last_name="Proxier",
            status="ACTIVE",
        )
        self.target = User.objects.create_user(tenant=_platform_tenant(), 
            email="audit-target@codex.test",
            password="Str0ng!pass123",
            first_name="Proxy",
            last_name="Target",
            status="ACTIVE",
        )
        self.third_party = User.objects.create_user(tenant=_platform_tenant(), 
            email="audit-third-party@codex.test",
            password="Str0ng!pass123",
            first_name="Third",
            last_name="Party",
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


# -----------------------------------------------------------------------------
# Audit export files: storage, retrieval and the authorisation around it
# -----------------------------------------------------------------------------

class AuditExportFileFixture:
    """A codex-tenant audit officer, a stranger, and an outsider in another tenant.

    ``officer`` holds ``platform.audit.export``. ``stranger`` holds nothing.
    ``outsider`` holds the very same key, but in a different tenant - which is
    the case that decides whether one school's export is reachable from another.
    """

    def build(self):
        call_command("seed_actions", verbosity=0)
        call_command("seed_platform_permissions", verbosity=0)

        self.tenant = Tenant.objects.get(slug="codex")
        self.other_tenant = Tenant.objects.create(
            name="Bright Star School",
            slug="bright-star",
            kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )

        self.officer = self._user(
            "audit.officer@example.test", role="audit_officer",
            keys=["platform.audit.export", "platform.audit.view"],
        )
        self.stranger = self._user("stranger@example.test", role="no_audit", keys=[])
        self.outsider = self._user(
            "outsider@example.test", role="audit_officer",
            keys=["platform.audit.export", "platform.audit.view"],
            tenant=self.other_tenant,
        )

    def _user(self, email, *, role, keys, tenant=None):
        from vs_rbac.models import (
            Permission, TenantRolePermission, TenantRoleTemplate, TenantUserRoleAssignment,
        )

        tenant = tenant or self.tenant
        user = User.objects.create_user(
            email=email, password="Str0ng!pass123", status="ACTIVE", first_name=email.split("@")[0], last_name="Tester",
            tenant=tenant,
        )
        template, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=tenant, key=role, defaults={"name": role, "status": "ACTIVE"},
        )
        for permission in Permission.objects.filter(key__in=keys):
            TenantRolePermission.objects.get_or_create(
                role=template, permission=permission, defaults={"granted": True},
            )
        TenantUserRoleAssignment.objects.create(
            tenant=tenant, user=user, role=template, assignment_status="ACTIVE",
        )
        return user

    # Seeding and user creation emit audit rows of their own, so every export
    # below is filtered to the entity type this fixture writes.
    FILTER = {"entity_type": "PurchaseOrder"}

    def exported_events(self):
        return AuditEvent.objects.filter(entity_type="PurchaseOrder")

    def make_events(self, count, *, module_key=AuditModuleKey.PROCUREMENT):
        """Write *count* events whose rows are comfortably wide (~200 chars each)."""
        events = [
            AuditEvent(
                module_key=module_key,
                action_type=AuditActionType.PROCUREMENT_ACTION,
                severity=AuditSeverity.WARNING,
                status=AuditStatus.FAILED,
                actor_type=AuditActorType.USER,
                actor_user=self.officer,
                actor_label="Priya Buyer, Procurement Officer, Corona Secondary School",
                entity_type="PurchaseOrder",
                entity_id=f"PO-{index:05d}",
                entity_label=f"Purchase order {index:05d} for the science block refurbishment",
                summary=(
                    f"Purchase order {index:05d} approval failed because the approver "
                    "was no longer assigned to the requesting branch."
                ),
            )
            for index in range(count)
        ]
        AuditEvent.objects.bulk_create(events)
        return events


class EventExplorerTenantFilterEndpointTests(AuditExportFileFixture, TestCase):
    """The filter on the wire: same gate, same readers, one new parameter.

    ``officer`` is a Codex audit officer; ``outsider`` holds the very same
    ``platform.audit.view`` key inside Bright Star, and ``stranger`` holds
    nothing. Nobody's reach may change.
    """

    def setUp(self):
        self.build()
        self.make_events(2)
        AuditEvent.objects.filter(entity_type="PurchaseOrder").update(
            tenant=self.other_tenant,
        )
        self.client = TenantAPIClient(self.officer)

    @staticmethod
    def _rows(response):
        payload = response.data["data"]
        return payload["results"] if isinstance(payload, dict) else payload

    def test_the_filter_narrows_the_listing_to_one_tenant(self):
        response = self.client.get(
            "/v1/audit/events/", {"tenant_slug": self.other_tenant.slug},
        )

        self.assertEqual(response.status_code, 200, response.data)
        rows = self._rows(response)
        self.assertTrue(rows)
        self.assertEqual({row["tenant"] for row in rows}, {self.other_tenant.slug})

    def test_the_null_sentinel_finds_events_that_belong_to_no_tenant(self):
        response = self.client.get("/v1/audit/events/", {"tenant_slug": NO_TENANT})

        self.assertEqual(response.status_code, 200, response.data)
        rows = self._rows(response)
        self.assertTrue(rows)
        self.assertEqual({row["tenant"] for row in rows}, {None})

    def test_an_unknown_tenant_is_a_400_and_not_an_empty_page(self):
        response = self.client.get("/v1/audit/events/", {"tenant_slug": "no-such-school"})

        self.assertEqual(response.status_code, 400)

    def test_the_permission_gate_is_unchanged(self):
        # Adding a filter must not open the surface to anyone new.
        denied = TenantAPIClient(self.stranger).get(
            "/v1/audit/events/", {"tenant_slug": self.other_tenant.slug},
        )
        self.assertEqual(denied.status_code, 403)

        allowed = self.client.get("/v1/audit/events/")
        self.assertEqual(allowed.status_code, 200)

    def test_the_tenant_roster_is_offered_to_platform_callers_only(self):
        platform = self.client.get("/v1/audit/events/filter-options/")
        self.assertEqual(platform.status_code, 200, platform.data)
        values = {row["value"] for row in platform.data["data"]["tenants"]}
        self.assertIn(self.other_tenant.slug, values)
        self.assertIn(NO_TENANT, values)

        # A school audit officer holding the same key is not handed Codex's
        # customer list just for opening the filter drawer.
        school_side = TenantAPIClient(self.outsider).get(
            "/v1/audit/events/filter-options/",
        )
        self.assertEqual(school_side.status_code, 200, school_side.data)
        self.assertEqual(school_side.data["data"]["tenants"], [])


class AuditExportStorageTests(AuditExportFileFixture, TestCase):
    """The export writes a file to storage and records its name, not its body."""

    def setUp(self):
        self.build()
        self.client = TenantAPIClient(self.officer)

    def _create_export(self, client=None, payload=None):
        return (client or self.client).post(
            "/v1/audit/exports/",
            payload if payload is not None else {"filter_payload": self.FILTER},
            format="json",
        )

    def test_export_of_many_events_stores_a_name_and_not_the_csv_body(self):
        self.make_events(60)

        response = self._create_export()

        self.assertEqual(response.status_code, 201, response.data)
        job = AuditExportJob.objects.get(id=response.data["data"]["id"])
        self.assertEqual(job.status, ExportJobStatus.COMPLETED)
        self.assertEqual(job.row_count, 60)
        # The old defect: the whole CSV went into a 500-character column.
        self.assertLess(len(job.file_path), 500)
        self.assertNotIn("event_id,event_at", job.file_path)
        self.assertTrue(job.file_path.startswith("audit-exports/"))
        self.assertTrue(default_storage.exists(job.file_path))
        # The body really is in storage, and it is the whole file.
        with default_storage.open(job.file_path, "rb") as handle:
            body = handle.read().decode("utf-8")
        self.assertGreater(len(body), 500)
        self.assertEqual(len(body.strip().splitlines()), 61)

    def test_detail_payload_offers_a_download_url_and_never_the_storage_key(self):
        self.make_events(3)

        response = self._create_export()
        data = response.data["data"]

        job = AuditExportJob.objects.get(id=data["id"])
        self.assertNotIn("file_path", data)
        self.assertEqual(data["download_url"], f"/v1/audit/exports/{job.id}/download/")

        detail = self.client.get(f"/v1/audit/exports/{job.id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn("file_path", detail.data["data"])

        # The history list is where the download button lives, so it carries the
        # link too - and equally never the storage key.
        listing = self.client.get("/v1/audit/exports/")
        self.assertEqual(listing.status_code, 200)
        payload = listing.data["data"]
        rows = payload["results"] if isinstance(payload, dict) else payload
        row = next(item for item in rows if item["id"] == data["id"])
        self.assertNotIn("file_path", row)
        self.assertEqual(row["download_url"], data["download_url"])

    def test_stored_export_round_trips_through_the_download_route(self):
        made = self.make_events(60)

        created = self._create_export()
        job_id = created.data["data"]["id"]

        response = self.client.get(f"/v1/audit/exports/{job_id}/download/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("attachment", response["Content-Disposition"])
        body = b"".join(response.streaming_content).decode("utf-8")
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual(rows[0], EXPORT_CSV_HEADER)
        self.assertEqual(len(rows) - 1, 60)
        # Every exported event is in the file, and nothing else is.
        self.assertEqual(
            {row[0] for row in rows[1:]},
            {str(event.id) for event in self.exported_events()},
        )
        exported = {row[0]: row for row in rows[1:]}
        sample = AuditEvent.objects.get(id=made[0].id)
        self.assertEqual(exported[str(sample.id)][12], sample.summary)
        self.assertEqual(exported[str(sample.id)][7], self.officer.email)

    def test_zero_event_export_completes_with_a_header_only_file(self):
        # No events at all: an empty answer is a real answer, not a failure.
        response = self._create_export()

        self.assertEqual(response.status_code, 201, response.data)
        job = AuditExportJob.objects.get(id=response.data["data"]["id"])
        self.assertEqual(job.status, ExportJobStatus.COMPLETED)
        self.assertEqual(job.row_count, 0)
        self.assertTrue(default_storage.exists(job.file_path))

        download = self.client.get(f"/v1/audit/exports/{job.id}/download/")
        self.assertEqual(download.status_code, 200)
        body = b"".join(download.streaming_content).decode("utf-8")
        self.assertEqual(list(csv.reader(io.StringIO(body))), [EXPORT_CSV_HEADER])

    def test_download_of_an_unfinished_job_is_refused_rather_than_empty(self):
        job = AuditExportJob.objects.create(
            requested_by=self.officer,
            export_format=ExportFormat.CSV,
            status=ExportJobStatus.RUNNING,
        )
        response = self.client.get(f"/v1/audit/exports/{job.id}/download/")
        self.assertEqual(response.status_code, 409)

    def test_download_of_an_expired_job_is_gone(self):
        self.make_events(2)
        created = self._create_export()
        job = AuditExportJob.objects.get(id=created.data["data"]["id"])
        job.expires_at = timezone.now() - timedelta(seconds=1)
        job.save(update_fields=["expires_at"])

        response = self.client.get(f"/v1/audit/exports/{job.id}/download/")
        self.assertEqual(response.status_code, 410)

        detail = self.client.get(f"/v1/audit/exports/{job.id}/")
        self.assertIsNone(detail.data["data"]["download_url"])


class AuditExportAuthorisationTests(AuditExportFileFixture, TestCase):
    """Who may take the trail out of the building."""

    def setUp(self):
        self.build()
        self.make_events(5)
        self.officer_client = TenantAPIClient(self.officer)
        created = self.officer_client.post(
            "/v1/audit/exports/", {"filter_payload": self.FILTER}, format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.job = AuditExportJob.objects.get(id=created.data["data"]["id"])

    def test_caller_without_the_export_permission_is_refused(self):
        client = TenantAPIClient(self.stranger)

        self.assertEqual(
            client.post(
                "/v1/audit/exports/", {"filter_payload": self.FILTER}, format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            client.get(f"/v1/audit/exports/{self.job.id}/download/").status_code, 403,
        )

    def test_caller_cannot_retrieve_another_tenants_export(self):
        # The outsider holds platform.audit.export in their own tenant, so the
        # only thing standing between them and this file is the job's requester.
        client = TenantAPIClient(self.outsider)

        response = client.get(f"/v1/audit/exports/{self.job.id}/download/")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("event_id", str(response.data))

    def test_outsider_can_still_download_their_own_export(self):
        client = TenantAPIClient(self.outsider)
        created = client.post(
            "/v1/audit/exports/", {"filter_payload": self.FILTER}, format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        job_id = created.data["data"]["id"]

        response = client.get(f"/v1/audit/exports/{job_id}/download/")
        self.assertEqual(response.status_code, 200)


class AuditExportFailureStateTests(AuditExportFileFixture, TestCase):
    """A job that cannot finish must say so, not sit at RUNNING for ever."""

    def setUp(self):
        self.build()
        self.client = TenantAPIClient(self.officer)

    @override_settings(MEDIA_DB_MAX_BYTES=2000)
    def test_export_past_the_size_ceiling_fails_at_the_final_check(self):
        # 60 wide rows is well past 2 KB but short of the mid-write checkpoint,
        # so this exercises the guard that runs after the last row.
        self.make_events(60)

        response = self.client.post(
            "/v1/audit/exports/", {"filter_payload": self.FILTER}, format="json",
        )

        self.assertEqual(response.status_code, 413)
        job = AuditExportJob.objects.get(id=response.data["error"]["job_id"])
        self.assertEqual(job.status, ExportJobStatus.FAILED)
        self.assertIn("2,000 bytes", job.failure_reason)
        self.assertEqual(job.file_path, "")
        self.assertIsNotNone(job.completed_at)
        self.assertFalse(StoredFile.objects.filter(name__startswith="audit-exports/").exists())

    @override_settings(MEDIA_DB_MAX_BYTES=2000)
    def test_export_past_the_size_ceiling_is_abandoned_mid_write(self):
        # 600 rows crosses the 500-row checkpoint, so generation stops there
        # rather than building the whole file and failing at save.
        self.make_events(600)

        response = self.client.post(
            "/v1/audit/exports/", {"filter_payload": self.FILTER}, format="json",
        )

        self.assertEqual(response.status_code, 413)
        job = AuditExportJob.objects.get(id=response.data["error"]["job_id"])
        self.assertEqual(job.status, ExportJobStatus.FAILED)
        self.assertFalse(StoredFile.objects.filter(name__startswith="audit-exports/").exists())

        # Generation really did stop at the checkpoint rather than writing all 600.
        with self.assertRaises(ExportTooLarge) as caught:
            write_audit_export_file(job, self.exported_events(), set())
        self.assertEqual(caught.exception.rows_written, 500)

    def test_an_unexpected_error_leaves_the_job_failed_not_running(self):
        self.make_events(3)

        with mock.patch(
            "vs_audit.views.write_audit_export_file",
            side_effect=OSError("storage went away"),
        ):
            response = self.client.post(
                "/v1/audit/exports/", {"filter_payload": self.FILTER}, format="json",
            )

        self.assertEqual(response.status_code, 500)
        job = AuditExportJob.objects.get(id=response.data["error"]["job_id"])
        self.assertEqual(job.status, ExportJobStatus.FAILED)
        self.assertTrue(job.failure_reason)
        self.assertIsNotNone(job.completed_at)


# -----------------------------------------------------------------------------
# Who may read an audit row: the tenant boundary on every audit surface
# -----------------------------------------------------------------------------

class AuditTenantIsolationFixture:
    """Two schools with audit officers of their own, and a Codex reviewer.

    ``bright_officer`` and ``green_officer`` hold exactly the same two keys -
    ``platform.audit.view`` and ``platform.audit.export`` - inside different
    tenants, which is the arrangement ``PermissionScope.TENANT`` exists to
    allow and which ``seed_platform_permissions`` names in so many words.
    ``reviewer`` holds them on the codex PLATFORM tenant and must keep reading
    across every school.

    The events are written in all three shapes the table actually holds:

    * ``*_current`` - ``tenant`` populated, the shape every row has had since
      d1ceccb;
    * ``*_legacy`` - ``tenant`` NULL with the owner's pk in
      ``metadata['tenant_id']``, the shape d1ceccb deliberately did not
      backfill;
    * ``unattributed`` - NULL with no recorded id at all, older than 661a73a
      and therefore readable by nobody but platform staff.
    """

    def build(self):
        call_command("seed_actions", verbosity=0)
        call_command("seed_platform_permissions", verbosity=0)
        # Events below are written outside any request, so the ambient tenant
        # must not survive from an earlier test and stamp itself on them.
        clear_request_context()

        self.platform = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        self.bright_star = Tenant.objects.create(
            name="Bright Star School", slug="bright-star",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        self.greenfield = Tenant.objects.create(
            name="Greenfield School", slug="greenfield",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )

        self.reviewer = self._officer("cx.reviewer@example.test", self.platform)
        self.bright_officer = self._officer("bright.officer@example.test", self.bright_star)
        self.green_officer = self._officer("green.officer@example.test", self.greenfield)

        self.bright_current = self._event("PO-BRIGHT-1", tenant=self.bright_star)
        self.bright_legacy = self._event("PO-BRIGHT-2", owner=self.bright_star)
        self.green_current = self._event(
            "PO-GREEN-1", tenant=self.greenfield, severity=AuditSeverity.CRITICAL,
        )
        self.green_legacy = self._event(
            "PO-GREEN-2", owner=self.greenfield, severity=AuditSeverity.CRITICAL,
        )
        self.unattributed = self._event("PO-ANON")

    OWN_ROWS = {"PO-BRIGHT-1", "PO-BRIGHT-2"}
    OTHER_ROWS = {"PO-GREEN-1", "PO-GREEN-2"}
    EVERY_ROW = OWN_ROWS | OTHER_ROWS | {"PO-ANON"}
    FILTER = {"entity_type": "PurchaseOrder"}

    def _officer(self, email, tenant):
        from vs_rbac.models import (
            Permission, TenantRolePermission, TenantRoleTemplate, TenantUserRoleAssignment,
        )

        user = User.objects.create_user(
            email=email, password="Str0ng!pass123", status="ACTIVE",
            first_name=email.split(".")[0].title(), last_name="Officer",
            tenant=tenant,
        )
        template, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=tenant, key="audit_officer",
            defaults={"name": "Audit officer", "status": "ACTIVE"},
        )
        keys = ["platform.audit.view", "platform.audit.export"]
        for permission in Permission.objects.filter(key__in=keys):
            TenantRolePermission.objects.get_or_create(
                role=template, permission=permission, defaults={"granted": True},
            )
        TenantUserRoleAssignment.objects.create(
            tenant=tenant, user=user, role=template, assignment_status="ACTIVE",
        )
        return user

    def _event(self, entity_id, *, tenant=None, owner=None, severity=AuditSeverity.WARNING):
        """Write one procurement event, through the real emitter so a trail exists."""
        event = emit_audit_event(
            module_key=AuditModuleKey.PROCUREMENT,
            action_type=AuditActionType.PROCUREMENT_ACTION,
            severity=severity,
            status=AuditStatus.FAILED,
            tenant=tenant,
            entity_type="PurchaseOrder",
            entity_id=entity_id,
            entity_label=f"Purchase order {entity_id} for the science block",
            summary=f"Approval failed on purchase order {entity_id}",
            metadata={"tenant_id": str(owner.pk)} if owner is not None else {},
        )
        self.assertIsNotNone(event, f"{entity_id} was swallowed by emit_audit_event")
        return event

    @staticmethod
    def rows(response):
        payload = response.data["data"]
        if isinstance(payload, dict):
            return payload.get("results", [])
        return payload

    def entity_ids(self, response):
        return {row["entity_id"] for row in self.rows(response)}


class AuditEventTenantIsolationTests(AuditTenantIsolationFixture, TestCase):
    """Bright Star's audit officer may read Bright Star's trail and no other."""

    def setUp(self):
        self.build()
        self.bright = TenantAPIClient(self.bright_officer)
        self.green = TenantAPIClient(self.green_officer)
        self.cx = TenantAPIClient(self.reviewer)

    def test_a_school_officer_reads_only_their_own_tenants_events(self):
        response = self.bright.get("/v1/audit/events/", dict(self.FILTER))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.entity_ids(response), self.OWN_ROWS)
        # Not merely absent from the id set: nothing of Greenfield's rides
        # along in a summary, a label or an actor name either.
        self.assertNotIn("PO-GREEN", str(response.data))

    def test_a_school_officer_still_sees_their_pre_backfill_history(self):
        # The row d1ceccb left at tenant=NULL is recovered through the pk that
        # was recorded in metadata at the time - not inferred, and not the
        # whole null set.
        self.assertIsNone(self.bright_legacy.tenant_id)

        response = self.bright.get("/v1/audit/events/", dict(self.FILTER))

        self.assertIn("PO-BRIGHT-2", self.entity_ids(response))

    def test_a_null_row_belonging_to_nobody_stays_platform_only(self):
        # Older than 661a73a: no column, no recorded id, so no school may claim
        # it. Being invisible to a school is the safe direction to be wrong in.
        bright = self.bright.get("/v1/audit/events/", dict(self.FILTER))
        green = self.green.get("/v1/audit/events/", dict(self.FILTER))

        self.assertNotIn("PO-ANON", self.entity_ids(bright))
        self.assertNotIn("PO-ANON", self.entity_ids(green))

    def test_each_school_sees_its_own_trail_and_only_its_own(self):
        response = self.green.get("/v1/audit/events/", dict(self.FILTER))

        self.assertEqual(self.entity_ids(response), self.OTHER_ROWS)

    def test_a_platform_reviewer_still_reads_across_tenants(self):
        response = self.cx.get("/v1/audit/events/", dict(self.FILTER))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.entity_ids(response), self.EVERY_ROW)

    def test_the_tenant_slug_filter_still_narrows_inside_the_boundary(self):
        response = self.bright.get(
            "/v1/audit/events/", {**self.FILTER, "tenant_slug": self.bright_star.slug},
        )

        self.assertEqual(response.status_code, 200, response.data)
        # Narrowed to the column, so the legacy null row drops out - which is
        # what asking for "rows stamped bright-star" means.
        self.assertEqual(self.entity_ids(response), {"PO-BRIGHT-1"})

    def test_the_tenant_slug_filter_cannot_widen_the_boundary(self):
        response = self.bright.get(
            "/v1/audit/events/", {**self.FILTER, "tenant_slug": self.greenfield.slug},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.entity_ids(response), set())

    def test_the_null_sentinel_cannot_reach_another_schools_null_rows(self):
        response = self.bright.get(
            "/v1/audit/events/", {**self.FILTER, "tenant_slug": NO_TENANT},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.entity_ids(response), {"PO-BRIGHT-2"})

    def test_the_permission_gate_is_unchanged_for_the_holder(self):
        # Scoping must narrow what a holder reads, never lock them out of the
        # surface: both officers still get a 200 and rows of their own.
        for client in (self.bright, self.green, self.cx):
            response = client.get("/v1/audit/events/", dict(self.FILTER))
            self.assertEqual(response.status_code, 200, response.data)
            self.assertTrue(self.rows(response))


class AuditEventDetailTenantIsolationTests(AuditTenantIsolationFixture, TestCase):
    """Knowing an event's id is not authority to read it."""

    def setUp(self):
        self.build()
        self.bright = TenantAPIClient(self.bright_officer)

    def test_another_tenants_event_is_a_404_even_with_its_id(self):
        response = self.bright.get(f"/v1/audit/events/{self.green_current.id}/")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("PO-GREEN", str(response.data))

    def test_another_tenants_legacy_event_is_a_404_too(self):
        response = self.bright.get(f"/v1/audit/events/{self.green_legacy.id}/")

        self.assertEqual(response.status_code, 404)

    def test_the_callers_own_events_still_open_in_both_shapes(self):
        for event in (self.bright_current, self.bright_legacy):
            response = self.bright.get(f"/v1/audit/events/{event.id}/")
            self.assertEqual(response.status_code, 200, response.data)

    def test_a_platform_reviewer_still_opens_any_event(self):
        response = TenantAPIClient(self.reviewer).get(
            f"/v1/audit/events/{self.green_current.id}/",
        )

        self.assertEqual(response.status_code, 200, response.data)


class EntityAuditTrailTenantIsolationTests(AuditTenantIsolationFixture, TestCase):
    """The entity catalogue is the enumerable route, so it is bounded too."""

    def setUp(self):
        self.build()
        self.bright = TenantAPIClient(self.bright_officer)

    def test_the_catalogue_lists_only_entities_the_caller_can_read(self):
        response = self.bright.get("/v1/audit/entity-trails/", dict(self.FILTER))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.entity_ids(response), self.OWN_ROWS)
        self.assertNotIn("PO-GREEN", str(response.data))

    def test_a_platform_reviewer_still_sees_the_whole_catalogue(self):
        response = TenantAPIClient(self.reviewer).get(
            "/v1/audit/entity-trails/", dict(self.FILTER),
        )

        self.assertEqual(self.entity_ids(response), self.EVERY_ROW)

    def test_another_tenants_trail_is_a_404_not_an_empty_trail(self):
        response = self.bright.get("/v1/audit/entity-trails/PurchaseOrder/PO-GREEN-1/")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("science block", str(response.data))

    def test_the_callers_own_trail_still_opens_in_both_shapes(self):
        for entity_id in sorted(self.OWN_ROWS):
            response = self.bright.get(
                f"/v1/audit/entity-trails/PurchaseOrder/{entity_id}/",
            )
            self.assertEqual(response.status_code, 200, response.data)
            events = response.data["data"]["events"]
            self.assertEqual({row["entity_id"] for row in events}, {entity_id})


class AuditDashboardTenantIsolationTests(AuditTenantIsolationFixture, TestCase):
    """A count is a disclosure: the dashboard answers to the same boundary."""

    def setUp(self):
        self.build()

    def _kpis(self, user):
        response = TenantAPIClient(user).get("/v1/audit/dashboard-summary/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_the_dashboard_counts_only_the_callers_own_events(self):
        # Both of Greenfield's rows are CRITICAL and nobody else's are.
        self.assertEqual(self._kpis(self.bright_officer)["kpis"]["critical_24h"], 0)
        self.assertEqual(self._kpis(self.green_officer)["kpis"]["critical_24h"], 2)
        self.assertEqual(self._kpis(self.reviewer)["kpis"]["critical_24h"], 2)

    def test_the_critical_heatmap_does_not_leak_another_schools_incidents(self):
        def total(payload):
            return sum(sum(row) for row in payload["critical_heatmap"])

        self.assertEqual(total(self._kpis(self.bright_officer)), 0)
        self.assertEqual(total(self._kpis(self.green_officer)), 2)

    def test_the_severity_series_does_not_leak_another_schools_volume(self):
        def criticals(payload):
            return sum(day["CRITICAL"] for day in payload["severity_series"])

        self.assertEqual(criticals(self._kpis(self.bright_officer)), 0)
        self.assertEqual(criticals(self._kpis(self.green_officer)), 2)


class AuditExportTenantIsolationTests(AuditTenantIsolationFixture, TestCase):
    """The copy that leaves the building carries no more than the screen showed."""

    def setUp(self):
        self.build()
        self.bright = TenantAPIClient(self.bright_officer)

    def _export(self, client):
        response = client.post(
            "/v1/audit/exports/", {"filter_payload": dict(self.FILTER)}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return AuditExportJob.objects.get(id=response.data["data"]["id"])

    def test_an_export_carries_only_the_callers_own_rows(self):
        job = self._export(self.bright)

        self.assertEqual(job.row_count, 2)
        with default_storage.open(job.file_path, "rb") as handle:
            body = handle.read().decode("utf-8")
        self.assertIn("PO-BRIGHT-1", body)
        self.assertIn("PO-BRIGHT-2", body)
        self.assertNotIn("PO-GREEN", body)
        self.assertNotIn("PO-ANON", body)

    def test_a_platform_export_still_covers_every_tenant(self):
        job = self._export(TenantAPIClient(self.reviewer))

        self.assertEqual(job.row_count, len(self.EVERY_ROW))

    def test_export_history_shows_only_the_callers_own_tenant(self):
        cx_job = self._export(TenantAPIClient(self.reviewer))
        own_job = self._export(self.bright)

        response = self.bright.get("/v1/audit/exports/")

        self.assertEqual(response.status_code, 200, response.data)
        ids = {row["id"] for row in self.rows(response)}
        self.assertIn(str(own_job.id), ids)
        self.assertNotIn(str(cx_job.id), ids)

    def test_another_tenants_export_job_is_not_readable_by_id(self):
        cx_job = self._export(TenantAPIClient(self.reviewer))

        response = self.bright.get(f"/v1/audit/exports/{cx_job.id}/")

        self.assertEqual(response.status_code, 404)
