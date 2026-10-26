"""Device sessions: weeks-long sign-ins, one cookie per account on a device.

Ada and Bayo share one phone, and the product offers both of them its app
(a policy registered here stands in for the product). Femi is offered none and
keeps the ordinary browser session. The tests speak from a browser origin with
CSRF enforced, as the app does.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from vs_audit.models import AuditEvent

from .models import LoginSession
from .services import device_sessions
from .services.audit import blacklist_all_user_tokens
from .services.device_sessions import DeviceSessionPolicy, register_device_session_policy
from .signals import device_session_ended
from .test_browser_session import BROWSER_ORIGIN_REGEXES, BROWSER_ORIGINS, HARDENED_COOKIE
from .tests import make_cx_user

PASSWORD = "Str0ng!pass123"
ORIGIN = "http://localhost:5174"
PREFIX = "device_session_"
APP = DeviceSessionPolicy(lifetime=dt.timedelta(days=30), app_path="/app/")
#: An app the account opens on a device but whose browser sign-in stays put.
SIDE_APP = DeviceSessionPolicy(
    lifetime=dt.timedelta(days=30), app_path="/app/", sends_browser_sign_in_to_app=False,
)


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

    def test_a_policy_that_keeps_the_browser_sign_in_sends_it_nowhere_but_still_offers_a_device(self):
        from .services import home_apps

        self.offered = set()
        register_device_session_policy("side_app", lambda user: SIDE_APP if user == self.femi else None)
        self.addCleanup(device_sessions._providers.pop, "side_app", None)
        home_apps.register_home_app("test_home", lambda user: "/home/" if user == self.femi else None)
        home_apps.register_other_app("test_other", lambda user: "/other/" if user == self.femi else None)
        self.addCleanup(home_apps._providers.pop, "test_home", None)
        self.addCleanup(home_apps._other_providers.pop, "test_other", None)

        browser = self.sign_in(self.femi, session="browser").json()["data"]
        self.assertIsNone(browser["device_app"])
        self.assertEqual(browser["home_app"], {"path": "/home/"})
        self.assertEqual(browser["other_apps"], [{"key": "test_other", "path": "/other/"}])

        device = self.sign_in(self.femi)
        self.assertEqual(device.status_code, 200, device.content)
        self.assertIn("account", device.json()["data"])
        self.assertGreater(int(device.cookies[f"{PREFIX}{device.json()['data']['account']}"]["max-age"]), 29 * 86400)

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

    # ── a refresh whose answer never reached the device ───────────────────

    def lose_the_answer(self, account):
        """Refresh as the server sees it, then put back the credential the device still holds."""
        held = self.cookie(account)
        self.assertEqual(self.call("refresh", {"account": account}).status_code, 200)
        self.client.cookies[f"{PREFIX}{account}"] = held
        return held

    @staticmethod
    def after(seconds):
        """The clock *seconds* from now, for the code that decides whether a window is open."""
        return mock.patch.object(
            device_sessions.timezone, "now",
            return_value=timezone.now() + dt.timedelta(seconds=seconds),
        )

    def test_a_lost_answer_is_recovered_by_presenting_the_old_credential_again(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        session = LoginSession.all_objects.get(device_key=account)
        first = session.refresh_jti
        self.lose_the_answer(account)
        rotated = LoginSession.all_objects.get(device_key=account).refresh_jti
        self.assertNotEqual(rotated, first)

        with self.after(30):
            retry = self.call("refresh", {"account": account})
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertTrue(retry.json()["data"]["access"])
        delivered = RefreshToken(retry.cookies[f"{PREFIX}{account}"].value)
        self.assertEqual(str(delivered["jti"]), rotated)

        # The device now holds the credential the session is on, and it carries on.
        self.assertEqual(self.call("refresh", {"account": account}).status_code, 200)
        self.assertTrue(LoginSession.all_objects.get(device_key=account).is_active)

    def test_the_grace_retry_gives_back_the_same_credential_however_often_it_is_asked(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        held = self.lose_the_answer(account)

        delivered = set()
        for _ in range(3):
            self.client.cookies[f"{PREFIX}{account}"] = held
            with self.after(10):
                response = self.call("refresh", {"account": account})
            self.assertEqual(response.status_code, 200, response.content)
            delivered.add(response.cookies[f"{PREFIX}{account}"].value)
        self.assertEqual(len(delivered), 1)

    def test_two_windows_refreshing_with_one_credential_end_with_the_same_working_credential(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        held = self.cookie(account)
        first = self.call("refresh", {"account": account})
        first_delivered = first.cookies[f"{PREFIX}{account}"].value
        self.client.cookies[f"{PREFIX}{account}"] = held
        second = self.call("refresh", {"account": account})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first_delivered, second.cookies[f"{PREFIX}{account}"].value)
        # Whichever answer the browser keeps, it works.
        self.client.cookies[f"{PREFIX}{account}"] = first_delivered
        self.assertEqual(self.call("refresh", {"account": account}).status_code, 200)

    def test_the_old_credential_after_the_window_ends_the_session(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        held = self.lose_the_answer(account)
        current = LoginSession.all_objects.get(device_key=account).refresh_jti

        with self.after(device_sessions.refresh_grace().total_seconds() + 1):
            response = self.call("refresh", {"account": account})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["error_code"], "TOKEN_REVOKED")
        self.assertEqual(response.cookies[f"{PREFIX}{account}"]["max-age"], 0)

        session = LoginSession.all_objects.get(device_key=account)
        self.assertFalse(session.is_active)
        self.assertEqual(session.end_reason, "CREDENTIAL_REUSED")
        self.assertTrue(device_sessions._blacklisted(current))
        self.assertTrue(device_sessions._blacklisted(RefreshToken(held, verify=False)["jti"]))

        # The credential the session had moved to is dead with it.
        self.client.cookies[f"{PREFIX}{account}"] = held
        self.assertEqual(self.call("refresh", {"account": account}).status_code, 401)

    def test_a_credential_two_rotations_old_is_refused_even_inside_the_window(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        oldest = self.cookie(account)
        self.assertEqual(self.call("refresh", {"account": account}).status_code, 200)
        self.assertEqual(self.call("refresh", {"account": account}).status_code, 200)

        self.client.cookies[f"{PREFIX}{account}"] = oldest
        response = self.call("refresh", {"account": account})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["error_code"], "TOKEN_REVOKED")
        session = LoginSession.all_objects.get(device_key=account)
        self.assertFalse(session.is_active)
        self.assertEqual(session.end_reason, "CREDENTIAL_REUSED")

    def test_ending_a_session_for_a_reused_credential_is_audited_and_releases_the_device(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        ended_devices = []
        device_session_ended.connect(
            lambda sender, device_key, **kwargs: ended_devices.append(device_key),
            weak=False, dispatch_uid="test.reuse_ended",
        )
        self.addCleanup(device_session_ended.disconnect, dispatch_uid="test.reuse_ended")
        self.lose_the_answer(account)

        with self.after(device_sessions.refresh_grace().total_seconds() + 1):
            self.call("refresh", {"account": account})

        self.assertEqual(ended_devices, [account])
        event = AuditEvent.objects.filter(
            metadata__auth_event="TOKEN_REVOKED", metadata__reason="CREDENTIAL_REUSED",
        ).first()
        self.assertIsNotNone(event)

    def test_a_forced_logout_is_not_undone_by_the_window(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        self.lose_the_answer(account)
        blacklist_all_user_tokens(self.ada, reason="FORCE_LOGOUT")

        with self.after(5):
            response = self.call("refresh", {"account": account})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["error_code"], "TOKEN_REVOKED")

    def test_an_account_no_longer_offered_the_app_is_refused_inside_the_window(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        self.lose_the_answer(account)
        self.offered.discard(self.ada.pk)

        with self.after(5):
            response = self.call("refresh", {"account": account})
        self.assertEqual(response.status_code, 401)
        session = LoginSession.all_objects.get(device_key=account)
        self.assertFalse(session.is_active)
        self.assertEqual(session.end_reason, "NOT_ALLOWED")

    def test_an_account_that_may_no_longer_sign_in_is_refused_inside_the_window(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        self.lose_the_answer(account)
        type(self.ada).objects.filter(pk=self.ada.pk).update(status="SUSPENDED")

        with self.after(5):
            response = self.call("refresh", {"account": account})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(LoginSession.all_objects.get(device_key=account).is_active)

    def test_a_window_of_zero_seconds_accepts_no_old_credential(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        self.lose_the_answer(account)
        with override_settings(DEVICE_SESSION_REFRESH_GRACE_SECONDS=0):
            response = self.call("refresh", {"account": account})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["error_code"], "TOKEN_REVOKED")

    def test_a_current_credential_that_cannot_be_replayed_is_rotated_instead(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        held = self.lose_the_answer(account)
        session = LoginSession.all_objects.get(device_key=account)
        OutstandingToken.objects.filter(jti=session.refresh_jti).update(token="")

        with self.after(5):
            response = self.call("refresh", {"account": account})
        self.assertEqual(response.status_code, 200, response.content)
        reissued = RefreshToken(response.cookies[f"{PREFIX}{account}"].value)
        self.assertNotEqual(str(reissued["jti"]), session.refresh_jti)
        self.assertTrue(device_sessions._blacklisted(session.refresh_jti))
        # The device holds the credential it presented, which stays good for the window.
        self.client.cookies[f"{PREFIX}{account}"] = held
        with self.after(10):
            self.assertEqual(self.call("refresh", {"account": account}).status_code, 200)

    def test_signing_out_with_the_credential_the_device_still_holds_ends_the_session(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        self.lose_the_answer(account)
        self.assertEqual(self.call("sign-out", {"account": account}).status_code, 200)
        self.assertFalse(LoginSession.all_objects.get(device_key=account).is_active)

    def test_one_accounts_credential_cannot_refresh_another(self):
        ada = self.sign_in(self.ada).json()["data"]["account"]
        bayo = self.sign_in(self.bayo).json()["data"]["account"]
        self.client.cookies[f"{PREFIX}{bayo}"] = self.cookie(ada)
        self.assertEqual(self.call("refresh", {"account": bayo}).status_code, 401)

    def test_ending_every_session_of_an_account_reaches_its_device_session(self):
        account = self.sign_in(self.ada).json()["data"]["account"]
        ended_devices = []
        device_session_ended.connect(
            lambda sender, device_key, **kwargs: ended_devices.append(device_key),
            weak=False, dispatch_uid="test.ended_devices",
        )
        self.addCleanup(device_session_ended.disconnect, dispatch_uid="test.ended_devices")

        blacklist_all_user_tokens(self.ada, reason="PASSWORD_CHANGE")

        # Ended at once, with its reason, and the phone's push released.
        session = LoginSession.all_objects.get(device_key=account)
        self.assertFalse(session.is_active)
        self.assertEqual(session.end_reason, "PASSWORD_CHANGE")
        self.assertEqual(ended_devices, [account])

        response = self.call("refresh", {"account": account})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["error_code"], "TOKEN_REVOKED")

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
