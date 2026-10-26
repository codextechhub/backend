"""Store apps sign in with the credential in the body, never in a cookie.

Tunde and Bola share their mother's iPhone, and the product offers both of them
the pupils' app. That app runs its pages from inside the phone at
``capacitor://localhost``, which to the API is another site, so a cookie would
never come back. Their credentials therefore travel in the response body, rest
in the iPhone's keychain, and return in the body. Their mother signs in with the
ordinary staff-style browser session and is offered no app.

The other half of the contract is what a website must never get: a page at the
school's own address, or at ``https://evil.example``, cannot receive a body
credential, cannot present a stolen one, and cannot make the API skip its CSRF
check, because only the exact native origins are treated this way.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.settings import base as base_settings

from .models import LoginSession
from .services import device_sessions
from .services.device_sessions import DeviceSessionPolicy, register_device_session_policy
from .signals import device_session_ended
from .test_browser_session import BROWSER_ORIGIN_REGEXES, BROWSER_ORIGINS, HARDENED_COOKIE
from .tests import make_cx_user

PASSWORD = "Str0ng!pass123"
PREFIX = "device_session_"
NATIVE = "capacitor://localhost"
ANDROID = "https://localhost"
SCHOOL_PAGE = "http://localhost:5174"
EVIL = "https://evil.example"
APP = DeviceSessionPolicy(lifetime=dt.timedelta(days=30), app_path="/student/")

LOGIN = "/v1/user/auth/login/"
REFRESH = "/v1/user/auth/device/refresh/"
SIGN_OUT = "/v1/user/auth/device/sign-out/"


@override_settings(
    AUTH_BROWSER_ALLOWED_ORIGINS=BROWSER_ORIGINS + [NATIVE, ANDROID],
    AUTH_BROWSER_ALLOWED_ORIGIN_REGEXES=BROWSER_ORIGIN_REGEXES,
    NATIVE_APP_ORIGINS=[NATIVE, ANDROID],
    CSRF_TRUSTED_ORIGINS=BROWSER_ORIGINS + ["http://*.localhost:5174"],
    AUTH_DEVICE_COOKIE_PREFIX=PREFIX,
    AUTH_DEVICE_COOKIE_PATH="/v1/user/auth/",
    **HARDENED_COOKIE,
)
class NativeAppSessionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tunde = make_cx_user(email="tunde@codex.test", password=PASSWORD)
        cls.bola = make_cx_user(email="bola@codex.test", password=PASSWORD)
        cls.mother = make_cx_user(email="mother@codex.test", password=PASSWORD)

    def setUp(self):
        cache.clear()
        self.offered = {self.tunde.pk, self.bola.pk}
        register_device_session_policy(
            "test_app", lambda user: APP if user.pk in self.offered else None,
        )
        self.addCleanup(device_sessions._providers.pop, "test_app", None)
        self.client = APIClient(enforce_csrf_checks=True)

    # ── helpers ───────────────────────────────────────────────────────────

    def sign_in(self, user, *, origin=NATIVE, session="device"):
        cache.clear()
        body = {"email": user.email, "password": PASSWORD, "tenant": user.tenant.slug}
        if session is not None:
            body["session"] = session
        return self.client.post(LOGIN, body, format="json", HTTP_ORIGIN=origin)

    def native_sign_in(self, user):
        response = self.sign_in(user)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        return data["account"], data["refresh"]

    def call(self, route, account, refresh=None, *, origin=NATIVE):
        body = {"account": account}
        if refresh is not None:
            body["refresh"] = refresh
        return self.client.post(route, body, format="json", HTTP_ORIGIN=origin)

    @staticmethod
    def after(seconds):
        """The clock *seconds* from now, for the code that decides whether a window is open."""
        return mock.patch.object(
            device_sessions.timezone, "now",
            return_value=timezone.now() + dt.timedelta(seconds=seconds),
        )

    def assert_no_cookies(self, response):
        self.assertEqual(list(response.cookies), [])

    # ── what a website must never get ────────────────────────────────────

    def test_a_school_page_signing_in_for_a_device_gets_its_credential_in_a_cookie_only(self):
        response = self.sign_in(self.tunde, origin=SCHOOL_PAGE)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertNotIn("refresh", data)
        self.assertIn(f"{PREFIX}{data['account']}", response.cookies)

    def test_a_sign_in_with_no_origin_never_gets_a_body_credential(self):
        cache.clear()
        response = self.client.post(
            LOGIN,
            {"email": self.tunde.email, "password": PASSWORD,
             "tenant": self.tunde.tenant.slug, "session": "device"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("refresh", response.json()["data"])

    def test_a_school_page_cannot_use_a_stolen_credential_in_the_body(self):
        account, stolen = self.native_sign_in(self.tunde)

        refused = self.call(REFRESH, account, stolen, origin=SCHOOL_PAGE)
        self.assertEqual(refused.status_code, 403)

        ended = self.call(SIGN_OUT, account, stolen, origin=SCHOOL_PAGE)
        self.assertEqual(ended.status_code, 403)

        session = LoginSession.all_objects.get(device_key=account)
        self.assertTrue(session.is_active)
        # The credential was never rotated, so it still works from the phone.
        self.assertEqual(self.call(REFRESH, account, stolen).status_code, 200)

    def test_a_school_page_with_csrf_still_ignores_a_body_credential(self):
        account, stolen = self.native_sign_in(self.tunde)
        self.client.get("/v1/user/auth/csrf/", HTTP_ORIGIN=SCHOOL_PAGE)
        token = self.client.cookies["csrftoken"].value
        self.client.cookies.pop(f"{PREFIX}{account}", None)

        for route in (REFRESH, SIGN_OUT):
            response = self.client.post(
                route, {"account": account, "refresh": stolen}, format="json",
                HTTP_ORIGIN=SCHOOL_PAGE, HTTP_X_CSRFTOKEN=token,
            )
            if route == REFRESH:
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["error_code"], "TOKEN_INVALID")
            else:
                self.assertEqual(response.status_code, 200)
            self.assertNotIn("refresh", response.json().get("data") or {})

        self.assertTrue(LoginSession.all_objects.get(device_key=account).is_active)
        self.assertEqual(self.call(REFRESH, account, stolen).status_code, 200)

    def test_an_unlisted_origin_gets_no_body_credential_and_is_refused(self):
        response = self.sign_in(self.tunde, origin=EVIL)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('"refresh"', response.content.decode())
        self.assertFalse(LoginSession.all_objects.filter(user=self.tunde).exists())

        account, credential = self.native_sign_in(self.bola)
        self.assertEqual(self.call(REFRESH, account, credential, origin=EVIL).status_code, 403)
        self.assertEqual(self.call(SIGN_OUT, account, credential, origin=EVIL).status_code, 403)
        self.assertTrue(LoginSession.all_objects.get(device_key=account).is_active)

    def test_the_native_origins_come_only_from_the_setting(self):
        with override_settings(NATIVE_APP_ORIGINS=[ANDROID]):
            response = self.sign_in(self.tunde, origin=NATIVE)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("refresh", response.json()["data"])
        self.assertIn(f"{PREFIX}{response.json()['data']['account']}", response.cookies)

    def test_a_native_sign_in_must_be_a_device_session(self):
        for session in (None, "browser"):
            response = self.sign_in(self.tunde, session=session)
            self.assertEqual(response.status_code, 400, response.content)
            self.assertEqual(response.json()["error"]["code"], "DEVICE_SESSION_REQUIRED")
            self.assertNotIn('"refresh"', response.content.decode())
        self.assertFalse(LoginSession.all_objects.filter(user=self.tunde).exists())

    def test_an_account_offered_no_app_is_refused_from_a_native_origin(self):
        response = self.sign_in(self.mother)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "DEVICE_SESSION_NOT_OFFERED")
        self.assertFalse(LoginSession.all_objects.filter(user=self.mother).exists())
        self.assert_no_cookies(response)

    # ── signing in ────────────────────────────────────────────────────────

    def test_a_native_sign_in_returns_the_credential_and_sets_no_cookie(self):
        for origin in (NATIVE, ANDROID):
            response = self.sign_in(self.tunde, origin=origin)
            self.assertEqual(response.status_code, 200, response.content)
            data = response.json()["data"]
            self.assertTrue(data["account"])
            self.assertTrue(data["refresh"])
            self.assertTrue(data["access"])
            self.assert_no_cookies(response)

    # ── refreshing ────────────────────────────────────────────────────────

    def test_a_native_refresh_needs_no_csrf_and_rotates_the_credential(self):
        account, first = self.native_sign_in(self.tunde)

        response = self.call(REFRESH, account, first)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["account"], account)
        self.assertTrue(data["access"])
        second = data["refresh"]
        self.assertNotEqual(second, first)
        self.assert_no_cookies(response)

        # The credential just spent is still answered inside the window, with
        # the one the session is on; the new one works as well.
        again = self.call(REFRESH, account, first)
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again.json()["data"]["refresh"], second)
        self.assert_no_cookies(again)
        self.assertEqual(self.call(REFRESH, account, second).status_code, 200)

    def test_a_native_refresh_whose_answer_was_lost_is_recovered_inside_the_window(self):
        account, first = self.native_sign_in(self.tunde)
        delivered = self.call(REFRESH, account, first).json()["data"]["refresh"]

        # The phone never saw that answer and asks again with what it holds.
        with self.after(60):
            retry = self.call(REFRESH, account, first)
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(retry.json()["data"]["refresh"], delivered)
        self.assert_no_cookies(retry)
        with self.after(61):
            self.assertEqual(self.call(REFRESH, account, delivered).status_code, 200)

    def test_a_native_credential_presented_after_the_window_ends_the_session(self):
        account, first = self.native_sign_in(self.tunde)
        current = self.call(REFRESH, account, first).json()["data"]["refresh"]

        with self.after(device_sessions.refresh_grace().total_seconds() + 1):
            response = self.call(REFRESH, account, first)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["error_code"], "TOKEN_REVOKED")
        self.assert_no_cookies(response)
        session = LoginSession.all_objects.get(device_key=account)
        self.assertFalse(session.is_active)
        self.assertEqual(session.end_reason, "CREDENTIAL_REUSED")
        self.assertEqual(self.call(REFRESH, account, current).status_code, 401)

    def test_a_native_credential_two_rotations_old_ends_the_session(self):
        account, first = self.native_sign_in(self.tunde)
        second = self.call(REFRESH, account, first).json()["data"]["refresh"]
        self.assertEqual(self.call(REFRESH, account, second).status_code, 200)

        response = self.call(REFRESH, account, first)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["error_code"], "TOKEN_REVOKED")
        self.assertFalse(LoginSession.all_objects.get(device_key=account).is_active)

    def test_a_native_refresh_ignores_a_cookie(self):
        account, credential = self.native_sign_in(self.tunde)
        self.client.cookies[f"{PREFIX}{account}"] = credential
        response = self.call(REFRESH, account)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["error_code"], "TOKEN_INVALID")
        self.assert_no_cookies(response)

    def test_two_accounts_on_one_phone_refresh_independently(self):
        tunde, tunde_credential = self.native_sign_in(self.tunde)
        bola, bola_credential = self.native_sign_in(self.bola)

        first = self.call(REFRESH, tunde, tunde_credential)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()["data"]["account"], tunde)
        # Bola's untouched credential still works, and Tunde's cannot be used as hers.
        self.assertEqual(self.call(REFRESH, bola, tunde_credential).status_code, 401)
        self.assertEqual(self.call(REFRESH, bola, bola_credential).status_code, 200)

    def test_expired_and_revoked_credentials_answer_as_the_cookie_path_does(self):
        account, credential = self.native_sign_in(self.tunde)

        missing = self.call(REFRESH, account)
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["error"]["error_code"], "TOKEN_INVALID")

        garbage = self.call(REFRESH, account, "not-a-token")
        self.assertEqual(garbage.json()["error"]["error_code"], "TOKEN_INVALID")

        LoginSession.all_objects.filter(device_key=account).update(is_active=False)
        revoked = self.call(REFRESH, account, credential)
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(revoked.json()["error"]["error_code"], "TOKEN_REVOKED")
        self.assert_no_cookies(revoked)

        short = DeviceSessionPolicy(lifetime=dt.timedelta(seconds=-5), app_path="/student/")
        register_device_session_policy("test_app", lambda user: short)
        other, expired = self.native_sign_in(self.bola)
        self.assertEqual(
            self.call(REFRESH, other, expired).json()["error"]["error_code"], "TOKEN_EXPIRED",
        )

    # ── signing out ───────────────────────────────────────────────────────

    def test_a_native_sign_out_ends_that_account_only(self):
        tunde, tunde_credential = self.native_sign_in(self.tunde)
        bola, bola_credential = self.native_sign_in(self.bola)
        ended = []
        device_session_ended.connect(
            lambda sender, device_key, **kwargs: ended.append(device_key),
            weak=False, dispatch_uid="test.native_ended",
        )
        self.addCleanup(device_session_ended.disconnect, dispatch_uid="test.native_ended")

        response = self.call(SIGN_OUT, tunde, tunde_credential)
        self.assertEqual(response.status_code, 200)
        self.assert_no_cookies(response)
        self.assertFalse(LoginSession.all_objects.get(device_key=tunde).is_active)
        self.assertTrue(LoginSession.all_objects.get(device_key=bola).is_active)
        self.assertEqual(ended, [tunde])

        self.assertEqual(self.call(REFRESH, tunde, tunde_credential).status_code, 401)
        self.assertEqual(self.call(REFRESH, bola, bola_credential).status_code, 200)

    def test_a_native_sign_out_without_a_valid_credential_ends_nothing(self):
        account, _credential = self.native_sign_in(self.tunde)
        for refresh in (None, "forged"):
            response = self.call(SIGN_OUT, account, refresh)
            self.assertEqual(response.status_code, 200)
            self.assert_no_cookies(response)
        self.assertTrue(LoginSession.all_objects.get(device_key=account).is_active)

    # ── CORS ──────────────────────────────────────────────────────────────

    def test_the_production_settings_allow_the_native_origins(self):
        self.assertIn(NATIVE, base_settings.CORS_ALLOWED_ORIGINS)
        self.assertIn(ANDROID, base_settings.CORS_ALLOWED_ORIGINS)
        self.assertIn(NATIVE, base_settings.AUTH_BROWSER_ALLOWED_ORIGINS)
        self.assertIn(ANDROID, base_settings.AUTH_BROWSER_ALLOWED_ORIGINS)

    def test_a_preflight_from_the_native_origin_is_allowed(self):
        with override_settings(
            CORS_ALLOW_ALL_ORIGINS=False,
            CORS_ALLOWED_ORIGINS=base_settings.CORS_ALLOWED_ORIGINS,
            CORS_ALLOWED_ORIGIN_REGEXES=base_settings.CORS_ALLOWED_ORIGIN_REGEXES,
        ):
            for origin in (NATIVE, ANDROID):
                response = self.client.options(
                    LOGIN, HTTP_ORIGIN=origin,
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                    HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Access-Control-Allow-Origin"], origin)
            blocked = self.client.options(
                LOGIN, HTTP_ORIGIN=EVIL, HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            )
            self.assertNotIn("Access-Control-Allow-Origin", blocked)
