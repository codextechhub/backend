"""Device sessions: weeks-long sign-ins, one cookie per account on a device.

Ada and Bayo share one phone, and the product offers both of them its app
(a policy registered here stands in for the product). Femi is offered none and
keeps the ordinary browser session. The tests speak from a browser origin with
CSRF enforced, as the app does.
"""
from __future__ import annotations

import datetime as dt

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from .models import LoginSession
from .services import device_sessions
from .services.audit import blacklist_all_user_tokens
from .services.device_sessions import DeviceSessionPolicy, register_device_session_policy
from .test_browser_session import BROWSER_ORIGIN_REGEXES, BROWSER_ORIGINS, HARDENED_COOKIE
from .tests import make_cx_user

PASSWORD = "Str0ng!pass123"
ORIGIN = "http://localhost:5174"
PREFIX = "device_session_"
APP = DeviceSessionPolicy(lifetime=dt.timedelta(days=30), app_path="/app/")


@override_settings(
    AUTH_BROWSER_ALLOWED_ORIGINS=BROWSER_ORIGINS,
    AUTH_BROWSER_ALLOWED_ORIGIN_REGEXES=BROWSER_ORIGIN_REGEXES,
    CSRF_TRUSTED_ORIGINS=BROWSER_ORIGINS + ["http://*.localhost:5174"],
    AUTH_DEVICE_COOKIE_PREFIX=PREFIX,
    AUTH_DEVICE_COOKIE_PATH="/v1/user/auth/",
    **HARDENED_COOKIE,
)
class DeviceSessionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ada = make_cx_user(email="ada@codex.test", password=PASSWORD)
        cls.bayo = make_cx_user(email="bayo@codex.test", password=PASSWORD)
        cls.femi = make_cx_user(email="femi@codex.test", password=PASSWORD)

    def setUp(self):
        cache.clear()
        self.offered = {self.ada.pk, self.bayo.pk}
        register_device_session_policy(
            "test_app", lambda user: APP if user.pk in self.offered else None,
        )
        self.addCleanup(device_sessions._providers.pop, "test_app", None)
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.get("/v1/user/auth/csrf/", HTTP_ORIGIN=ORIGIN)

    # ── helpers ───────────────────────────────────────────────────────────

    def sign_in(self, user, *, session="device"):
        cache.clear()
        return self.client.post(
            "/v1/user/auth/login/",
            {"email": user.email, "password": PASSWORD, "tenant": user.tenant.slug,
             "session": session},
            format="json", HTTP_ORIGIN=ORIGIN,
        )

    def call(self, route, body=None):
        return self.client.post(
            f"/v1/user/auth/device/{route}/", body or {}, format="json",
            HTTP_ORIGIN=ORIGIN, HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
        )

    def cookie(self, account):
        return self.client.cookies[f"{PREFIX}{account}"].value

    # ── signing in ────────────────────────────────────────────────────────

    def test_a_device_sign_in_sets_its_own_hardened_cookie_for_thirty_days(self):
        response = self.sign_in(self.ada)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertNotIn("refresh", data)
        self.assertNotIn("refresh_token", response.cookies)
        cookie = response.cookies[f"{PREFIX}{data['account']}"]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(cookie["path"], "/v1/user/auth/")
        self.assertGreater(int(cookie["max-age"]), 29 * 86400)

        token = RefreshToken(cookie.value)
        outstanding = OutstandingToken.objects.get(jti=token["jti"])
        self.assertGreater(outstanding.expires_at, timezone.now() + dt.timedelta(days=29))

    def test_an_account_offered_no_app_is_refused_before_anything_is_issued(self):
        response = self.sign_in(self.femi)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "DEVICE_SESSION_NOT_OFFERED")
        self.assertFalse(LoginSession.all_objects.filter(user=self.femi).exists())
        self.assertFalse([c for c in response.cookies if c.startswith(PREFIX)])

    def test_a_browser_sign_in_names_the_app_only_for_an_account_offered_one(self):
        self.assertEqual(
            self.sign_in(self.ada, session="browser").json()["data"]["device_app"],
            {"path": "/app/"},
        )
        self.assertIsNone(self.sign_in(self.femi, session="browser").json()["data"]["device_app"])

    # ── refreshing ────────────────────────────────────────────────────────

    def test_refresh_needs_csrf(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        response = self.client.post(
            "/v1/user/auth/device/refresh/", {"account": account}, format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(response.status_code, 403)

    def test_two_accounts_on_one_device_refresh_independently(self):
        ada = self.sign_in(self.ada).json()["data"]["account"]
        bayo = self.sign_in(self.bayo).json()["data"]["account"]
        bayo_before = self.cookie(bayo)

        response = self.call("refresh", {"account": ada})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["account"], ada)
        self.assertEqual(self.cookie(bayo), bayo_before)
        self.assertEqual(self.call("refresh", {"account": bayo}).status_code, 200)

    def test_a_rotated_credential_cannot_be_used_again(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        old = self.cookie(account)
        self.assertEqual(self.call("refresh", {"account": account}).status_code, 200)

        self.client.cookies[f"{PREFIX}{account}"] = old
        response = self.call("refresh", {"account": account})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["error_code"], "TOKEN_REVOKED")
        self.assertEqual(response.cookies[f"{PREFIX}{account}"]["max-age"], 0)

    def test_one_accounts_credential_cannot_refresh_another(self):
        ada = self.sign_in(self.ada).json()["data"]["account"]
        bayo = self.sign_in(self.bayo).json()["data"]["account"]
        self.client.cookies[f"{PREFIX}{bayo}"] = self.cookie(ada)
        self.assertEqual(self.call("refresh", {"account": bayo}).status_code, 401)

    def test_ending_every_session_of_an_account_reaches_its_device_session(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        blacklist_all_user_tokens(self.ada)
        self.assertEqual(self.call("refresh", {"account": account}).status_code, 401)

    def test_an_account_no_longer_offered_the_app_is_signed_out_for_good(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        self.offered.discard(self.ada.pk)
        self.assertEqual(self.call("refresh", {"account": account}).status_code, 401)
        self.assertFalse(
            LoginSession.all_objects.get(device_key=account).is_active,
        )

    # ── signing out ───────────────────────────────────────────────────────

    def test_signing_out_ends_one_account_and_leaves_the_other(self):
        ada = self.sign_in(self.ada).json()["data"]["account"]
        bayo = self.sign_in(self.bayo).json()["data"]["account"]
        ada_cookie = self.cookie(ada)

        response = self.call("sign-out", {"account": ada})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.cookies[f"{PREFIX}{ada}"]["max-age"], 0)
        self.assertFalse(LoginSession.all_objects.get(device_key=ada).is_active)

        self.client.cookies[f"{PREFIX}{ada}"] = ada_cookie
        self.assertEqual(self.call("refresh", {"account": ada}).status_code, 401)
        self.assertEqual(self.call("refresh", {"account": bayo}).status_code, 200)

    def test_signing_out_without_the_credential_ends_nothing(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        del self.client.cookies[f"{PREFIX}{account}"]
        self.assertEqual(self.call("sign-out", {"account": account}).status_code, 200)
        self.assertTrue(LoginSession.all_objects.get(device_key=account).is_active)

    # ── handing a browser sign-in over ────────────────────────────────────

    def test_a_browser_sign_in_is_handed_over_without_signing_in_again(self):
        self.sign_in(self.ada, session="browser")
        browser = self.client.cookies["refresh_token"].value

        response = self.call("hand-over")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["app_path"], "/app/")
        self.assertEqual(response.cookies["refresh_token"]["max-age"], 0)
        self.assertEqual(self.call("refresh", {"account": data["account"]}).status_code, 200)

        self.client.cookies["refresh_token"] = browser
        refreshed = self.client.post(
            "/v1/user/auth/token/refresh/", {}, format="json", HTTP_ORIGIN=ORIGIN,
            HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
        )
        self.assertEqual(refreshed.status_code, 401)

    def test_an_account_offered_no_app_cannot_be_handed_over(self):
        self.sign_in(self.femi, session="browser")
        response = self.call("hand-over")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["error_code"], "DEVICE_SESSION_NOT_OFFERED")
