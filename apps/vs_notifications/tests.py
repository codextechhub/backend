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
# Runs on Postgres, the only engine the platform uses - so the conditional
# UniqueConstraints here are exercised the way production enforces them.
# =============================================================================

from unittest import mock

from django.contrib.auth import get_user_model
from django.core import checks as checks_module
from django.core.checks.registry import registry as check_registry
from django.core import mail
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings

from schools.vs_schools.models import School

from .constants import ChannelChoices, NotificationErrorCode, NotificationPermission, NotificationStatus
from .models import (
    Notification,
    NotificationEventType,
    NotificationSetting,
    NotificationTemplate,
)
from .services.dispatch import NotificationService, UnregisteredRecipient
from .services.settings import resolve_channels, resolve_channels_bulk
from .services.seed import seed_notification_templates, seed_platform_settings
from . import signals

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _grant_school_permission(user, school, permission_key):
    """Build the full RBAC chain so *user* holds *permission_key* in *school*."""
    from vs_rbac.tests.helpers import scope_for_key
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
    perm, _ = Permission.objects.get_or_create(
        module=module, resource=resource, action=action,
        defaults={"scope": scope_for_key(permission_key)},
    )

    role = TenantRoleTemplate.objects.create(
        tenant=school.tenant, key=f"role-{permission_key.replace('.', '-')}",
        name=f"Role {permission_key}",
    )
    TenantRolePermission.objects.create(role=role, permission=perm, granted=True)
    TenantUserRoleAssignment.objects.create(
        tenant=school.tenant, user=user, role=role, assignment_status="ACTIVE",
    )


