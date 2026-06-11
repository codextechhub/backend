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


def make_cx_user(email="staff@codex.test", password="Str0ng!pass123"):
    return User.objects.create_user(
        email=email,
        password=password,
        user_type="CX_STAFF",
        status="ACTIVE",
        first_name="Code",
        last_name="Xer",
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
