"""
Tests for vs_user auth flows.

Covers the security-review fixes:
- B13: lock state is only revealed after a correct password (no oracle).
- B14: failed attempts record the email as entered, even for unknown accounts.
- B10: logout ends only the submitted session, not every device.
- B11: refresh rotation updates only the matching session's JTI.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from vs_user.models import AccountLockout, AuthAttempt, LoginSession, User
from vs_user.services.auth import LoginService


class UserListScopeTests(TestCase):
    """Platform user lists keep CX and tenant-bound accounts separate."""

    def setUp(self):
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory
        from vs_user.views.accounts import UserAccountViewSet

        self.cx_user = make_cx_user(email="scope-cx@codex.test")
        school = make_school(name="Scope School", slug="scope-school")
        self.school_user = make_school_admin(school, email="scope-admin@school.test")
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key="school-administrator", name="School Administrator",
        )
        TenantUserRoleAssignment.objects.create(
            tenant=school.tenant,
            user=self.school_user,
            role=role,
            assignment_status="ACTIVE",
        )
        self.request_class = Request
        self.request_factory = APIRequestFactory()
        self.view_class = UserAccountViewSet

    def _queryset_for(self, query: str):
        request = self.request_class(self.request_factory.get(f"/v1/user/users/{query}"))
        request._user = self.cx_user
        view = self.view_class()
        view.request = request
        return view.get_queryset()

    def test_cx_user_type_filter_returns_only_cx_staff(self):
        users = self._queryset_for("?user_type=CX_STAFF")
        self.assertQuerySetEqual(users, [self.cx_user], transform=lambda user: user)

    def test_school_scope_excludes_cx_staff(self):
        users = self._queryset_for("?scope=school")
        self.assertQuerySetEqual(users, [self.school_user], transform=lambda user: user)

    def test_school_scope_serializes_placement_and_active_role(self):
        from vs_user.serializers import UserListSerializer

        user = self._queryset_for("?scope=school").get(pk=self.school_user.pk)
        data = UserListSerializer(user).data

        self.assertEqual(data["school_name"], "Scope School")
        self.assertEqual(data["role"], "School Administrator")


def make_cx_user(email="staff@codex.test", password="Str0ng!pass123"):
    return User.objects.create_user(
        email=email,
        password=password,
        user_type="CX_STAFF",
        status="ACTIVE",
        first_name="Code",
        last_name="Xer",
    )


def make_school(name="Caleb International College", slug="caleb"):
    from vs_schools.models import School
    return School.objects.create(name=name, slug=slug, status="ACTIVE")


def make_school_admin(school, email="admin@caleb.test", password="Str0ng!pass123"):
    return User.objects.create_user(
        email=email,
        password=password,
        user_type="SCHOOL_ADMIN",
        status="ACTIVE",
        first_name="Ada",
        last_name="Obi",
        school=school,
    )


class LoginLockoutOracleTests(TestCase):
    """B13 — wrong-password attempts must never reveal the locked state."""

    def setUp(self):
        self.password = "Str0ng!pass123"
        self.user = make_cx_user(password=self.password)

    def _lock(self):
        lockout, _ = AccountLockout.objects.get_or_create(user=self.user)
        lockout.register_failure(ip="127.0.0.1", lock_threshold=1, lock_minutes=15)
        lockout.save()
        self.assertTrue(lockout.is_locked_now())

    def test_wrong_password_on_locked_account_says_invalid_credentials(self):
        self._lock()
        with self.assertRaises(ValueError) as ctx:
            LoginService.login(self.user.email, "wrong-password")
        self.assertEqual(ctx.exception.args[0]["code"], "INVALID_CREDENTIALS")

    def test_correct_password_on_locked_account_reveals_lock(self):
        self._lock()
        with self.assertRaises(ValueError) as ctx:
            LoginService.login(self.user.email, self.password)
        self.assertEqual(ctx.exception.args[0]["code"], "ACCOUNT_LOCKED")

    def test_successful_login_returns_tokens(self):
        result = LoginService.login(self.user.email, self.password)
        self.assertIn("access", result)
        self.assertIn("refresh", result)
        self.assertTrue(
            LoginSession.objects.filter(user=self.user, is_active=True).exists()
        )


class FailedAttemptAuditTests(TestCase):
    """B14 — the attempted email is recorded even when no account matches."""

    def test_unknown_email_is_recorded_as_entered(self):
        with self.assertRaises(ValueError):
            LoginService.login("ghost@nowhere.test", "whatever")
        attempt = AuthAttempt.objects.latest("id")
        self.assertEqual(attempt.email_entered, "ghost@nowhere.test")
        self.assertEqual(attempt.result, AuthAttempt.Result.FAIL)

    def test_known_email_wrong_password_recorded(self):
        user = make_cx_user()
        with self.assertRaises(ValueError):
            LoginService.login(user.email, "wrong-password")
        attempt = AuthAttempt.objects.latest("id")
        self.assertEqual(attempt.email_entered, user.email)


class SessionScopedLogoutTests(TestCase):
    """B10/B11 — multi-device session integrity."""

    def setUp(self):
        self.password = "Str0ng!pass123"
        self.user = make_cx_user(password=self.password)
        self.device_a = LoginService.login(self.user.email, self.password)
        self.device_b = LoginService.login(self.user.email, self.password)
        self.assertEqual(
            LoginSession.objects.filter(user=self.user, is_active=True).count(), 2
        )

    def _client(self, login_result):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login_result['access']}")
        return client

    def test_logout_ends_only_the_submitted_session(self):
        resp = self._client(self.device_a).post(
            "/v1/user/auth/logout/", {"refresh": self.device_a["refresh"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        active = LoginSession.objects.filter(user=self.user, is_active=True)
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.first().id, self.device_b["session_id"])

    def test_refresh_updates_only_matching_session_jti(self):
        session_b_before = LoginSession.objects.get(pk=self.device_b["session_id"])

        resp = self._client(self.device_a).post(
            "/v1/user/auth/token/refresh/", {"refresh": self.device_a["refresh"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        session_b_after = LoginSession.objects.get(pk=self.device_b["session_id"])
        self.assertEqual(session_b_before.refresh_jti, session_b_after.refresh_jti)


# =============================================================================
# WP-B1 — school branding in auth payloads (A.1)
# =============================================================================

class SchoolBrandingPayloadTests(TestCase):
    """The login / me payloads carry a nested `school` object for school users.

    Login is exercised through ``LoginService.login`` (the service returns the
    payload dict that the view wraps verbatim) with a real request so absolute
    logo URLs are built — this mirrors the existing tests in this module and
    sidesteps the login rate-throttle. ``/me`` is hit over HTTP.
    """

    def setUp(self):
        from django.test import RequestFactory
        self.password = "Str0ng!pass123"
        self.school = make_school()
        self.admin = make_school_admin(self.school, password=self.password)
        self.factory = RequestFactory()

    def _login(self, user, password):
        request = self.factory.post("/v1/user/auth/login/")
        return LoginService.login(user.email, password, request=request)

    def _me(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get("/v1/user/auth/me/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]

    def _add_branding(self, logo=True):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from vs_schools.models import SchoolBranding
        branding = SchoolBranding(school=self.school)
        if logo:
            branding.logo = SimpleUploadedFile(
                "caleb.png", b"\x89PNG\r\n\x1a\n-fake-png", content_type="image/png"
            )
        branding.save()
        return branding

    def test_login_includes_school_object_with_logo(self):
        self._add_branding(logo=True)
        data = self._login(self.admin, self.password)

        self.assertIn("school", data)
        school = data["school"]
        self.assertIsNotNone(school)
        self.assertEqual(school["id"], self.school.id)
        self.assertEqual(school["name"], self.school.name)
        self.assertEqual(school["slug"], self.school.slug)
        self.assertIsNotNone(school["logo"])
        # Absolute URL built from the request.
        self.assertTrue(school["logo"].startswith("http"))
        self.assertIn("caleb", school["logo"])

    def test_login_school_object_logo_null_when_no_branding(self):
        data = self._login(self.admin, self.password)
        self.assertIsNotNone(data["school"])
        self.assertEqual(data["school"]["name"], self.school.name)
        self.assertIsNone(data["school"]["logo"])

    def test_login_school_object_logo_null_when_branding_has_no_logo(self):
        self._add_branding(logo=False)
        data = self._login(self.admin, self.password)
        self.assertIsNotNone(data["school"])
        self.assertIsNone(data["school"]["logo"])

    def test_login_school_null_for_cx_staff(self):
        cx = make_cx_user(password=self.password)
        data = self._login(cx, self.password)
        self.assertIn("school", data)
        self.assertIsNone(data["school"])

    def test_existing_flat_fields_unchanged(self):
        # console-fe compatibility: additive change must not touch existing fields.
        # The user payload now carries tenant identity (tenant_slug/tenant_name)
        # instead of the legacy flat school_name; school identity lives in the
        # nested `school` object.
        data = self._login(self.admin, self.password)
        self.assertIn("user", data)
        self.assertIn("access", data)
        self.assertIn("permissions", data)
        self.assertEqual(data["user"]["tenant_name"], self.school.name)
        self.assertEqual(data["user"]["tenant_slug"], self.school.slug)
        self.assertEqual(data["school"]["name"], self.school.name)

    def test_me_returns_same_school_object(self):
        self._add_branding(logo=True)
        login_data = self._login(self.admin, self.password)
        me_data = self._me(self.admin)

        self.assertIn("school", me_data)
        self.assertEqual(me_data["school"]["id"], login_data["school"]["id"])
        self.assertEqual(me_data["school"]["name"], login_data["school"]["name"])
        self.assertEqual(me_data["school"]["slug"], login_data["school"]["slug"])
        self.assertEqual(me_data["school"]["logo"], login_data["school"]["logo"])

    def test_me_school_null_for_cx_staff(self):
        cx = make_cx_user(password=self.password)
        me_data = self._me(cx)
        self.assertIsNone(me_data["school"])


class EmailFailureResilienceTests(TestCase):
    """
    Regression — an SMTP outage during eager (in-process) email sending must
    not 500 the request.

    Email now flows through the vs_notifications engine: the vs_user task
    dispatches (synchronously, cheaply) and the engine's
    deliver_email_notification does the SMTP send. Under eager mode the delivery
    task runs in-process; its eager guard treats the first failure as final, so
    celery.exceptions.Retry never propagates through the HTTP request even when
    smtp.zoho.com is unreachable — the PasswordResetRequest / UserInvitation row
    is already persisted.
    """

    RESET_URL = "/v1/user/auth/password/reset/request/"

    def setUp(self):
        from apps.celery import app as celery_app
        from vs_notifications.services.seed import (
            seed_event_types, seed_notification_templates,
        )

        # The event registry + DB templates are not seeded by migrations, so the
        # engine has nothing to render/dispatch without this.
        seed_event_types()
        seed_notification_templates()

        self.celery_app = celery_app
        self._old_eager = celery_app.conf.task_always_eager
        self._old_propagates = celery_app.conf.task_eager_propagates
        # Mirror staging: tasks run in-process and exceptions propagate.
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        self.client = APIClient()

    def tearDown(self):
        self.celery_app.conf.task_always_eager = self._old_eager
        self.celery_app.conf.task_eager_propagates = self._old_propagates

    @staticmethod
    def _smtp_down(*args, **kwargs):
        import smtplib
        raise smtplib.SMTPConnectError(421, "smtp.zoho.com unreachable")

    def test_reset_request_returns_200_when_eager_smtp_send_fails(self):
        from unittest import mock

        from vs_user.models import PasswordResetRequest

        user = make_cx_user(email="reset-smtp@codex.test")
        with mock.patch("vs_notifications.tasks.send_email", side_effect=self._smtp_down):
            resp = self.client.post(self.RESET_URL, {"email": user.email}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            PasswordResetRequest.objects.filter(user=user, used_at__isnull=True).exists(),
            "reset row must exist so the emailed link (once SMTP recovers) works",
        )

    def test_reset_request_returns_200_when_broker_down_and_smtp_fails(self):
        """Broker unreachable → .delay() raises → .apply() fallback → SMTP fails."""
        from unittest import mock

        from vs_user import tasks
        from vs_user.models import PasswordResetRequest

        user = make_cx_user(email="reset-broker@codex.test")
        self.celery_app.conf.task_always_eager = False  # force the broker path
        with mock.patch.object(
            tasks.send_password_reset_email_task, "delay",
            side_effect=Exception("broker connection refused"),
        ), mock.patch("vs_notifications.tasks.send_email", side_effect=self._smtp_down):
            resp = self.client.post(self.RESET_URL, {"email": user.email}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            PasswordResetRequest.objects.filter(user=user, used_at__isnull=True).exists()
        )

    def test_invitation_email_eager_smtp_failure_marks_failed_without_raising(self):
        """Engine path: dispatch → deliver task fails under eager → receiver marks
        the invitation FAILED via the notification_failed signal, no retry."""
        from datetime import timedelta
        from unittest import mock

        from django.utils import timezone

        from vs_user.models import UserInvitation
        from vs_user.tasks import send_invitation_email_task

        user = make_cx_user(email="invitee@codex.test")
        invitation = UserInvitation.objects.create(
            user=user,
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
            is_used=False,
        )

        with mock.patch("vs_notifications.tasks.send_email", side_effect=self._smtp_down):
            # dispatch enqueues the delivery task via transaction.on_commit —
            # capture and execute it so the eager delivery runs and fails.
            with self.captureOnCommitCallbacks(execute=True):
                send_invitation_email_task.apply(
                    kwargs={"activation_key": str(user.activation_key)}
                )

        invitation.refresh_from_db()
        self.assertEqual(invitation.email_status, UserInvitation.EmailStatus.FAILED)
        self.assertEqual(
            invitation.email_attempts, 1,
            "eager mode must not retry in-process",
        )
        # The engine stores the raw exception string on failure_reason, which the
        # receiver copies into email_last_error (the old per-exception label
        # classifier is gone).
        self.assertIn("unreachable", invitation.email_last_error)


from django.test import override_settings


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="CodeX System <system@codexng.com>",
    EMAIL_CC=[],
    FRONTEND_BASE_URL="https://intranet.codexng.com",
)
class InvitationEngineDispatchTests(TestCase):
    """The vs_user email tasks now flow through the notification engine.

    Verifies the dispatch record + metadata, the receiver-driven invitation
    tracking on success, and the per-message From (from_name) parity.
    """

    def setUp(self):
        from vs_notifications.services.seed import (
            seed_event_types, seed_notification_templates,
        )
        seed_event_types()
        seed_notification_templates()

    def _invitation_for(self, user, invited_by=None):
        from datetime import timedelta

        from django.utils import timezone

        from vs_user.models import UserInvitation
        return UserInvitation.objects.create(
            user=user, invited_by=invited_by or user,
            expires_at=timezone.now() + timedelta(days=7), is_used=False,
        )

    def test_invitation_dispatch_creates_notification_with_activation_key(self):
        from unittest import mock

        from vs_notifications.constants import ChannelChoices
        from vs_notifications.models import Notification
        from vs_user.tasks import send_invitation_email_task

        user = make_cx_user(email="invited@codex.test")
        user.invited_by_name = "Ada Admin"
        user.save(update_fields=["invited_by_name"])

        with mock.patch("vs_notifications.tasks.deliver_email_notification.delay"):
            send_invitation_email_task.apply(
                kwargs={"activation_key": str(user.activation_key)}
            )

        notif = Notification.objects.get(recipient=user, channel=ChannelChoices.EMAIL)
        self.assertEqual(notif.event_type.key, "user.invited")
        self.assertEqual(notif.metadata.get("activation_key"), str(user.activation_key))
        self.assertEqual(notif.metadata.get("from_name"), "Ada Admin")

    def test_successful_delivery_updates_invitation_via_receiver(self):
        from django.core import mail

        from vs_user.models import UserInvitation
        from vs_user.tasks import send_invitation_email_task

        user = make_cx_user(email="invited-ok@codex.test")
        invitation = self._invitation_for(user)

        with self.captureOnCommitCallbacks(execute=True):
            send_invitation_email_task.apply(
                kwargs={"activation_key": str(user.activation_key)}
            )

        invitation.refresh_from_db()
        self.assertEqual(invitation.email_status, UserInvitation.EmailStatus.SENT)
        self.assertEqual(invitation.email_attempts, 1)
        self.assertIsNotNone(invitation.email_sent_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_from_name_lands_in_outgoing_from_header(self):
        from django.core import mail

        from vs_user.tasks import send_invitation_email_task

        user = make_cx_user(email="fromname@codex.test")
        user.invited_by_name = "Bola Inviter"
        user.save(update_fields=["invited_by_name"])
        self._invitation_for(user)

        with self.captureOnCommitCallbacks(execute=True):
            send_invitation_email_task.apply(
                kwargs={"activation_key": str(user.activation_key)}
            )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Bola Inviter", mail.outbox[0].from_email)
        # Address portion is preserved from DEFAULT_FROM_EMAIL.
        self.assertIn("system@codexng.com", mail.outbox[0].from_email)

    def test_password_reset_dispatch_creates_notification(self):
        from unittest import mock

        from vs_notifications.constants import ChannelChoices
        from vs_notifications.models import Notification
        from vs_user.tasks import send_password_reset_email_task

        user = make_cx_user(email="pwreset@codex.test")

        with mock.patch("vs_notifications.tasks.deliver_email_notification.delay"):
            send_password_reset_email_task.apply(
                kwargs={
                    "activation_key": str(user.activation_key),
                    "origin": "SELF",
                    "sender_name": "CodeX System",
                }
            )

        notif = Notification.objects.get(recipient=user, channel=ChannelChoices.EMAIL)
        self.assertEqual(notif.event_type.key, "user.password_reset")
        self.assertEqual(notif.metadata.get("from_name"), "CodeX System")
        self.assertIn("reset-password", notif.body)