class _NotifFixture(TestCase):
    """Seeds templates/platform settings and builds users + schools.

    The event types themselves are not seeded here: migration 0008 installs the
    whole registry, so every database already has them. Templates and platform
    settings still need a call, since nothing installs those.
    """

    def setUp(self):
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
            email="admin-a@test.com", password="x", user_type="STAFF",
            status="ACTIVE", first_name="Ada", last_name="Admin", tenant=self.school_a.tenant,
        )
        _grant_school_permission(
            self.admin_a, self.school_a, NotificationPermission.ENFORCE_PERMISSIONS,
        )

        # A plain school user with no RBAC grants (for 403 tests).
        self.plain_a = User.objects.create_user(
            email="plain-a@test.com", password="x", user_type="STAFF",
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
        404 tests) build the URL with an explicit ?tenant=<other slug> - the
        client only appends when the path has no tenant param.
        """
        from core.test_utils import TenantAPIClient
        return TenantAPIClient(user)

    def _event(self, key):
        return NotificationEventType.objects.get(key=key)


# ---------------------------------------------------------------------------
# resolve_channels - layering
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
# resolve_channels_bulk - layering across multiple event types, one query
# ---------------------------------------------------------------------------

class ResolveChannelsBulkTests(_NotifFixture):

    def test_layering_across_multiple_event_types_one_call(self):
        """school beats platform beats default - for several event types at once."""
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
        # et_tx: a disabled row must be ignored - transactional always fires.
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
        """1 event-type query + 1 settings query - no per-event resolve queries."""
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

    def test_unregistered_recipient_gets_no_in_app_record(self):
        """An address is not an inbox.

        Several billing events support both channels but are only ever sent to
        payers with no console account. Creating an in-app row for one produced a
        record with no recipient user that nobody could open - one per send, and
        more once re-sends existed.
        """
        from vs_notifications.notify import UnregisteredRecipient

        with mock.patch("vs_notifications.tasks.deliver_email_notification.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                ids = NotificationService.send(
                    event_key="ticket.created",
                    context={"student_first_name": "Sam", "student_last_name": "Doe"},
                    recipients=[],
                    unregistered_recipients=[
                        UnregisteredRecipient(email="payer@example.com", name="Payer"),
                    ],
                )

        notifs = Notification.objects.filter(id__in=ids)
        self.assertEqual(notifs.count(), 1)
        self.assertEqual(notifs.first().channel, ChannelChoices.EMAIL)
        self.assertFalse(notifs.filter(channel=ChannelChoices.IN_APP).exists())

    def test_registered_recipient_still_gets_in_app(self):
        """The guard is about having an account, not about the event."""
        rcpt = self._recipient("registered@test.com")
        from vs_notifications.notify import UnregisteredRecipient

        with mock.patch("vs_notifications.tasks.deliver_email_notification.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                ids = NotificationService.send(
                    event_key="ticket.created",
                    context={"student_first_name": "Sam", "student_last_name": "Doe"},
                    recipients=[rcpt],
                    unregistered_recipients=[
                        UnregisteredRecipient(email="payer@example.com", name="Payer"),
                    ],
                )

        notifs = Notification.objects.filter(id__in=ids)
        in_app = notifs.filter(channel=ChannelChoices.IN_APP)
        self.assertEqual(in_app.count(), 1)
        self.assertEqual(in_app.first().recipient_id, rcpt.id)
        # Both targets still get their email.
        self.assertEqual(notifs.filter(channel=ChannelChoices.EMAIL).count(), 2)

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
        # The pre-flight FAILED signal fires from on_commit - capture it.
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
                   DEFAULT_FROM_EMAIL="CodeX System <system@codexng.com>", EMAIL_BCC=[])
class DeliveryTaskTests(_NotifFixture):

    def _pending_email(self, html=""):
        et = self._event("ticket.created")
        # tenant is required (non-null); its value is irrelevant to delivery - the
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

    def test_metadata_bcc_is_passed_to_the_email_backend(self):
        from .tasks import deliver_email_notification
        notif = self._pending_email()
        notif.metadata = {"bcc": ["backend-test@codexng.com"]}
        notif.save(update_fields=["metadata"])

        deliver_email_notification(str(notif.id))

        self.assertEqual(mail.outbox[0].bcc, ["backend-test@codexng.com"])
        # The whole point of the switch: the recipient must not see it.
        self.assertEqual(mail.outbox[0].cc, [])

    @override_settings(EMAIL_BCC=["monitor@codexng.com"])
    def test_notification_without_metadata_bcc_keeps_the_platform_default(self):
        """An absent "bcc" key means "no opinion", not "copy nobody".

        Procurement and finance narrow the copy for external mail by setting the key
        explicitly. If that were expressed as an always-present list, every other
        notification on the platform would silently lose its monitoring copy.
        """
        from .tasks import deliver_email_notification
        notif = self._pending_email()

        deliver_email_notification(str(notif.id))

        self.assertEqual(mail.outbox[0].bcc, ["monitor@codexng.com"])

    @override_settings(EMAIL_BCC=["monitor@codexng.com"])
    def test_explicit_empty_metadata_bcc_suppresses_the_platform_default(self):
        """An explicit empty list is a real instruction: copy nobody."""
        from .tasks import deliver_email_notification
        notif = self._pending_email()
        notif.metadata = {"bcc": []}
        notif.save(update_fields=["metadata"])

        deliver_email_notification(str(notif.id))

        self.assertEqual(mail.outbox[0].bcc, [])

    def test_metadata_attachment_is_loaded_from_storage(self):
        from .tasks import deliver_email_notification
        storage_name = default_storage.save(
            "test-notifications/purchase-order.pdf", ContentFile(b"%PDF-1.4 test"),
        )
        self.addCleanup(default_storage.delete, storage_name)
        notif = self._pending_email()
        notif.metadata = {
            "attachments": [{
                "name": "Purchase-Order-PO-001.pdf",
                "storage_name": storage_name,
                "content_type": "application/pdf",
            }],
        }
        notif.save(update_fields=["metadata"])

        deliver_email_notification(str(notif.id))

        self.assertEqual(len(mail.outbox[0].attachments), 1)
        attachment = mail.outbox[0].attachments[0]
        self.assertEqual(attachment[0], "Purchase-Order-PO-001.pdf")
        self.assertEqual(attachment[2], "application/pdf")

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
# Feed retrieve - cross-user isolation
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
# Settings API - security + shape + upsert
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
        # A PLATFORM-kind assertion manages the platform DEFAULT layer - the
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
# History - school scoping
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

    def test_search_narrows_history_and_counts_as_a_filter(self):
        resp = self._client(self.admin_a).get("/v1/notify/history/?search=a")
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = {r["id"] for r in resp.json()["data"]}
        self.assertEqual(ids, {str(self.n_a.id)})

    def test_history_search_stays_inside_the_callers_tenant(self):
        resp = self._client(self.admin_a).get("/v1/notify/history/?search=b@test.com")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"], [])


# ---------------------------------------------------------------------------
# Inbox order + server-side search
#
# The inbox opens on what still needs attention, and searching must narrow the
# QUERYSET - a page of results that was filtered in the browser reports the
# wrong totals on every page after the first.
# ---------------------------------------------------------------------------

class FeedOrderAndSearchTests(_NotifFixture):

    def setUp(self):
        super().setUp()
        self.et = self._event("ticket.created")
        self.old_unread = self._notif("Invoice INV-9001 needs approval", read=False)
        self.new_read = self._notif("Payment received for INV-7777", read=True)
        self.newest_unread = self._notif("Stock count due", read=False)

    def _notif(self, body, *, read, recipient=None):
        return Notification.objects.create(
            tenant=self.school_a.tenant, recipient=recipient or self.admin_a,
            event_type=self.et, channel=ChannelChoices.IN_APP, body=body,
            is_read=read, status=NotificationStatus.SENT,
        )

    def _ids(self, response):
        return [row["id"] for row in response.json()["data"]]

    def test_unread_comes_first_then_newest(self):
        response = self._client(self.admin_a).get("/v1/notify/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            self._ids(response),
            [str(self.newest_unread.id), str(self.old_unread.id), str(self.new_read.id)],
        )

    def test_read_filter_still_narrows_to_one_tab(self):
        response = self._client(self.admin_a).get("/v1/notify/?is_read=true")
        self.assertEqual(self._ids(response), [str(self.new_read.id)])

    def test_search_narrows_the_queryset_so_totals_match(self):
        for index in range(4):
            self._notif(f"Unrelated notice {index}", read=False)

        response = self._client(self.admin_a).get("/v1/notify/?search=INV-&page_size=1")

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        # Two records match; the page carries one and the pagination block has
        # to describe the SEARCH result, not the whole inbox.
        self.assertEqual(payload["pagination"]["totalItems"], 2)
        self.assertEqual(payload["pagination"]["totalPages"], 2)
        self.assertEqual(len(payload["data"]), 1)

    def test_search_matches_the_event_label_as_well_as_the_message(self):
        response = self._client(self.admin_a).get("/v1/notify/?search=Ticket created")
        self.assertEqual(len(self._ids(response)), 3)

    def test_search_cannot_reach_another_users_inbox(self):
        theirs = self._notif("Invoice INV-9001 for someone else", read=False,
                             recipient=self.plain_a)
        response = self._client(self.admin_a).get("/v1/notify/?search=INV-9001")
        self.assertNotIn(str(theirs.id), self._ids(response))
        self.assertEqual(self._ids(response), [str(self.old_unread.id)])

    def test_search_combines_with_the_unread_filter(self):
        response = self._client(self.admin_a).get("/v1/notify/?search=INV-&is_read=false")
        self.assertEqual(self._ids(response), [str(self.old_unread.id)])

    def test_pagination_is_stable_when_created_at_ties(self):
        from django.utils import timezone

        rows = [self._notif(f"Batch row {index}", read=False) for index in range(6)]
        # A dispatch batch writes every record with the same timestamp; without
        # a unique tiebreaker the database may order those ties differently per
        # query, and rows then repeat or vanish between pages.
        stamped = timezone.now()
        Notification.objects.filter(
            id__in=[row.id for row in rows] + [self.old_unread.id, self.newest_unread.id]
        ).update(created_at=stamped)

        client = self._client(self.admin_a)
        seen = []
        for page in (1, 2, 3):
            response = client.get(f"/v1/notify/?page={page}&page_size=3")
            self.assertEqual(response.status_code, 200, response.content)
            seen.extend(self._ids(response))

        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), Notification.objects.filter(
            recipient=self.admin_a, channel=ChannelChoices.IN_APP).count())


# ---------------------------------------------------------------------------
# Email layout - one shared visual, composed from plain text
# ---------------------------------------------------------------------------

class EmailLayoutTests(_NotifFixture):

    def setUp(self):
        super().setUp()
        from .models import NotificationTemplate
        self.template = NotificationTemplate.objects.get(
            event_type=self._event("ticket.created"), channel=ChannelChoices.EMAIL,
        )

    def _render(self, **fields):
        """
        Apply editor-style changes, save, and return the HTML a recipient gets.

        The save matters: markup is stored, so it is save() that regenerates the
        standard document. Supplying html_body means hand-written markup, which
        is preserved instead of regenerated.
        """
        from .services.render import render_notification_template

        context = fields.pop("context", {})
        if "html_body" in fields:
            fields["html_is_custom"] = True
        for name, value in fields.items():
            setattr(self.template, name, value)
        self.template.save()
        return render_notification_template(self.template, context)[2]

    def test_plain_text_body_becomes_a_structured_document(self):
        html = self._render(
            subject="Ticket {{ number }} raised",
            body=(
                "Hello Ada,\n\n"
                "TICKET DETAILS\n"
                "Reference: {{ number }}\n"
                "Priority: High\n\n"
                "- Review the request\n"
                "- Assign an owner\n"
            ),
            context={"number": "TCK-0001"},
        )
        self.assertIn("<html", html.lower())
        self.assertIn("TICKET DETAILS", html)
        self.assertIn("<h2", html)          # the caps line is a heading
        self.assertIn("<table", html)       # the Label: value run is a table
        self.assertIn("<ul", html)          # the dashes are a list
        self.assertIn("TCK-0001", html)
        self.assertIn("Ticket TCK-0001 raised", html)  # headline

    def test_rendered_values_cannot_inject_markup(self):
        """A value substituted into the stored markup is escaped, every time."""
        html = self._render(
            subject="Ticket raised",
            body="Reason: {{ reason }}",
            context={"reason": "<script>alert('x')</script>"},
        )
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert", html)

    def test_hand_written_markup_escapes_its_values_too(self):
        """The escaping is in the render, so custom HTML inherits it."""
        html = self._render(
            html_body="<html><body><p>{{ reason }}</p></body></html>",
            context={"reason": "<img src=x onerror=alert(1)>"},
        )
        self.assertIn("&lt;img", html)
        self.assertNotIn("<img src=x", html)

    def test_call_to_action_becomes_a_button_and_absorbs_the_bare_link(self):
        html = self._render(
            subject="Ticket raised",
            body="Open the ticket:\n\n{{ link }}",
            cta_label="Open the ticket",
            cta_url="{{ link }}",
            context={"link": "https://xvs.codexng.com/support/tickets/1"},
        )
        self.assertIn('href="https://xvs.codexng.com/support/tickets/1"', html)
        self.assertIn(">Open the ticket<", html)
        # Twice only: the button's href and the "paste this link" line. The
        # naked URL paragraph in the body is dropped, since the button has it.
        self.assertEqual(html.count("https://xvs.codexng.com/support/tickets/1"), 2)

    def test_a_non_http_cta_never_becomes_a_link(self):
        html = self._render(
            subject="Ticket raised",
            body="Body text.",
            cta_label="Do the thing",
            cta_url="javascript:alert(1)",
            context={},
        )
        self.assertNotIn("javascript:", html)
        self.assertNotIn(">Do the thing<", html)

    def test_custom_html_body_still_overrides_the_layout(self):
        html = self._render(
            subject="Ticket raised",
            body="Body text.",
            html_body="<html><body>bespoke {{ number }}</body></html>",
            context={"number": "TCK-1"},
        )
        self.assertEqual(html, "<html><body>bespoke TCK-1</body></html>")

    def test_in_app_templates_carry_no_html(self):
        from .models import NotificationTemplate
        from .services.render import render_notification_template

        in_app = NotificationTemplate.objects.get(
            event_type=self._event("ticket.created"), channel=ChannelChoices.IN_APP,
        )
        in_app.html_body = "<html>should be ignored</html>"
        self.assertEqual(render_notification_template(in_app, {})[2], "")

    def test_seeded_email_templates_all_compose_a_document(self):
        """Every shipped email default must render through the shared shell."""
        from .models import NotificationTemplate
        from .services.preview import sample_context, template_variables
        from .services.render import render_notification_template

        templates = NotificationTemplate.objects.filter(
            channel=ChannelChoices.EMAIL, is_active=True,
        ).select_related("event_type")
        self.assertGreater(templates.count(), 10)
        for template in templates:
            with self.subTest(event=template.event_type.key):
                variables = template_variables(
                    template.subject, template.body, template.cta_label,
                    template.cta_url, template.html_body,
                )
                _, _, html = render_notification_template(
                    template, sample_context(variables),
                )
                self.assertIn("<html", html.lower())


# ---------------------------------------------------------------------------
# Stored email HTML - the database holds what gets sent
#
# The console edits html_body directly, so the invariant these pin is: what an
# administrator reads there is what a recipient receives. A standard template
# tracks the shared layout; a hand-edited one is left exactly as written.
# ---------------------------------------------------------------------------

class StoredEmailHtmlTests(_NotifFixture):

    def setUp(self):
        super().setUp()
        from .models import NotificationTemplate
        self.template = NotificationTemplate.objects.get(
            event_type=self._event("ticket.created"), channel=ChannelChoices.EMAIL,
        )

    def test_every_seeded_email_template_stores_its_markup(self):
        from .models import NotificationTemplate

        emails = NotificationTemplate.objects.filter(channel=ChannelChoices.EMAIL)
        self.assertGreater(emails.count(), 10)
        for template in emails:
            with self.subTest(event=template.event_type.key):
                self.assertIn("<html", template.html_body.lower())

    def test_in_app_templates_store_no_markup(self):
        from .models import NotificationTemplate

        for template in NotificationTemplate.objects.filter(channel=ChannelChoices.IN_APP):
            self.assertEqual(template.html_body, "")

    def test_stored_markup_keeps_its_placeholders(self):
        self.assertIn("{{ ticket_number }}", self.template.html_body)
        self.assertNotIn("{{ ticket_number }}", self.template.html_body.replace(
            "{{ ticket_number }}", "", 1,
        ).split("</head>")[0])  # not only in the <title>

    def test_conditional_tags_survive_html_escaping(self):
        """The quotes inside {% if x == 'Y' %} must not be escaped into entities."""
        self.template.body = "{% if origin == 'ADMIN' %}Reset by an admin{% endif %} Hello."
        self.template.save()
        self.assertIn("{% if origin == 'ADMIN' %}", self.template.html_body)
        self.assertNotIn("&#x27;ADMIN&#x27;", self.template.html_body)

    def test_a_standard_template_follows_its_message(self):
        self.template.body = "A brand new sentence."
        self.template.save()
        self.assertIn("A brand new sentence.", self.template.html_body)

    def test_a_hand_edited_template_is_left_alone(self):
        self.template.html_body = "<html><body>my own markup {{ ticket_number }}</body></html>"
        self.template.html_is_custom = True
        self.template.save()
        self.template.body = "Changing the message must not touch the markup."
        self.template.save()
        self.assertEqual(
            self.template.html_body,
            "<html><body>my own markup {{ ticket_number }}</body></html>",
        )

    def test_clearing_the_custom_flag_restores_the_standard_design(self):
        self.template.html_body = "<html>mine</html>"
        self.template.html_is_custom = True
        self.template.save()
        self.template.html_is_custom = False
        self.template.save()
        self.assertEqual(self.template.html_body, self.template.standard_html())

    def test_dispatch_sends_the_stored_markup(self):
        self.template.html_body = "<html><body>SENTINEL {{ ticket_number }}</body></html>"
        self.template.html_is_custom = True
        self.template.save()

        rcpt = self._recipient()
        with mock.patch("vs_notifications.tasks.deliver_email_notification.delay"):
            NotificationService.send(
                event_key="ticket.created",
                context={"ticket_number": "TCK-77"},
                recipients=[rcpt],
                tenant=self.school_a.tenant,
            )
        email = Notification.objects.get(
            recipient=rcpt, channel=ChannelChoices.EMAIL, event_type=self.template.event_type,
        )
        self.assertEqual(email.html_body, "<html><body>SENTINEL TCK-77</body></html>")

    def _recipient(self):
        return self.admin_a


# ---------------------------------------------------------------------------
# Template management + preview (Vision Staff)
# ---------------------------------------------------------------------------

class TemplatePreviewApiTests(_NotifFixture):

    def setUp(self):
        super().setUp()
        from .models import NotificationTemplate
        self.template = NotificationTemplate.objects.get(
            event_type=self._event("user.invited"), channel=ChannelChoices.EMAIL,
        )
        self.url = f"/v1/notify/templates/{self.template.id}/preview/"

    def test_preview_requires_the_template_permission(self):
        self.assertEqual(self._client(self.plain_a).get(self.url).status_code, 403)

    def test_get_preview_renders_the_visual_without_a_payload(self):
        response = self._client(self.cx).get(self.url)

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIn("<html", data["html_body"].lower())
        self.assertIn("invitation_url", data["variables"])
        self.assertFalse(data["html_is_custom"])
        self.assertEqual(data["channel"], ChannelChoices.EMAIL)
        # Every variable the template uses got a stand-in value.
        self.assertEqual(set(data["variables"]) - set(data["context_used"]), set())
        self.assertNotIn("{{", data["body"])

    def test_post_preview_context_overrides_the_samples(self):
        response = self._client(self.cx).post(
            self.url, {"context": {"user_first_name": "Ngozi"}}, format="json",
        )
        data = response.json()["data"]
        self.assertIn("Ngozi", data["body"])
        self.assertEqual(data["context_used"]["user_first_name"], "Ngozi")

    def test_preview_sends_nothing_and_stores_nothing(self):
        before = Notification.all_objects.count()
        self._client(self.cx).get(self.url)
        self.assertEqual(Notification.all_objects.count(), before)
        self.assertEqual(len(mail.outbox), 0)

    def test_template_list_reports_variables_and_supports_search(self):
        response = self._client(self.cx).get(
            "/v1/notify/templates/?search=user.invited&channel=email"
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["event_type_key"], "user.invited")
            self.assertIn("invitation_url", row["variables"])

    def test_preview_renders_unsaved_draft_edits_without_saving(self):
        before = self.template.body
        response = self._client(self.cx).post(
            self.url,
            {"draft": {"body": "A line typed in the editor {{ school_name }}."}},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIn("A line typed in the editor", data["body"])
        self.assertIn("A line typed in the editor", data["html_body"])
        self.template.refresh_from_db()
        self.assertEqual(self.template.body, before)

    def test_preview_returns_the_markup_as_well_as_the_rendered_email(self):
        """The editor's HTML box needs the source, not the sample-filled copy."""
        response = self._client(self.cx).get(self.url)
        data = response.json()["data"]

        self.assertIn("{{ invitation_url }}", data["html_source"])
        self.assertNotIn("{{", data["html_body"])
        self.assertEqual(data["html_source"], self.template.html_body)

    def test_a_draft_that_only_changes_the_message_refreshes_the_markup(self):
        """Otherwise the preview would show new text inside the old HTML."""
        response = self._client(self.cx).post(
            self.url, {"draft": {"body": "Fresh wording."}}, format="json",
        )
        html = response.json()["data"]["html_body"]
        self.assertIn("Fresh wording.", html)
        self.assertNotIn("You have been invited to join", html)

    def test_editing_the_markup_claims_ownership_of_it(self):
        response = self._client(self.cx).patch(
            f"/v1/notify/templates/{self.template.id}/",
            {"html_body": "<html><body>hand written</body></html>"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"]["html_is_custom"])
        self.template.refresh_from_db()
        self.assertEqual(self.template.html_body, "<html><body>hand written</body></html>")

    def test_saving_the_standard_markup_back_is_not_a_hand_edit(self):
        """The editor posts its whole form; that must not freeze the design."""
        response = self._client(self.cx).patch(
            f"/v1/notify/templates/{self.template.id}/",
            {"body": "Reworded message.", "html_body": self.template.html_body},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertFalse(data["html_is_custom"])
        self.assertIn("Reworded message.", data["html_body"])

    def test_resetting_restores_the_standard_design(self):
        client = self._client(self.cx)
        client.patch(
            f"/v1/notify/templates/{self.template.id}/",
            {"html_body": "<html>mine</html>"}, format="json",
        )
        response = client.patch(
            f"/v1/notify/templates/{self.template.id}/",
            {"html_is_custom": False}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.template.refresh_from_db()
        self.assertFalse(self.template.html_is_custom)
        self.assertEqual(self.template.html_body, self.template.standard_html())

    def test_available_events_lists_only_uncovered_pairs(self):
        from .models import NotificationTemplate

        response = self._client(self.cx).get("/v1/notify/templates/available-events/")
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        covered = {
            (t.event_type.key, t.channel)
            for t in NotificationTemplate.objects.select_related("event_type")
        }
        for row in rows:
            for channel in row["channels"]:
                self.assertNotIn((row["event_type_key"], channel), covered)

    def test_available_events_requires_the_template_permission(self):
        response = self._client(self.plain_a).get("/v1/notify/templates/available-events/")
        self.assertEqual(response.status_code, 403)

    def test_a_cta_label_without_a_destination_is_rejected(self):
        response = self._client(self.cx).patch(
            f"/v1/notify/templates/{self.template.id}/",
            {"cta_label": "Press me", "cta_url": ""}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("cta_url", str(response.json()))


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
# seed_notification_permissions - grants land in the tenant RBAC tables
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
        from schools.vs_schools.models import School

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


# ---------------------------------------------------------------------------
# Pending-tenant surface (FR-012) - the inbox is open, the rest is not
# ---------------------------------------------------------------------------

class PendingTenantInboxAccessTests(_NotifFixture):
    """A school that has not gone live must be able to read its own inbox.

    Onboarding dispatches in-app notifications while the tenant is still
    PENDING (onboarding.step_completed, onboarding.go_live_ready). Those
    messages were being written and left unreadable, because /v1/notify/ was
    not part of the pending-tenant surface. NotificationViewSet now declares
    the personal-inbox actions; nothing else in the module was opened.
    """

    def setUp(self):
        super().setUp()
        from vs_tenants.models import Tenant

        self.pending_school = School.objects.create(
            name="Pending Notif", slug="pending-notif", code="PNDNT",
            status="PENDING",
        )
        self.pending_tenant = self.pending_school.tenant
        self.pending_tenant.refresh_from_db()
        self.assertEqual(self.pending_tenant.status, Tenant.Status.PENDING)

        self.pending_admin = User.objects.create_user(
            email="pending-admin@test.com", password="x", user_type="STAFF",
            status="ACTIVE", first_name="Pat", last_name="Pending",
            tenant=self.pending_tenant,
        )
        # Granted deliberately: with the permission held, a 403 on the settings
        # endpoint can only be the surface gate, never a missing grant.
        _grant_school_permission(
            self.pending_admin, self.pending_school,
            NotificationPermission.ENFORCE_PERMISSIONS,
        )

        self.onboarding_notification = Notification.objects.create(
            tenant=self.pending_tenant, recipient=self.pending_admin,
            event_type=self._event("onboarding.step_completed"),
            channel=ChannelChoices.IN_APP, body="Step complete",
            subject="Onboarding step completed", status=NotificationStatus.SENT,
        )

    # ── open: the personal inbox ──────────────────────────────────────────

    def test_pending_tenant_can_list_own_notifications(self):
        resp = self._client(self.pending_admin).get("/v1/notify/")

        self.assertEqual(resp.status_code, 200, resp.data)
        ids = [row["id"] for row in resp.json()["data"]]
        self.assertIn(str(self.onboarding_notification.id), ids)

    def test_pending_tenant_can_read_and_mark_the_inbox(self):
        """Retrieve, unread-count and the mark-read actions come with it."""
        client = self._client(self.pending_admin)

        detail = client.get(f"/v1/notify/{self.onboarding_notification.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)

        count = client.get("/v1/notify/unread-count/")
        self.assertEqual(count.status_code, 200, count.data)
        self.assertEqual(count.json()["data"]["unread_count"], 1)

        marked = client.post(
            "/v1/notify/mark-read/",
            {"ids": [str(self.onboarding_notification.id)]}, format="json",
        )
        self.assertEqual(marked.status_code, 200, marked.data)
        self.onboarding_notification.refresh_from_db()
        self.assertTrue(self.onboarding_notification.is_read)

        all_read = client.post("/v1/notify/mark-all-read/", {}, format="json")
        self.assertEqual(all_read.status_code, 200, all_read.data)

    # ── still closed: everything that is not the personal inbox ───────────

    def test_pending_tenant_is_still_refused_the_settings_surface(self):
        resp = self._client(self.pending_admin).get("/v1/notify/settings/")

        self.assertEqual(resp.status_code, 403, resp.data)
        self.assertEqual(resp.data["error"]["code"], "TENANT_NOT_LIVE")

    def test_pending_tenant_is_still_refused_history_and_the_catalogue(self):
        client = self._client(self.pending_admin)

        for path in ("/v1/notify/history/", "/v1/notify/event-types/"):
            with self.subTest(path=path):
                resp = client.get(path)
                self.assertEqual(resp.status_code, 403, resp.data)
                self.assertEqual(resp.data["error"]["code"], "TENANT_NOT_LIVE")

    # ── unchanged: a live tenant ──────────────────────────────────────────

    def test_active_tenant_behaviour_is_unchanged(self):
        client = self._client(self.admin_a)

        inbox = client.get("/v1/notify/")
        self.assertEqual(inbox.status_code, 200, inbox.data)

        settings_matrix = client.get("/v1/notify/settings/")
        self.assertEqual(settings_matrix.status_code, 200, settings_matrix.data)

        catalogue = client.get("/v1/notify/event-types/")
        self.assertEqual(catalogue.status_code, 200, catalogue.data)


# ---------------------------------------------------------------------------
# System check - an active event type with no active template sends nothing
# ---------------------------------------------------------------------------

# Snapshot the check registry as this module is imported, which is before any
# test here imports vs_notifications.checks. Anything in it arrived through an
# AppConfig.ready(), so it is the evidence that the wiring is real.
_CHECK_MODULES_AT_IMPORT_TIME = {
    check.__module__ for check in check_registry.get_checks()
}


class TemplateCoverageCheckTests(TestCase):
    """checks.check_event_types_have_templates turns the dispatcher's silent
    channel skip into a signal at check time.

    Event types are not seeded here: migration 0008 installs the whole registry,
    so the test database already has them. Templates are the thing under test,
    so each case seeds or removes them explicitly.
    """

    def setUp(self):
        from .services.seed import seed_notification_templates

        seed_notification_templates()

    def _run_check(self, databases=("default",)):
        from .checks import check_event_types_have_templates

        return check_event_types_have_templates(
            app_configs=None,
            databases=list(databases) if databases else databases,
        )

    # ── registration ──────────────────────────────────────────────────────

    def test_the_check_is_registered_against_the_database_tag(self):
        """The tag is the load-bearing part: untagged and unguarded, the check
        would query the database during collectstatic and during migrate on an
        empty database."""
        from django.core.checks import Tags

        registered = [
            check for check in check_registry.get_checks()
            if check.__module__ == "vs_notifications.checks"
        ]

        self.assertEqual(len(registered), 1, registered)
        self.assertIn(Tags.database, registered[0].tags)

    def test_the_app_config_registers_the_check_on_ready(self):
        """Registration has to come from VsNotificationsConfig.ready().

        _CHECK_MODULES_AT_IMPORT_TIME was taken before any test in this file
        imported vs_notifications.checks, so the module can only be in it
        because ready() pulled it in.
        """
        self.assertIn("vs_notifications.checks", _CHECK_MODULES_AT_IMPORT_TIME)

    # ── the healthy case ──────────────────────────────────────────────────

    def test_a_seeded_database_reports_no_gaps(self):
        """The check has to be able to reach clean, or it becomes noise people
        learn to ignore. This also guards the seed itself: a new registry entry
        with no default template fails here."""
        self.assertEqual(self._run_check(), [])

    def test_no_database_named_means_no_query_and_no_message(self):
        with self.assertNumQueries(0):
            self.assertEqual(self._run_check(databases=None), [])
            self.assertEqual(self._run_check(databases=[]), [])

    # ── the gaps it exists to find ────────────────────────────────────────

    def test_an_active_event_type_with_no_template_is_reported_by_key(self):
        event_type = NotificationEventType.objects.create(
            key="checks.probe_missing", label="Probe missing",
            source_module="vs_notifications",
            supported_channels=[ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        )

        messages = self._run_check()

        self.assertEqual(len(messages), 1, messages)
        self.assertEqual(messages[0].id, "vs_notifications.W001")
        self.assertEqual(messages[0].level, checks_module.WARNING)
        self.assertIn(event_type.key, messages[0].msg)
        self.assertIn(ChannelChoices.IN_APP, messages[0].msg)
        self.assertIn(ChannelChoices.EMAIL, messages[0].msg)
        self.assertIn("seed_notification_templates", messages[0].hint)

    def test_an_inactive_template_counts_as_missing(self):
        event_type = NotificationEventType.objects.create(
            key="checks.probe_inactive_template", label="Probe inactive template",
            source_module="vs_notifications",
            supported_channels=[ChannelChoices.EMAIL],
        )
        NotificationTemplate.objects.create(
            event_type=event_type, channel=ChannelChoices.EMAIL,
            subject="Present", body="Present but switched off.", is_active=False,
        )

        messages = self._run_check()

        self.assertEqual(len(messages), 1, messages)
        self.assertIn(event_type.key, messages[0].msg)

    def test_an_inactive_event_type_with_no_template_is_not_reported(self):
        NotificationEventType.objects.create(
            key="checks.probe_retired", label="Probe retired",
            source_module="vs_notifications",
            supported_channels=[ChannelChoices.IN_APP, ChannelChoices.EMAIL],
            is_active=False,
        )

        self.assertEqual(self._run_check(), [])

    # ── it must not break a database that has no tables yet ───────────────

    def test_absent_tables_are_reported_as_no_messages_not_as_a_crash(self):
        from django.db.utils import OperationalError, ProgrammingError

        for error in (
            ProgrammingError('relation "vs_notifications_notificationeventtype" does not exist'),
            OperationalError("could not connect to server"),
        ):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(
                    NotificationEventType.objects, "using", side_effect=error,
                ):
                    self.assertEqual(self._run_check(), [])
