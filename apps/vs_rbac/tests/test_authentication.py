"""
Tests for TenantJWTAuthentication — the school-context-aware JWT auth class.

These guard the B1 fix: Django middleware runs before DRF authentication, so
the school context MUST be established by the authentication class itself.
"""
from rest_framework.test import APIRequestFactory
from rest_framework.exceptions import AuthenticationFailed, NotFound, ValidationError
from rest_framework_simplejwt.tokens import AccessToken
from django.test import TestCase

from vs_tenants.context import get_current_tenant, clear_current_tenant
from vs_tenants.models import Tenant
from vs_rbac.authentication import TenantJWTAuthentication
from vs_user.tokens import CodeXRefreshToken
from vs_user.models import User

from .helpers import make_school, make_branch, make_school_admin


class _FakeView:
    """Stand-in for the DRF view resolved from request.parser_context."""

    def __init__(self, *, tenant_param_required=True, platform_cross_tenant_param=False):
        self.tenant_param_required = tenant_param_required
        self.platform_cross_tenant_param = platform_cross_tenant_param


class TenantJWTAuthenticationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.auth = TenantJWTAuthentication()
        clear_current_tenant()

    def tearDown(self):
        clear_current_tenant()

    def _authed_request(self, user, *, tenant_slug=None, with_tenant=True,
                        token=None, view=None):
        if token is None:
            token = str(CodeXRefreshToken.for_user(user).access_token)
        slug = tenant_slug if tenant_slug is not None else user.tenant.slug
        path = f"/v1/any/?tenant={slug}" if with_tenant else "/v1/any/"
        request = self.factory.get(path, HTTP_AUTHORIZATION=f"Bearer {token}")
        # The DRF view is exposed via parser_context in production; simulate it.
        request.parser_context = {"view": view}
        return request

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
        self.assertEqual(request.tenant, school.tenant)
        self.assertEqual(get_current_tenant(), school.tenant)

    def test_platform_staff_gets_codex_tenant_context(self):
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
        self.assertEqual(request.tenant.slug, "codex")
        self.assertEqual(get_current_tenant(), request.tenant)

    def test_unauthenticated_request_untouched(self):
        request = self.factory.get("/v1/any/")
        self.assertIsNone(self.auth.authenticate(request))
        self.assertIsNone(get_current_tenant())

    # ── ITEM 2: reject pre-tenant JWTs ──────────────────────────────────────

    def test_token_without_tenant_slug_rejected(self):
        user = User.objects.create_user(
            email="pretenant@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="Pre", last_name="Tenant",
        )
        # A vanilla AccessToken carries no tenant_slug/tenant_id claims — a
        # pre-refactor token shape.
        legacy = str(AccessToken.for_user(user))
        request = self._authed_request(user, token=legacy)
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(request)

    def test_token_with_mismatched_tenant_id_rejected(self):
        user = User.objects.create_user(
            email="moved@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="Moved", last_name="User",
        )
        access = CodeXRefreshToken.for_user(user).access_token
        access["tenant_id"] = "9999999"  # simulate a token from before a tenant move
        request = self._authed_request(user, token=str(access))
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(request)

    # ── ITEM 3: ?tenant= required only on tenant-owned endpoints ─────────────

    def test_missing_tenant_param_required_by_default(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch, school=school)
        request = self._authed_request(user, with_tenant=False, view=_FakeView())
        with self.assertRaises(ValidationError):
            self.auth.authenticate(request)

    def test_exempt_view_binds_home_tenant_without_param(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch, school=school)
        view = _FakeView(tenant_param_required=False)
        request = self._authed_request(user, with_tenant=False, view=view)

        result = self.auth.authenticate(request)
        self.assertIsNotNone(result)
        authed_user, _ = result
        self.assertEqual(authed_user.pk, user.pk)
        self.assertEqual(request.tenant, school.tenant)
        self.assertEqual(get_current_tenant(), school.tenant)

    # ── ITEM 4: platform cross-tenant assertion via view flag ───────────────

    def test_platform_actor_cross_tenant_allowed_with_flag(self):
        cx = User.objects.create_user(
            email="cx-cross@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="CX", last_name="Cross",
        )
        school = make_school(slug="cross-school", name="Cross School")
        view = _FakeView(platform_cross_tenant_param=True)
        request = self._authed_request(cx, tenant_slug=school.slug, view=view)

        result = self.auth.authenticate(request)
        self.assertIsNotNone(result)
        self.assertEqual(request.tenant, school.tenant)
        # rbac_tenant stays the actor's own (platform) tenant.
        self.assertEqual(request.rbac_tenant, cx.tenant)

    def test_platform_actor_cross_tenant_denied_without_flag(self):
        cx = User.objects.create_user(
            email="cx-noflag@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="CX", last_name="NoFlag",
        )
        school = make_school(slug="noflag-school", name="No Flag School")
        request = self._authed_request(cx, tenant_slug=school.slug, view=_FakeView())
        with self.assertRaises(NotFound):
            self.auth.authenticate(request)
