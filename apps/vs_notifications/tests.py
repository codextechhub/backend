# =============================================================================
# vs_notifications / tests.py
#
# Suite for the recipient-centric notification overhaul.
#
# Security first (403 without RBAC, cross-tenant isolation, feed 404, history
# scoping), then the domain logic (resolve_channels layering, dispatch with no
# school, html multipart, pre-flight FAILED signal, delivery task signals), the
# settings API (effective matrix shape + source, upsert, IN_APP + transactional
# rejections), and the empty-list response shape.
#
# Runs on SQLite (apps.settings.test) and Postgres (apps.settings.local) — the
# conditional UniqueConstraints exercise on both.
# =============================================================================

from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings

from vs_schools.models import School

from .constants import ChannelChoices, NotificationErrorCode, NotificationPermission, NotificationStatus
from .models import (
    Notification,
    NotificationEventType,
    NotificationSetting,
)
from .services.dispatch import NotificationService, UnregisteredRecipient
from .services.settings import resolve_channels, resolve_channels_bulk
from .services.seed import seed_event_types, seed_notification_templates, seed_platform_settings
from . import signals

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _grant_school_permission(user, school, permission_key):
    """Build the full RBAC chain so *user* holds *permission_key* in *school*."""
    from vs_rbac.models import (
        Permission,
        PermissionAction,
        PermissionModule,
        PermissionResource,
        TenantRolePermission,
        TenantRoleTemplate,
        TenantUserRoleAssignment,
    )

    module_key, resource_name, action_key = permission_key.split(".")
    module, _ = PermissionModule.objects.get_or_create(name=module_key)
    resource, _ = PermissionResource.objects.get_or_create(module=module, name=resource_name)
    action, _ = PermissionAction.objects.get_or_create(name=action_key)
    perm, _ = Permission.objects.get_or_create(module=module, resource=resource, action=action)

    role = TenantRoleTemplate.objects.create(
        tenant=school.tenant, key=f"role-{permission_key.replace('.', '-')}",
        name=f"Role {permission_key}",
    )
    TenantRolePermission.objects.create(role=role, permission=perm, granted=True)
    TenantUserRoleAssignment.objects.create(
        tenant=school.tenant, user=user, role=role, assignment_status="ACTIVE",
    )


class _NotifFixture(TestCase):
    """Seeds event types/templates/platform settings and builds users + schools."""

    def setUp(self):
        seed_event_types()
        seed_notification_templates()
        seed_platform_settings()

        self.school_a = School.objects.create(
            name="Alpha", slug="alpha-nt", code="ALPNT", status="ACTIVE",
        )
        self.school_b = School.objects.create(
            name="Beta", slug="beta-nt", code="BETNT", status="ACTIVE",
        )

        # School-scoped admin in school A, granted the settings permission.
        self.admin_a = User.objects.create_user(
            email="admin-a@test.com", password="x", user_type="SCHOOL_ADMIN",
            status="ACTIVE", first_name="Ada", last_name="Admin", tenant=self.school_a.tenant,
        )
        _grant_school_permission(
            self.admin_a, self.school_a, NotificationPermission.ENFORCE_PERMISSIONS,
        )

        # A plain school user with no RBAC grants (for 403 tests).
        self.plain_a = User.objects.create_user(
            email="plain-a@test.com", password="x", user_type="SCHOOL_ADMIN",
            status="ACTIVE", first_name="Peter", last_name="Plain", tenant=self.school_a.tenant,
        )

        # A CX super admin (bypasses RBAC; no school → platform scope).
        self.cx = User.objects.create_user(
            email="cx@test.com", password="x", user_type="CX_STAFF",
            status="ACTIVE", first_name="Cee", last_name="Ex",
        )
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=self.cx.tenant, key="xvs_super_admin",
            defaults={"name": "XVS Super Admin", "status": "ACTIVE",
                      "is_system_role": True, "is_locked": True},
        )
        TenantUserRoleAssignment.objects.create(
            tenant=self.cx.tenant, user=self.cx, role=role, assignment_status="ACTIVE",
        )

    def _client(self, user):
        """
        Authenticate through the real tenant auth layer. TenantAPIClient mints a
        CodeXRefreshToken (carrying the tenant assertion) and auto-appends
        ?tenant=<the user's home tenant slug> to every request, so
        TenantJWTAuthentication establishes request.tenant exactly as production
        traffic does. Requests that must assert a DIFFERENT tenant (cross-tenant
        404 tests) build the URL with an explicit ?tenant=<other slug> — the
        client only appends when the path has no tenant param.
        """
        from core.test_utils import TenantAPIClient
        return TenantAPIClient(user)

    def _event(self, key):
        return NotificationEventType.objects.get(key=key)


