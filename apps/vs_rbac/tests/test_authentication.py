"""
Tests for TenantJWTAuthentication — the school-context-aware JWT auth class.

These guard the B1 fix: Django middleware runs before DRF authentication, so
the school context MUST be established by the authentication class itself.
"""
from rest_framework.test import APIRequestFactory
from rest_framework.exceptions import AuthenticationFailed
from django.test import TestCase

from core.thread_locals import get_current_school, clear_current_school
from vs_rbac.authentication import TenantJWTAuthentication
from vs_user.tokens import CodeXRefreshToken
from vs_user.models import User

from .helpers import make_school, make_branch, make_school_admin


class TenantJWTAuthenticationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.auth = TenantJWTAuthentication()
        clear_current_school()

    def tearDown(self):
        clear_current_school()

    def _authed_request(self, user):
        token = str(CodeXRefreshToken.for_user(user).access_token)
        return self.factory.get("/v1/any/", HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_school_user_gets_school_context(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch, school=school)

        request = self._authed_request(user)
        result = self.auth.authenticate(request)

        self.assertIsNotNone(result)
        authed_user, _ = result
        self.assertEqual(authed_user.pk, user.pk)
        self.assertEqual(request.school, school)
        self.assertEqual(get_current_school(), school)

    def test_vision_staff_has_no_school_context(self):
        user = User.objects.create_user(
            email="cx@test.com",
            password="testpass123",
            user_type="CX_STAFF",
            status="ACTIVE",
            first_name="CX",
            last_name="Staff",
        )

        request = self._authed_request(user)
        result = self.auth.authenticate(request)

        self.assertIsNotNone(result)
        self.assertIsNone(request.school)
        self.assertIsNone(get_current_school())

    def test_unauthenticated_request_untouched(self):
        request = self.factory.get("/v1/any/")
        self.assertIsNone(self.auth.authenticate(request))
        self.assertIsNone(get_current_school())
