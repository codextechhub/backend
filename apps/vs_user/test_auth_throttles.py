"""The sign-in routes that had no rate limit now have one.

Renewing a sign-in (browser and device), handing a sign-in to the device app,
changing a password, and opening or using an emailed reset or invitation link
each answer 429 once the caller is over its budget. The limit is counted
before the route reads its body, so a flood of refused requests is stopped as
surely as a flood of good ones.

Each test sets its own small rate, so it holds whatever a settings file
zeroes, and clears the cache, where the counts are kept.
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from core.test_utils import TenantAPIClient

from .tests import make_cx_user

AUTH = "/v1/user/auth"

#: (scope, method, path) for every route a caller reaches without signing in.
SIGNED_OUT = [
    ("token_refresh", "post", f"{AUTH}/token/refresh/"),
    ("device_refresh", "post", f"{AUTH}/device/refresh/"),
    ("device_hand_over", "post", f"{AUTH}/device/hand-over/"),
    ("password_reset_link", "get", f"{AUTH}/reset-password/not-a-token/preview/"),
    ("password_reset_link", "post", f"{AUTH}/password/reset/not-a-token/confirm/"),
    ("activation", "get", f"{AUTH}/activate/not-a-token/preview/"),
]


def statuses(client, method, path, times, data=None):
    send = getattr(client, method)
    return [send(path, data or {}, format="json").status_code for _ in range(times)]


class SignedOutRouteThrottleTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_each_route_answers_429_past_its_budget(self):
        for scope, method, path in SIGNED_OUT:
            with self.subTest(path), mock.patch.object(
                ScopedRateThrottle, "THROTTLE_RATES", {scope: "2/minute"},
            ):
                cache.clear()
                codes = statuses(APIClient(), method, path, 3)
                self.assertNotIn(429, codes[:2], codes)
                self.assertEqual(codes[2], 429, codes)


class PasswordChangeThrottleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ada = make_cx_user(email="ada@codex.test")
        cls.bayo = make_cx_user(email="bayo@codex.test")

    def setUp(self):
        cache.clear()

    def test_a_signed_in_person_cannot_keep_guessing_the_current_password(self):
        guess = {"current_password": "wrong", "password": "N3w!password", "confirm_password": "N3w!password"}
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"password_change": "2/minute"}):
            ada = statuses(TenantAPIClient(user=self.ada), "post", f"{AUTH}/password/change/", 3, guess)
            # Counted per person, not per address: Bayo, on the same address, is untouched.
            bayo = statuses(TenantAPIClient(user=self.bayo), "post", f"{AUTH}/password/change/", 1, guess)
        self.assertNotIn(429, ada[:2], ada)
        self.assertEqual(ada[2], 429, ada)
        self.assertNotEqual(bayo[0], 429, bayo)