# ---------------------------------------------------------------------------
# resolve_channels — layering
# ---------------------------------------------------------------------------

class ResolveChannelsTests(_NotifFixture):

    def test_default_when_no_rows(self):
        et = self._event("ticket.created")
        NotificationSetting.all_objects.filter(event_type=et).delete()
        resolved = resolve_channels(et, tenant=self.school_a.tenant)
        self.assertEqual(
            resolved,
            {ChannelChoices.IN_APP: et.default_enabled, ChannelChoices.EMAIL: et.default_enabled},
        )

    def test_platform_row_wins_over_default(self):
        et = self._event("ticket.created")
        NotificationSetting.all_objects.filter(
            event_type=et, channel=ChannelChoices.EMAIL, tenant__isnull=True,
        ).update(is_enabled=False)
        resolved = resolve_channels(et, tenant=self.school_a.tenant)
        self.assertFalse(resolved[ChannelChoices.EMAIL])

    def test_school_row_beats_platform(self):
        et = self._event("ticket.created")
        NotificationSetting.all_objects.filter(
            event_type=et, channel=ChannelChoices.EMAIL, tenant__isnull=True,
        ).update(is_enabled=False)
        NotificationSetting.all_objects.create(
            tenant=self.school_a.tenant, event_type=et,
            channel=ChannelChoices.EMAIL, is_enabled=True,
        )
        self.assertTrue(resolve_channels(et, tenant=self.school_a.tenant)[ChannelChoices.EMAIL])
        # School B has no override → still off (platform).
        self.assertFalse(resolve_channels(et, tenant=self.school_b.tenant)[ChannelChoices.EMAIL])

    def test_transactional_bypasses_disabled_rows(self):
        et = self._event("user.password_reset")
        NotificationSetting.all_objects.create(
            tenant=None, event_type=et, channel=ChannelChoices.EMAIL, is_enabled=False,
        )
        self.assertTrue(resolve_channels(et)[ChannelChoices.EMAIL])

    def test_is_active_kills_all(self):
        et = self._event("user.password_reset")  # transactional
        et.is_active = False
        et.save(update_fields=["is_active"])
        self.assertEqual(resolve_channels(et), {ChannelChoices.EMAIL: False})


# ---------------------------------------------------------------------------
# resolve_channels_bulk — layering across multiple event types, one query
# ---------------------------------------------------------------------------

