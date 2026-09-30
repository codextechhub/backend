"""Sign-in limits and records key on an address the caller cannot choose.

The login throttle, the lockout counter, the attempt log and the session list
all take the caller's address from ``core.client_ip``. These drive the real
endpoint, because the defect they guard against lives in what the endpoint
reads from the request, not in any one service.
"""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from vs_user.models import AccountLockout, AuthAttempt, LoginSession

from .tests import make_school, make_school_admin

URL = "/v1/user/auth/login/"
PASSWORD = "Str0ng!pass123"
CLOUDFLARE = ("HTTP_CF_CONNECTING_IP", "HTTP_TRUE_CLIENT_IP")


class LoginClientAddressTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(name="Bright Star School", slug="bright-star")
        cls.ada = make_school_admin(
            cls.school, email="ada.okoye@example.test", password=PASSWORD,
        )

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()

    def _login(self, password, **meta):
        return self.client.post(
            URL,
            {"email": self.ada.email, "password": password, "tenant": "bright-star"},
            format="json",
            **meta,
        )

    def test_a_new_forwarded_for_on_every_request_buys_no_new_allowance(self):
        from rest_framework.throttling import ScopedRateThrottle

        rate = 2
        with mock.patch.object(
            ScopedRateThrottle, "THROTTLE_RATES", {"login": f"{rate}/minute"},
        ):
            statuses = [
                self._login(
                    "wrong-password", REMOTE_ADDR="192.0.2.10",
                    HTTP_X_FORWARDED_FOR=f"198.51.100.{n}",
                ).status_code
                for n in range(rate + 1)
            ]

        self.assertEqual(statuses, [401] * rate + [429])

    def test_a_malformed_forwarded_for_still_counts_toward_the_lockout(self):
        """The address columns are typed, so a header that is not an address
        would fail the write. The failure has to be counted regardless."""
        response = self._login(
            "wrong-password", REMOTE_ADDR="192.0.2.10",
            HTTP_X_FORWARDED_FOR="not-an-address",
        )

        self.assertEqual(response.status_code, 401, response.content)
        lockout = AccountLockout.objects.get(user=self.ada)
        self.assertEqual(lockout.failure_count, 1)
        self.assertEqual(lockout.last_failure_ip, "192.0.2.10")
        self.assertEqual(
            AuthAttempt.all_objects.latest("id").ip_address, "192.0.2.10",
        )

    @override_settings(CLIENT_IP_HEADERS=CLOUDFLARE)
    def test_behind_cloudflare_the_session_records_cf_connecting_ip(self):
        response = self._login(
            PASSWORD,
            REMOTE_ADDR="10.201.3.14",
            HTTP_CF_CONNECTING_IP="203.0.113.7",
            HTTP_X_FORWARDED_FOR="198.51.100.66, 203.0.113.7, 172.70.1.1",
        )

        self.assertEqual(response.status_code, 200, response.content)
        session = LoginSession.all_objects.filter(user=self.ada).latest("id")
        self.assertEqual(session.ip_address, "203.0.113.7")
        self.assertEqual(
            AuthAttempt.all_objects.filter(user=self.ada).latest("id").ip_address,
            "203.0.113.7",
        )
