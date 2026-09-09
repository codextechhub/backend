"""Regression tests for the browser-only refresh-cookie contract."""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from .models import LoginSession
from .tests import make_cx_user


class BrowserSessionContractTests(TestCase):
    password = "Str0ng!pass123"
    origin = "http://localhost:5173"

    def setUp(self):
        cache.clear()
        self.user = make_cx_user(password=self.password)

    def _login(self, client, *, cookie_mode=True):
        headers = {
            "HTTP_ORIGIN": self.origin,
            **({"HTTP_X_AUTH_MODE": "cookie"} if cookie_mode else {}),
        }
        return client.post(
            "/v1/user/auth/login/",
            {
                "email": self.user.email,
                "password": self.password,
                "tenant": self.user.tenant.slug,
            },
            format="json",
            **headers,
        )

    def test_cookie_login_hides_refresh_token_and_sets_hardened_cookie(self):
        client = APIClient(enforce_csrf_checks=True)
        response = self._login(client)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("refresh", response.json()["data"])
        cookie = response.cookies["refresh_token"]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(cookie["path"], "/")
        self.assertIn("csrftoken", response.cookies)

    def test_cookie_refresh_requires_csrf(self):
        client = APIClient(enforce_csrf_checks=True)
        login = self._login(client)
        self.assertEqual(login.status_code, 200, login.content)

        response = client.post(
            "/v1/user/auth/token/refresh/",
            {},
            format="json",
            HTTP_ORIGIN=self.origin,
        )

        self.assertEqual(response.status_code, 403, response.content)

    def test_cookie_refresh_rotates_cookie_without_exposing_it(self):
        client = APIClient(enforce_csrf_checks=True)
        login = self._login(client)
        old_refresh = client.cookies["refresh_token"].value
        csrf_token = client.cookies["csrftoken"].value

        response = client.post(
            "/v1/user/auth/token/refresh/",
            {},
            format="json",
            HTTP_ORIGIN=self.origin,
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("access", response.json()["data"])
        self.assertIn("session_id", response.json()["data"])
        self.assertNotIn("refresh", response.json()["data"])
        self.assertNotEqual(response.cookies["refresh_token"].value, old_refresh)

    def test_cookie_logout_requires_csrf_then_clears_and_revokes_session(self):
        client = APIClient(enforce_csrf_checks=True)
        login = self._login(client)
        session_id = login.json()["data"]["session_id"]

        refused = client.post(
            "/v1/user/auth/logout/",
            {},
            format="json",
            HTTP_ORIGIN=self.origin,
        )
        self.assertEqual(refused.status_code, 403, refused.content)

        response = client.post(
            "/v1/user/auth/logout/",
            {},
            format="json",
            HTTP_ORIGIN=self.origin,
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.cookies["refresh_token"]["max-age"], 0)
        self.assertFalse(LoginSession.objects.get(pk=session_id).is_active)

    def test_cookie_login_rejects_another_allowed_cors_origin(self):
        client = APIClient()
        response = client.post(
            "/v1/user/auth/login/",
            {
                "email": self.user.email,
                "password": self.password,
                "tenant": self.user.tenant.slug,
            },
            format="json",
            HTTP_X_AUTH_MODE="cookie",
            HTTP_ORIGIN="https://bright-star.xvs.codexng.com",
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertNotIn("refresh_token", response.cookies)

    def test_me_returns_active_proxy_metadata_for_memory_only_restore(self):
        from vs_admin_console.models import ImpersonationSession

        client = APIClient()
        login = self._login(client)
        target = make_cx_user(email="proxy-target@codex.test", password=self.password)
        session = ImpersonationSession.objects.create(
            staff_user=self.user,
            tenant=target.tenant,
            target_user=target,
            justification="Restore after browser reload.",
        )
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.json()['data']['access']}",
        )

        response = client.get("/v1/user/auth/me/")

        self.assertEqual(response.status_code, 200, response.content)
        restored = response.json()["data"]["active_impersonation"]
        self.assertEqual(restored["id"], session.pk)
        self.assertEqual(restored["target"]["id"], target.pk)

    def test_legacy_body_contract_remains_available_without_cookie_credentials(self):
        client = APIClient()
        login = self._login(client, cookie_mode=False)
        self.assertEqual(login.status_code, 200, login.content)
        refresh = login.json()["data"]["refresh"]
        self.assertNotIn("refresh_token", login.cookies)

        response = client.post(
            "/v1/user/auth/token/refresh/",
            {"refresh": refresh},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("refresh", response.json()["data"])