class ResolveChannelsBulkTests(_NotifFixture):

    def test_layering_across_multiple_event_types_one_call(self):
        """school beats platform beats default — for several event types at once."""
        et_school = self._event("ticket.created")       # school override wins
        et_platform = self._event("ticket.assigned")      # platform row wins
        et_default = self._event("ticket.resolved")       # no rows → default
        et_tx = self._event("user.password_reset")        # transactional bypass

        # et_school: platform says off, school A says on → school wins.
        NotificationSetting.all_objects.filter(
            event_type=et_school, channel=ChannelChoices.EMAIL, tenant__isnull=True,
        ).update(is_enabled=False)
        NotificationSetting.all_objects.create(
            tenant=self.school_a.tenant, event_type=et_school,
            channel=ChannelChoices.EMAIL, is_enabled=True,
        )
        # et_platform: platform row says off; no school override → platform wins.
        NotificationSetting.all_objects.filter(
            event_type=et_platform, channel=ChannelChoices.EMAIL, tenant__isnull=True,
        ).update(is_enabled=False)
        # et_default: no rows at all → default_enabled fallback.
        NotificationSetting.all_objects.filter(event_type=et_default).delete()
        # et_tx: a disabled row must be ignored — transactional always fires.
        NotificationSetting.all_objects.create(
            tenant=None, event_type=et_tx, channel=ChannelChoices.EMAIL, is_enabled=False,
        )

        resolved = resolve_channels_bulk(
            [et_school, et_platform, et_default, et_tx], tenant=self.school_a.tenant,
        )

        self.assertTrue(resolved[et_school.id][ChannelChoices.EMAIL])       # school layer
        self.assertFalse(resolved[et_platform.id][ChannelChoices.EMAIL])    # platform layer
        self.assertEqual(                                                    # default layer
            resolved[et_default.id][ChannelChoices.EMAIL], et_default.default_enabled,
        )
        self.assertTrue(resolved[et_tx.id][ChannelChoices.EMAIL])           # transactional

    def test_bulk_uses_single_settings_query(self):
        event_types = list(NotificationEventType.objects.filter(is_active=True))
        with self.assertNumQueries(1):
            resolve_channels_bulk(event_types, tenant=self.school_a.tenant)

    def test_matrix_build_costs_two_queries(self):
        """1 event-type query + 1 settings query — no per-event resolve queries."""
        from .views import NotificationSettingViewSet
        view = NotificationSettingViewSet()
        with self.assertNumQueries(2):
            matrix = view._build_matrix(self.school_a.tenant)
        self.assertTrue(matrix)

    def test_single_resolve_delegates_to_bulk(self):
        et = self._event("ticket.created")
        self.assertEqual(
            resolve_channels(et, tenant=self.school_a.tenant),
            resolve_channels_bulk([et], tenant=self.school_a.tenant)[et.id],
        )


# ---------------------------------------------------------------------------
# Dispatch service
# ---------------------------------------------------------------------------

