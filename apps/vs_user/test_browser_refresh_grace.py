"""Browser sessions: a refresh whose answer never arrived does not sign anyone out.

Ngozi is a bursar at Bright Star School and Tunde works on the platform
console. Each holds one browser session whose rotating credential lives in an
HttpOnly cookie. A refresh rotates the credential before the browser has it, so
a connection that drops or a tab that closes mid-request leaves the browser
holding a credential the session has moved past. These tests pin the rule that
credential is answered again for a short while, and that nothing else old is.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from vs_audit.models import AuditEvent

from .models import LoginSession, User
from .services import session_credentials
from .services.audit import blacklist_all_user_tokens
from .services.auth import LoginService
from .services.user import UserStatusService
from .tests import make_cx_user, make_school, make_school_admin
from .tokens import CodeXRefreshToken
from .views.auth import TokenRefreshView
from .two_step_testing import without_two_step

PASSWORD = "Str0ng!pass123"
REFRESH = "/v1/user/auth/token/refresh/"


def _clock(seconds):
    """The clock *seconds* from now, for the code that decides whether a window is open."""
    return mock.patch(
        "django.utils.timezone.now",
        return_value=timezone.now() + dt.timedelta(seconds=seconds),
    )


class BrowserRefreshCase:
    """Everything the browser-session tests share; subclasses name the account."""

    email = ""
    tenant_slug = ""

    def sign_in(self):
        """A fresh browser session: the login result and a client holding its cookie."""
        login = LoginService.login(self.email, PASSWORD, tenant=self.tenant_slug)
        client = APIClient(enforce_csrf_checks=True)
        client.cookies["refresh_token"] = login["refresh"]
        client.get("/v1/user/auth/csrf/")
        return login, client

    @staticmethod
    def refresh(client, **extra):
        return client.post(
            REFRESH, {}, format="json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value, **extra,
        )

    @staticmethod
    def hold(client, raw):
        client.cookies["refresh_token"] = raw

    def lose_the_answer(self, client):
        """Refresh as the server sees it, then put back the credential the browser still holds."""
        held = client.cookies["refresh_token"].value
        self.assertEqual(self.refresh(client).status_code, 200)
        self.hold(client, held)
        return held

    @staticmethod
    def session_of(login):
        return LoginSession.all_objects.get(pk=login["session_id"])

    @staticmethod
    def blacklisted(jti):
        return BlacklistedToken.objects.filter(token__jti=jti).exists()

    def assert_refused(self, response, code):
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(response.json()["error"]["error_code"], code)
        self.assertEqual(response.cookies["refresh_token"]["max-age"], 0)


class BrowserRefreshGraceBehaviour(BrowserRefreshCase):
    """The rules a browser session keeps, whichever account holds it."""

    def setUp(self):
        cache.clear()

    def test_a_lost_answer_is_recovered_by_presenting_the_old_credential_again(self):
        login, client = self.sign_in()
        first = self.session_of(login).refresh_jti
        self.lose_the_answer(client)
        rotated = self.session_of(login).refresh_jti
        self.assertNotEqual(rotated, first)

        with _clock(30):
            retry = self.refresh(client)

        self.assertEqual(retry.status_code, 200, retry.content)
        body = retry.json()["data"]
        self.assertTrue(body["access"])
        self.assertEqual(body["session_id"], login["session_id"])
        self.assertNotIn("refresh", body)
        delivered = RefreshToken(retry.cookies["refresh_token"].value)
        self.assertEqual(str(delivered["jti"]), rotated)
        self.assertEqual(self.session_of(login).refresh_jti, rotated)

        # The browser now holds the credential the session is on, and carries on.
        self.hold(client, retry.cookies["refresh_token"].value)
        self.assertEqual(self.refresh(client).status_code, 200)
        self.assertTrue(self.session_of(login).is_active)

    def test_the_retry_gives_back_the_same_credential_however_often_it_is_asked(self):
        login, client = self.sign_in()
        held = self.lose_the_answer(client)

        delivered = set()
        for _ in range(3):
            self.hold(client, held)
            with _clock(10):
                response = self.refresh(client)
            self.assertEqual(response.status_code, 200, response.content)
            delivered.add(response.cookies["refresh_token"].value)
        self.assertEqual(len(delivered), 1)

    def test_two_windows_refreshing_with_one_credential_end_with_the_same_working_credential(self):
        login, client = self.sign_in()
        held = client.cookies["refresh_token"].value
        first = self.refresh(client)
        first_delivered = first.cookies["refresh_token"].value
        self.hold(client, held)
        second = self.refresh(client)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first_delivered, second.cookies["refresh_token"].value)
        # Whichever answer the browser keeps, it works.
        self.hold(client, first_delivered)
        self.assertEqual(self.refresh(client).status_code, 200)

    def test_the_old_credential_after_the_window_ends_the_session(self):
        login, client = self.sign_in()
        held = self.lose_the_answer(client)
        current = self.session_of(login).refresh_jti

        with _clock(session_credentials.refresh_grace().total_seconds() + 1):
            response = self.refresh(client)

        self.assert_refused(response, "TOKEN_REVOKED")
        session = self.session_of(login)
        self.assertFalse(session.is_active)
        self.assertEqual(session.end_reason, "CREDENTIAL_REUSED")
        self.assertIsNotNone(session.ended_at)
        self.assertTrue(self.blacklisted(current))

        # The credential the session had moved to is dead with it.
        self.hold(client, held)
        self.assert_refused(self.refresh(client), "TOKEN_REVOKED")

    def test_ending_a_session_for_a_reused_credential_is_audited(self):
        login, client = self.sign_in()
        self.lose_the_answer(client)

        with _clock(session_credentials.refresh_grace().total_seconds() + 1):
            self.refresh(client)

        event = AuditEvent.objects.filter(
            metadata__auth_event="TOKEN_REVOKED", metadata__reason="CREDENTIAL_REUSED",
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.metadata["session_id"], login["session_id"])

    def test_a_credential_two_rotations_old_is_refused_even_inside_the_window(self):
        login, client = self.sign_in()
        oldest = client.cookies["refresh_token"].value
        self.assertEqual(self.refresh(client).status_code, 200)
        self.assertEqual(self.refresh(client).status_code, 200)

        self.hold(client, oldest)
        self.assert_refused(self.refresh(client), "TOKEN_REVOKED")

    def test_a_window_of_zero_seconds_accepts_no_old_credential(self):
        login, client = self.sign_in()
        self.lose_the_answer(client)
        with override_settings(SESSION_REFRESH_GRACE_SECONDS=0):
            response = self.refresh(client)
        self.assert_refused(response, "TOKEN_REVOKED")
        self.assertEqual(self.session_of(login).end_reason, "CREDENTIAL_REUSED")

    def test_a_current_credential_that_cannot_be_replayed_is_rotated_instead(self):
        login, client = self.sign_in()
        held = self.lose_the_answer(client)
        current = self.session_of(login).refresh_jti
        OutstandingToken.objects.filter(jti=current).update(token="")

        with _clock(5):
            response = self.refresh(client)

        self.assertEqual(response.status_code, 200, response.content)
        reissued = RefreshToken(response.cookies["refresh_token"].value)
        self.assertNotEqual(str(reissued["jti"]), current)
        self.assertTrue(self.blacklisted(current))
        self.assertEqual(self.session_of(login).refresh_jti, str(reissued["jti"]))
        self.assertEqual(self.session_of(login).previous_refresh_jti, str(RefreshToken(held, verify=False)["jti"]))

    def test_an_expired_credential_is_answered_token_expired(self):
        login, client = self.sign_in()
        token = RefreshToken(client.cookies["refresh_token"].value)
        token.set_exp(lifetime=dt.timedelta(days=-1))
        self.hold(client, str(token))

        self.assert_refused(self.refresh(client), "TOKEN_EXPIRED")
        self.assertTrue(self.session_of(login).is_active)

    def test_garbage_is_answered_token_invalid(self):
        login, client = self.sign_in()
        self.hold(client, "not-a-token")
        self.assert_refused(self.refresh(client), "TOKEN_INVALID")

    def test_a_missing_cookie_is_answered_token_invalid(self):
        login, client = self.sign_in()
        client.cookies.pop("refresh_token")
        self.assert_refused(self.refresh(client), "TOKEN_INVALID")

    def test_an_access_token_is_not_a_refresh_credential(self):
        login, client = self.sign_in()
        self.hold(client, login["access"])
        self.assert_refused(self.refresh(client), "TOKEN_INVALID")

    def test_a_tampered_signature_is_answered_token_invalid(self):
        login, client = self.sign_in()
        raw = client.cookies["refresh_token"].value
        head, body, signature = raw.split(".")
        flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
        self.hold(client, ".".join([head, body, flipped]))
        self.assert_refused(self.refresh(client), "TOKEN_INVALID")

    def test_a_logged_out_credential_is_refused(self):
        login, client = self.sign_in()
        held = client.cookies["refresh_token"].value
        out = client.post(
            "/v1/user/auth/logout/", {}, format="json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )
        self.assertEqual(out.status_code, 200, out.content)
        self.hold(client, held)

        with _clock(5):
            self.assert_refused(self.refresh(client), "TOKEN_REVOKED")

    def test_a_forced_logout_is_not_undone_by_the_window(self):
        login, client = self.sign_in()
        self.lose_the_answer(client)
        blacklist_all_user_tokens(User.objects.get(email=self.email), reason="FORCE_LOGOUT")

        with _clock(5):
            self.assert_refused(self.refresh(client), "TOKEN_REVOKED")

    def test_a_current_credential_blacklisted_without_ending_the_session_ends_it(self):
        login, client = self.sign_in()
        BlacklistedToken.objects.get_or_create(
            token=OutstandingToken.objects.get(jti=self.session_of(login).refresh_jti),
        )

        self.assert_refused(self.refresh(client), "TOKEN_REVOKED")
        self.assertFalse(self.session_of(login).is_active)

    def test_a_suspended_account_is_refused_inside_the_window(self):
        login, client = self.sign_in()
        self.lose_the_answer(client)
        admin = make_cx_user(email="suspender@codex.test", password=PASSWORD)
        UserStatusService.suspend(User.objects.get(email=self.email), admin)

        with _clock(5):
            self.assert_refused(self.refresh(client), "TOKEN_REVOKED")

    def test_a_deactivated_account_is_refused_inside_the_window(self):
        login, client = self.sign_in()
        self.lose_the_answer(client)
        admin = make_cx_user(email="deactivator@codex.test", password=PASSWORD)
        UserStatusService.deactivate(User.objects.get(email=self.email), admin)

        with _clock(5):
            self.assert_refused(self.refresh(client), "TOKEN_REVOKED")

    def test_an_account_that_may_no_longer_sign_in_ends_its_session_inside_the_window(self):
        login, client = self.sign_in()
        self.lose_the_answer(client)
        User.objects.filter(email=self.email).update(status="SUSPENDED")

        with _clock(5):
            response = self.refresh(client)

        self.assert_refused(response, "TOKEN_REVOKED")
        session = self.session_of(login)
        self.assertFalse(session.is_active)
        self.assertEqual(session.end_reason, "NOT_ALLOWED")

    def test_an_account_that_may_no_longer_sign_in_is_refused_on_an_ordinary_refresh(self):
        login, client = self.sign_in()
        User.objects.filter(email=self.email).update(status="SUSPENDED")

        self.assert_refused(self.refresh(client), "TOKEN_REVOKED")
        self.assertEqual(self.session_of(login).end_reason, "NOT_ALLOWED")

    def test_an_inactive_account_is_refused_on_an_ordinary_refresh(self):
        login, client = self.sign_in()
        User.objects.filter(email=self.email).update(is_active=False)

        self.assert_refused(self.refresh(client), "TOKEN_REVOKED")

    def test_a_credential_for_a_deleted_account_is_refused(self):
        login, client = self.sign_in()
        token = CodeXRefreshToken.for_user(User.objects.get(email=self.email))
        token["user_id"] = "999999999"
        self.hold(client, str(token))

        self.assert_refused(self.refresh(client), "TOKEN_INVALID")

    def test_a_device_credential_is_not_a_browser_credential(self):
        login, client = self.sign_in()
        token = CodeXRefreshToken.for_user(User.objects.get(email=self.email))
        token["dk"] = "a" * 32
        self.hold(client, str(token))

        self.assert_refused(self.refresh(client), "TOKEN_INVALID")

    def test_refresh_needs_csrf(self):
        login, client = self.sign_in()
        response = client.post(REFRESH, {}, format="json")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(
            self.session_of(login).refresh_jti,
            str(RefreshToken(client.cookies["refresh_token"].value)["jti"]),
        )

    def test_the_cookie_is_set_hardened_and_the_body_never_carries_the_credential(self):
        login, client = self.sign_in()
        response = self.refresh(client)

        self.assertEqual(response.status_code, 200, response.content)
        cookie = response.cookies["refresh_token"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(set(response.json()["data"]), {"access", "session_id"})

    def test_a_rotation_registers_the_new_credential_and_revokes_the_old(self):
        login, client = self.sign_in()
        old = self.session_of(login).refresh_jti
        response = self.refresh(client)

        new = str(RefreshToken(response.cookies["refresh_token"].value)["jti"])
        outstanding = OutstandingToken.objects.get(jti=new)
        self.assertEqual(outstanding.user.email, self.email)
        self.assertGreater(outstanding.expires_at, timezone.now() + dt.timedelta(hours=23))
        self.assertTrue(self.blacklisted(old))
        self.assertFalse(self.blacklisted(new))
        session = self.session_of(login)
        self.assertEqual((session.refresh_jti, session.previous_refresh_jti), (new, old))
        self.assertIsNotNone(session.refreshed_at)

    def test_other_sessions_of_the_account_are_left_alone(self):
        login, client = self.sign_in()
        other_login, _other = self.sign_in()
        before = self.session_of(other_login).refresh_jti

        self.lose_the_answer(client)
        with _clock(session_credentials.refresh_grace().total_seconds() + 1):
            self.refresh(client)

        other = self.session_of(other_login)
        self.assertEqual((other.is_active, other.refresh_jti), (True, before))


class NoSessionRowBehaviour(BrowserRefreshCase):
    """A credential no session row knows about is rotated exactly as it always was."""

    def setUp(self):
        cache.clear()

    def credential_without_a_session(self):
        user = User.objects.get(email=self.email)
        client = APIClient(enforce_csrf_checks=True)
        client.cookies["refresh_token"] = str(CodeXRefreshToken.for_user(user))
        client.get("/v1/user/auth/csrf/")
        return client

    def test_it_rotates_and_answers_no_session_id(self):
        client = self.credential_without_a_session()
        old = client.cookies["refresh_token"].value

        response = self.refresh(client)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(set(response.json()["data"]), {"access"})
        new = RefreshToken(response.cookies["refresh_token"].value)
        self.assertNotEqual(str(new["jti"]), str(RefreshToken(old, verify=False)["jti"]))
        self.assertTrue(OutstandingToken.objects.filter(jti=new["jti"]).exists())
        self.assertTrue(self.blacklisted(RefreshToken(old, verify=False)["jti"]))

    def test_the_credential_it_rotated_away_from_is_refused_at_once(self):
        client = self.credential_without_a_session()
        held = client.cookies["refresh_token"].value
        self.assertEqual(self.refresh(client).status_code, 200)

        self.hold(client, held)
        with _clock(5):
            self.assert_refused(self.refresh(client), "TOKEN_REVOKED")

    def test_a_blacklisted_one_is_refused(self):
        client = self.credential_without_a_session()
        RefreshToken(client.cookies["refresh_token"].value).blacklist()
        self.assert_refused(self.refresh(client), "TOKEN_REVOKED")

    def test_an_inactive_account_is_refused(self):
        client = self.credential_without_a_session()
        User.objects.filter(email=self.email).update(is_active=False)
        self.assert_refused(self.refresh(client), "TOKEN_REVOKED")

    def test_a_session_that_already_ended_does_not_revive_its_old_credential(self):
        login, client = self.sign_in()
        held = client.cookies["refresh_token"].value
        client.post(
            "/v1/user/auth/logout/", {}, format="json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )
        self.hold(client, held)
        self.assert_refused(self.refresh(client), "TOKEN_REVOKED")


class StaffAtASchool(BrowserRefreshGraceBehaviour, TestCase):
    """Ngozi, a bursar at Bright Star School."""

    email = "ngozi@bright-star.test"
    tenant_slug = "bright-star"

    @classmethod
    def setUpTestData(cls):
        school = make_school(name="Bright Star School", slug="bright-star")
        make_school_admin(school, email=cls.email, password=PASSWORD)


@without_two_step
class ConsoleAccount(BrowserRefreshGraceBehaviour, TestCase):
    """Tunde, on the platform console."""

    email = "tunde@codex.test"

    @classmethod
    def setUpTestData(cls):
        user = make_cx_user(email=cls.email, password=PASSWORD)
        cls.tenant_slug = user.tenant.slug


class SchoolWithoutASession(NoSessionRowBehaviour, TestCase):
    email = "ngozi@bright-star.test"
    tenant_slug = "bright-star"

    @classmethod
    def setUpTestData(cls):
        school = make_school(name="Bright Star School", slug="bright-star")
        make_school_admin(school, email=cls.email, password=PASSWORD)


@without_two_step
class ConsoleWithoutASession(NoSessionRowBehaviour, TestCase):
    email = "tunde@codex.test"

    @classmethod
    def setUpTestData(cls):
        user = make_cx_user(email=cls.email, password=PASSWORD)
        cls.tenant_slug = user.tenant.slug


@without_two_step
class ImpersonationAndRefreshTests(BrowserRefreshCase, TestCase):
    """A refresh never starts, ends or reads a proxy session; a revoked credential ends the actor's.

    Tunde proxies into Ngozi's account at Bright Star. The proxy is a separate
    record named by the ``X-Impersonation-Session`` header on ordinary requests;
    the refresh cookie belongs to Tunde alone.
    """

    email = "tunde@codex.test"

    @classmethod
    def setUpTestData(cls):
        from vs_admin_console.models import ImpersonationSession

        cls.tunde = make_cx_user(email=cls.email, password=PASSWORD)
        cls.tenant_slug = cls.tunde.tenant.slug
        school = make_school(name="Bright Star School", slug="bright-star")
        cls.ngozi = make_school_admin(school, email="ngozi@bright-star.test", password=PASSWORD)
        cls.proxy = ImpersonationSession.objects.create(
            staff_user=cls.tunde, tenant=cls.ngozi.tenant, target_user=cls.ngozi,
            justification="Look at her fee screen.",
        )

    def setUp(self):
        cache.clear()

    def test_refreshing_while_proxying_leaves_the_proxy_alone(self):
        login, client = self.sign_in()
        held = self.lose_the_answer(client)

        response = self.refresh(client, HTTP_X_IMPERSONATION_SESSION=str(self.proxy.pk))
        self.assertEqual(response.status_code, 200, response.content)
        with _clock(10):
            self.hold(client, held)
            retry = self.refresh(client, HTTP_X_IMPERSONATION_SESSION=str(self.proxy.pk))
        self.assertEqual(retry.status_code, 200, retry.content)

        self.proxy.refresh_from_db()
        self.assertEqual(self.proxy.status, "ACTIVE")

    def test_a_reused_credential_ends_the_actors_proxy_as_signing_out_does(self):
        login, client = self.sign_in()
        self.lose_the_answer(client)

        with _clock(session_credentials.refresh_grace().total_seconds() + 1):
            self.assert_refused(self.refresh(client), "TOKEN_REVOKED")

        self.proxy.refresh_from_db()
        self.assertEqual(self.proxy.status, "ENDED")


class RefreshViewContractTests(SimpleTestCase):
    def test_it_stays_open_to_a_pending_tenant_and_names_no_tenant(self):
        self.assertTrue(TokenRefreshView.pending_tenant_surface)
        self.assertFalse(TokenRefreshView.tenant_param_required)