class DispatchTests(_NotifFixture):

    def _recipient(self, email="rcpt@test.com"):
        return User.objects.create_user(
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",
            first_name="Rex", last_name="Cipient",
        )

    def test_school_none_creates_records_and_enqueues_email(self):
        rcpt = self._recipient()
        # on_commit fires the enqueue; TestCase never commits, so capture it.
        with mock.patch("vs_notifications.tasks.deliver_email_notification.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                ids = NotificationService.send(
                    event_key="ticket.created",
                    context={"student_first_name": "Sam", "student_last_name": "Doe"},
                    recipients=[rcpt],
                    # no school → platform scope
                )
        self.assertEqual(len(ids), 2)  # in_app + email
        notifs = Notification.objects.filter(id__in=ids)
        # No school passed → dispatch anchors the records on the recipient's home
        # tenant, which for a CX recipient is the codex PLATFORM tenant.
        self.assertEqual(notifs.first().tenant_id, rcpt.tenant_id)
        self.assertEqual(notifs.first().tenant.kind, "PLATFORM")
        email = notifs.get(channel=ChannelChoices.EMAIL)
        self.assertEqual(email.status, NotificationStatus.PENDING)
        in_app = notifs.get(channel=ChannelChoices.IN_APP)
        self.assertEqual(in_app.status, NotificationStatus.SENT)
        delay.assert_called_once_with(str(email.id))

    def test_metadata_stored_but_never_serialized(self):
        rcpt = self._recipient()
        with mock.patch("vs_notifications.tasks.deliver_email_notification.delay"):
            ids = NotificationService.send(
                event_key="ticket.created",
                context={"student_first_name": "Sam"},
                recipients=[rcpt],
                metadata={"activation_key": "abc123"},
            )
        n = Notification.objects.filter(id__in=ids).first()
        self.assertEqual(n.metadata, {"activation_key": "abc123"})
        from .serializers import NotificationDetailSerializer, NotificationHistoryDetailSerializer
        self.assertNotIn("metadata", NotificationDetailSerializer(n).data)
        self.assertNotIn("metadata", NotificationHistoryDetailSerializer(n).data)

    def test_feed_exposes_body_and_allowlisted_action_without_metadata(self):
        from .serializers import NotificationListSerializer

        rcpt = self._recipient()
        notification = Notification.objects.create(
            recipient=rcpt,
            tenant=rcpt.tenant,
            event_type=self._event("ticket.commented"),
            channel=ChannelChoices.IN_APP,
            subject="",
            body="Ada commented on ticket TCK-0001.",
            status=NotificationStatus.SENT,
            metadata={"ticket_id": 42, "secret": "never-expose"},
        )

        data = NotificationListSerializer(notification).data
        self.assertEqual(data["subject"], notification.event_type.label)
        self.assertEqual(data["body"], "Ada commented on ticket TCK-0001.")
        self.assertEqual(data["action_url"], "/support/tickets/42")
        self.assertNotIn("metadata", data)

    def test_html_body_rendered_and_stored(self):
        with mock.patch("vs_notifications.tasks.deliver_email_notification.delay"):
            ids = NotificationService.send(
                event_key="user.invited",
                context={
                    "user_first_name": "Jane", "user_full_name": "Jane Doe",
                    "school_name": "Alpha", "invitation_url": "https://x/y", "expiry_days": 7,
                },
                recipients=[],
                unregistered_recipients=[UnregisteredRecipient(email="new@test.com", name="Jane")],
            )
        n = Notification.objects.get(id=ids[0])
        self.assertEqual(n.channel, ChannelChoices.EMAIL)
        self.assertIn("Jane", n.html_body)
        self.assertIn("<html", n.html_body.lower())

    def test_preflight_failed_fires_notification_failed(self):
        received = []
        signals.notification_failed.connect(
            lambda sender, notification, **kw: received.append(notification),
            weak=False, dispatch_uid="test-preflight",
        )
        self.addCleanup(
            signals.notification_failed.disconnect, dispatch_uid="test-preflight",
        )
        # The pre-flight FAILED signal fires from on_commit — capture it.
        with self.captureOnCommitCallbacks(execute=True):
            ids = NotificationService.send(
                event_key="user.invited",
                context={"user_first_name": "Jane", "school_name": "Alpha",
                         "invitation_url": "u", "expiry_days": 7, "user_full_name": "Jane Doe"},
                recipients=[],
                unregistered_recipients=[UnregisteredRecipient(email="", name="Jane")],
            )
        n = Notification.objects.get(id=ids[0])
        self.assertEqual(n.status, NotificationStatus.FAILED)
        self.assertEqual(n.failure_reason, "NO_EMAIL_ADDRESS")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].id, n.id)

    def test_all_channels_disabled_returns_empty(self):
        et = self._event("ticket.created")
        NotificationSetting.all_objects.filter(event_type=et).update(is_enabled=False)
        rcpt = self._recipient()
        ids = NotificationService.send(
            event_key="ticket.created",
            context={"student_first_name": "Sam"},
            recipients=[rcpt],
            tenant=self.school_a.tenant,
        )
        self.assertEqual(ids, [])


# ---------------------------------------------------------------------------
# Delivery task + signals
# ---------------------------------------------------------------------------

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   DEFAULT_FROM_EMAIL="CodeX System <system@codexng.com>", EMAIL_CC=[])
class DeliveryTaskTests(_NotifFixture):

    def _pending_email(self, html=""):
        et = self._event("ticket.created")
        # tenant is required (non-null); its value is irrelevant to delivery — the
        # task keys off recipient/unregistered_email, not the anchor tenant.
        return Notification.objects.create(
            tenant=self.school_a.tenant, recipient=None,
            unregistered_email="dest@test.com",
            event_type=et, channel=ChannelChoices.EMAIL, subject="Hi",
            body="plain body", html_body=html, status=NotificationStatus.PENDING,
        )

    def test_deliver_marks_sent_and_fires_signal(self):
        from .tasks import deliver_email_notification
        notif = self._pending_email()
        received = []
        signals.notification_sent.connect(
            lambda sender, notification, **kw: received.append(notification),
            weak=False, dispatch_uid="test-sent",
        )
        self.addCleanup(signals.notification_sent.disconnect, dispatch_uid="test-sent")

        deliver_email_notification(str(notif.id))
        notif.refresh_from_db()
        self.assertEqual(notif.status, NotificationStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(len(received), 1)

    def test_deliver_multipart_when_html_present(self):
        from .tasks import deliver_email_notification
        notif = self._pending_email(html="<p>rich</p>")
        deliver_email_notification(str(notif.id))
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertTrue(
            any(ct == "text/html" for _, ct in getattr(msg, "alternatives", [])),
            "expected an HTML alternative to be attached",
        )

    def test_deliver_no_html_is_plain(self):
        from .tasks import deliver_email_notification
        notif = self._pending_email(html="")
        deliver_email_notification(str(notif.id))
        msg = mail.outbox[0]
        self.assertEqual(getattr(msg, "alternatives", []), [])

    def test_from_name_metadata_sets_from_header(self):
        from .tasks import deliver_email_notification
        et = self._event("ticket.created")
        notif = Notification.objects.create(
            tenant=self.school_a.tenant, recipient=None,
            unregistered_email="dest@test.com",
            event_type=et, channel=ChannelChoices.EMAIL, subject="Hi",
            body="plain", status=NotificationStatus.PENDING,
            metadata={"from_name": "Ada Admin"},
        )
        deliver_email_notification(str(notif.id))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Ada Admin", mail.outbox[0].from_email)
        self.assertIn("system@codexng.com", mail.outbox[0].from_email)

    def test_eager_mode_first_failure_is_final_no_retry(self):
        from unittest import mock

        from .tasks import deliver_email_notification
        notif = self._pending_email()
        received = []
        signals.notification_failed.connect(
            lambda sender, notification, **kw: received.append(notification),
            weak=False, dispatch_uid="test-eager-fail",
        )
        self.addCleanup(
            signals.notification_failed.disconnect, dispatch_uid="test-eager-fail",
        )

        def _boom(*a, **k):
            raise RuntimeError("smtp down")

        # request.is_eager is True when the task runs synchronously (.apply()),
        # so the guard must mark FAILED on the first failure without retrying.
        with mock.patch("vs_notifications.tasks.send_email", side_effect=_boom):
            deliver_email_notification.apply(args=[str(notif.id)]).get()

        notif.refresh_from_db()
        self.assertEqual(notif.status, NotificationStatus.FAILED)
        self.assertEqual(notif.retry_count, 1, "must not retry in eager mode")
        self.assertEqual(len(received), 1)


# ---------------------------------------------------------------------------
# Feed retrieve — cross-user isolation
# ---------------------------------------------------------------------------

class FeedRetrieveTests(_NotifFixture):

    def test_retrieve_other_users_notification_is_404(self):
        et = self._event("ticket.created")
        mine = Notification.objects.create(
            tenant=self.school_a.tenant, recipient=self.admin_a, event_type=et,
            channel=ChannelChoices.IN_APP, body="x", status=NotificationStatus.SENT,
        )
        theirs = Notification.objects.create(
            tenant=self.school_a.tenant, recipient=self.plain_a, event_type=et,
            channel=ChannelChoices.IN_APP, body="y", status=NotificationStatus.SENT,
        )
        client = self._client(self.admin_a)
        self.assertEqual(client.get(f"/v1/notify/{mine.id}/").status_code, 200)
        self.assertEqual(client.get(f"/v1/notify/{theirs.id}/").status_code, 404)

    def test_acknowledge_route_marks_only_matching_ticket_for_caller(self):
        et = self._event("ticket.created")
        mine = Notification.objects.create(
            tenant=self.school_a.tenant, recipient=self.admin_a, event_type=et,
            channel=ChannelChoices.IN_APP, body="x", status=NotificationStatus.SENT,
            metadata={"ticket_id": 42},
        )
        other_ticket = Notification.objects.create(
            tenant=self.school_a.tenant, recipient=self.admin_a, event_type=et,
            channel=ChannelChoices.IN_APP, body="y", status=NotificationStatus.SENT,
            metadata={"ticket_id": 43},
        )
        other_user = Notification.objects.create(
            tenant=self.school_a.tenant, recipient=self.plain_a, event_type=et,
            channel=ChannelChoices.IN_APP, body="z", status=NotificationStatus.SENT,
            metadata={"ticket_id": 42},
        )

        response = self._client(self.admin_a).post(
            "/v1/notify/acknowledge-route/", {"path": "/support/tickets/42"}, format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["updated_count"], 1)
        mine.refresh_from_db()
        other_ticket.refresh_from_db()
        other_user.refresh_from_db()
        self.assertTrue(mine.is_read)
        self.assertIsNotNone(mine.read_at)
        self.assertFalse(other_ticket.is_read)
        self.assertFalse(other_user.is_read)

    def test_acknowledge_route_rejects_external_url(self):
        response = self._client(self.admin_a).post(
            "/v1/notify/acknowledge-route/",
            {"path": "https://attacker.test/finance"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_action_routes_and_acknowledgment_stay_aligned_across_modules(self):
        from .serializers import NotificationListSerializer

        cases = (
            ("ticket.test_route", {"ticket_id": 81}, "/support/tickets/81"),
            ("workflow.stage_activated", {"workflow_instance_id": 82}, "/workflow/approvals/82"),
            ("workflow.test_route", {"workflow_instance_id": 83}, "/workflow/my-submissions/83"),
            ("import.test_route", {}, "/data-imports/batches"),
            ("team.test_route", {}, "/team-management"),
            ("security.test_route", {}, "/me/security"),
            ("finance.test_route", {}, "/finance"),
            ("payments.test_route", {}, "/finance"),
            ("procurement.test_route", {}, "/procurement"),
        )
        client = self._client(self.admin_a)

        for index, (event_key, metadata, route) in enumerate(cases):
            with self.subTest(event_key=event_key):
                event_type, _ = NotificationEventType.objects.get_or_create(
                    key=event_key,
                    defaults={
                        "label": f"Route test {index}",
                        "source_module": "vs_notifications",
                        "supported_channels": [ChannelChoices.IN_APP],
                    },
                )
                notification = Notification.objects.create(
                    tenant=self.school_a.tenant,
                    recipient=self.admin_a,
                    event_type=event_type,
                    channel=ChannelChoices.IN_APP,
                    body="route test",
                    status=NotificationStatus.SENT,
                    metadata=metadata,
                )
                self.assertEqual(
                    NotificationListSerializer(notification).data["action_url"],
                    route,
                )

                response = client.post(
                    "/v1/notify/acknowledge-route/", {"path": route}, format="json",
                )

                self.assertEqual(response.status_code, 200)
                notification.refresh_from_db()
                self.assertTrue(notification.is_read)


# ---------------------------------------------------------------------------
# Settings API — security + shape + upsert
# ---------------------------------------------------------------------------

class SettingsApiTests(_NotifFixture):

    def test_settings_requires_rbac_permission(self):
        resp = self._client(self.plain_a).get("/v1/notify/settings/")
        self.assertEqual(resp.status_code, 403)

    def test_school_admin_cannot_read_other_school(self):
        # Asserting a foreign tenant is refused at the auth layer with a
        # non-enumerating 404 (never leak another tenant's existence).
        resp = self._client(self.admin_a).get(
            f"/v1/notify/settings/?tenant={self.school_b.slug}"
        )
        self.assertEqual(resp.status_code, 404)

    def test_school_admin_can_read_own_school(self):
        # No explicit ?tenant → TenantAPIClient appends the admin's own home
        # tenant, which they are entitled to read.
        resp = self._client(self.admin_a).get("/v1/notify/settings/")
        self.assertEqual(resp.status_code, 200)

    def test_matrix_shape_and_source_field(self):
        resp = self._client(self.cx).get("/v1/notify/settings/")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]
        self.assertTrue(rows)
        row = rows[0]
        for field in ["event_type_key", "event_type_label", "source_module",
                      "channel", "is_enabled", "is_transactional", "source"]:
            self.assertIn(field, row)
        self.assertIn(row["source"], {"platform", "default"})
        tx = [r for r in rows if r["event_type_key"] == "user.password_reset"]
        self.assertTrue(tx and all(r["is_transactional"] for r in tx))

    def test_patch_upsert_creates_override_row(self):
        resp = self._client(self.cx).patch(
            "/v1/notify/settings/update/",
            {"updates": [{"event_type_key": "ticket.created",
                          "channel": "email", "is_enabled": False}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        # A PLATFORM-kind assertion manages the platform DEFAULT layer — the
        # tenant-NULL rows every school inherits (codex-tenant rows would be
        # invisible to school dispatch resolution).
        row = NotificationSetting.all_objects.get(
            tenant__isnull=True, event_type__key="ticket.created", channel="email",
        )
        self.assertFalse(row.is_enabled)
        entries = resp.json()["data"]
        self.assertEqual(entries[0]["event_type_key"], "ticket.created")
        self.assertFalse(entries[0]["is_enabled"])

    def test_patch_school_scoped_writes_school_row(self):
        # A school admin's PATCH resolves to their own tenant assertion, writing
        # a tenant-scoped override row (no ?school= needed any more).
        self._client(self.admin_a).patch(
            "/v1/notify/settings/update/",
            {"updates": [{"event_type_key": "ticket.created",
                          "channel": "email", "is_enabled": False}]},
            format="json",
        )
        self.assertTrue(
            NotificationSetting.all_objects.filter(
                tenant=self.school_a.tenant, event_type__key="ticket.created",
                channel="email", is_enabled=False,
            ).exists()
        )

    def test_patch_reject_disable_in_app(self):
        resp = self._client(self.cx).patch(
            "/v1/notify/settings/update/",
            {"updates": [{"event_type_key": "ticket.created",
                          "channel": "in_app", "is_enabled": False}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        errs = resp.json()["error"]["updates"]
        self.assertEqual(errs[0]["error_code"], NotificationErrorCode.IN_APP_ALWAYS_ENABLED)

    def test_patch_reject_transactional_toggle(self):
        resp = self._client(self.cx).patch(
            "/v1/notify/settings/update/",
            {"updates": [{"event_type_key": "user.password_reset",
                          "channel": "email", "is_enabled": False}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        errs = resp.json()["error"]["updates"]
        self.assertEqual(
            errs[0]["error_code"], NotificationErrorCode.TRANSACTIONAL_NOT_CONFIGURABLE,
        )

    def test_patch_reject_unknown_event(self):
        resp = self._client(self.cx).patch(
            "/v1/notify/settings/update/",
            {"updates": [{"event_type_key": "does.not.exist",
                          "channel": "email", "is_enabled": True}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# History — school scoping
# ---------------------------------------------------------------------------

class HistoryScopingTests(_NotifFixture):

    def setUp(self):
        super().setUp()
        _grant_school_permission(
            self.admin_a, self.school_a, NotificationPermission.AUDIT_ACTIVITY,
        )
        et = self._event("ticket.created")
        self.n_a = Notification.objects.create(
            tenant=self.school_a.tenant, recipient=self.admin_a, event_type=et,
            channel=ChannelChoices.IN_APP, body="a", status=NotificationStatus.SENT,
        )
        self.n_b = Notification.objects.create(
            tenant=self.school_b.tenant, recipient=None, unregistered_email="b@test.com",
            event_type=et, channel=ChannelChoices.EMAIL, body="b",
            status=NotificationStatus.SENT,
        )
        # Platform row anchors on the CX recipient's codex PLATFORM tenant.
        self.n_platform = Notification.objects.create(
            tenant=self.cx.tenant, recipient=self.cx, event_type=et,
            channel=ChannelChoices.IN_APP, body="p", status=NotificationStatus.SENT,
        )

    def test_school_admin_sees_only_own_school(self):
        resp = self._client(self.admin_a).get(
            "/v1/notify/history/?event_type_key=ticket.created"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = {r["id"] for r in resp.json()["data"]}
        self.assertIn(str(self.n_a.id), ids)
        self.assertNotIn(str(self.n_b.id), ids)
        self.assertNotIn(str(self.n_platform.id), ids)

    def test_cx_platform_scope_filter(self):
        resp = self._client(self.cx).get("/v1/notify/history/?scope=platform")
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = {r["id"] for r in resp.json()["data"]}
        self.assertIn(str(self.n_platform.id), ids)
        self.assertNotIn(str(self.n_a.id), ids)

    def test_cx_requires_a_filter(self):
        resp = self._client(self.cx).get("/v1/notify/history/")
        self.assertEqual(resp.status_code, 422)


# ---------------------------------------------------------------------------
# Empty-list / object response shapes
# ---------------------------------------------------------------------------

class ResponseShapeTests(_NotifFixture):

    def test_unread_count_object_shape(self):
        resp = self._client(self.plain_a).get("/v1/notify/unread-count/")
        self.assertEqual(resp.json()["data"], {"unread_count": 0})

    def test_settings_matrix_returns_list(self):
        resp = self._client(self.cx).get("/v1/notify/settings/")
        self.assertIsInstance(resp.json()["data"], list)


# ---------------------------------------------------------------------------
# seed_notification_permissions — grants land in the tenant RBAC tables
# ---------------------------------------------------------------------------

class SeedNotificationPermissionsTests(TestCase):
    """The communication permission seed must grant into TenantRolePermission on
    the codex platform roles (the legacy platform-role grant path is retired)."""

    def setUp(self):
        from django.core.management import call_command
        call_command("seed_actions", verbosity=0)
        call_command("seed_notification_permissions", verbosity=0)

    def test_platform_roles_granted_in_tenant_table(self):
        from vs_rbac.models import Permission, TenantRolePermission

        for key in (
            "communication.notification_templates.configure",
            "communication.communication_permissions.enforce",
            "communication.message_activity.audit",
        ):
            self.assertTrue(Permission.objects.filter(key=key).exists(), key)
            for role_key in ("xvs_super_admin", "xvs_platform_admin"):
                self.assertTrue(
                    TenantRolePermission.objects.filter(
                        role__key=role_key, role__tenant__kind="PLATFORM",
                        permission_id=key, granted=True,
                    ).exists(),
                    f"{role_key}:{key}",
                )

    def test_native_school_role_backfilled_in_tenant_table(self):
        from vs_rbac.models import TenantRolePermission, TenantRoleTemplate
        from vs_schools.models import School

        school = School.objects.create(name="Notif Backfill", slug="notif-bf", code="NBF")
        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key="school_admin", name="School Admin",
            is_system_role=True,
        )
        from django.core.management import call_command
        call_command("seed_notification_permissions", verbosity=0)

        keys = set(
            TenantRolePermission.objects
            .filter(role=role, granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertIn("communication.communication_permissions.enforce", keys)
        self.assertIn("communication.message_activity.audit", keys)
