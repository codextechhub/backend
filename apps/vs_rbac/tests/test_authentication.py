"""
Tests for TenantJWTAuthentication - the school-context-aware JWT auth class.

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


def _platform_tenant():
    """The one PLATFORM tenant, seeded by vs_tenants migration 0002.

    Being platform staff IS being on this tenant - there is no persona column
    standing in for it any more - so a fixture that wants a CX account names
    the tenant, exactly as production code does.
    """
    from vs_tenants.models import Tenant

    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


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
        user = make_school_admin(branch)

        request = self._authed_request(user)
        result = self.auth.authenticate(request)

        self.assertIsNotNone(result)
        authed_user, _ = result
        self.assertEqual(authed_user.pk, user.pk)
        self.assertEqual(request.tenant, school.tenant)
        self.assertEqual(get_current_tenant(), school.tenant)

    def test_platform_staff_gets_codex_tenant_context(self):
        user = User.objects.create_user(tenant=_platform_tenant(), 
            email="cx@test.com",
            password="testpass123",
            status="ACTIVE",
            first_name="CX",
            last_name="Staff",
        )

        request = self._authed_request(user)
        result = self.auth.authenticate(request)

        self.assertIsNotNone(result)
        self.assertEqual(request.tenant.slug, "codex")
        self.assertEqual(get_current_tenant(), request.tenant)

    def test_unauthenticated_request_untouched(self):
        request = self.factory.get("/v1/any/")
        self.assertIsNone(self.auth.authenticate(request))
        self.assertIsNone(get_current_tenant())

    # ── ITEM 2: reject pre-tenant JWTs ──────────────────────────────────────

    def test_token_without_tenant_slug_rejected(self):
        user = User.objects.create_user(
            tenant=_platform_tenant(),
            email="pretenant@test.com", password="testpass123",
            status="ACTIVE",
            first_name="Pre", last_name="Tenant",
        )
        # A vanilla AccessToken carries no tenant_slug/tenant_id claims - a
        # pre-refactor token shape.
        legacy = str(AccessToken.for_user(user))
        request = self._authed_request(user, token=legacy)
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(request)

    def test_token_with_mismatched_tenant_id_rejected(self):
        user = User.objects.create_user(tenant=_platform_tenant(), 
            email="moved@test.com", password="testpass123",
            status="ACTIVE",
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
        user = make_school_admin(branch, tenant=school.tenant)
        request = self._authed_request(user, with_tenant=False, view=_FakeView())
        with self.assertRaises(ValidationError):
            self.auth.authenticate(request)

    def test_exempt_view_binds_home_tenant_without_param(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch, tenant=school.tenant)
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
        cx = User.objects.create_user(tenant=_platform_tenant(), 
            email="cx-cross@test.com", password="testpass123",
            status="ACTIVE",
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
        cx = User.objects.create_user(tenant=_platform_tenant(), 
            email="cx-noflag@test.com", password="testpass123",
            status="ACTIVE",
            first_name="CX", last_name="NoFlag",
        )
        school = make_school(slug="noflag-school", name="No Flag School")
        request = self._authed_request(cx, tenant_slug=school.slug, view=_FakeView())
        with self.assertRaises(NotFound):
            self.auth.authenticate(request)

    # ── FR-012: which tenant statuses may authenticate ─────────────────────

    def _user_in_tenant_with_status(self, status, *, slug, email):
        """Build a school + admin, then force the tenant to *status*.

        The tenant is written directly rather than through School.save(), which
        maps only ACTIVE and INACTIVE and would silently turn a SUSPENDED
        school back into a PENDING tenant.
        """
        school = make_school(slug=slug, name=slug.replace("-", " ").title())
        branch = make_branch(school)
        user = make_school_admin(branch, email=email)
        Tenant.objects.filter(pk=school.tenant_id).update(status=status)
        school.tenant.refresh_from_db()
        return user

    def test_authentication_admits_active_and_pending_only(self):
        admitted = {}
        for status in (Tenant.Status.ACTIVE, Tenant.Status.PENDING):
            user = self._user_in_tenant_with_status(
                status,
                slug=f"admits-{status.lower()}",
                email=f"admits-{status.lower()}@test.com",
            )
            request = self._authed_request(user, view=_FakeView())
            result = self.auth.authenticate(request)
            self.assertIsNotNone(result, f"{status} should authenticate")
            self.assertEqual(request.tenant.status, status)
            admitted[status] = True

        for status in (Tenant.Status.SUSPENDED, Tenant.Status.INACTIVE):
            user = self._user_in_tenant_with_status(
                status,
                slug=f"admits-{status.lower()}",
                email=f"admits-{status.lower()}@test.com",
            )
            request = self._authed_request(user, view=_FakeView())
            with self.assertRaises(NotFound, msg=f"{status} must not authenticate"):
                self.auth.authenticate(request)

        self.assertEqual(
            sorted(admitted), sorted([Tenant.Status.ACTIVE, Tenant.Status.PENDING]),
        )

    def test_suspended_and_inactive_tenants_still_answer_404(self):
        # 404, not 403: the refusal must stay indistinguishable from an unknown
        # slug so tenant identifiers cannot be enumerated.
        for status in (Tenant.Status.SUSPENDED, Tenant.Status.INACTIVE):
            user = self._user_in_tenant_with_status(
                status,
                slug=f"closed-{status.lower()}",
                email=f"closed-{status.lower()}@test.com",
            )
            request = self._authed_request(user, view=_FakeView())
            with self.assertRaises(NotFound) as caught:
                self.auth.authenticate(request)
            self.assertEqual(caught.exception.status_code, 404)

        # An unknown slug answers the same way.
        known = self._user_in_tenant_with_status(
            Tenant.Status.ACTIVE, slug="closed-control", email="closed-control@test.com",
        )
        request = self._authed_request(
            known, tenant_slug="no-such-tenant", view=_FakeView(),
        )
        with self.assertRaises(NotFound):
            self.auth.authenticate(request)

    def test_active_tenant_access_is_unchanged_on_every_surface(self):
        """An ACTIVE tenant behaves exactly as before on all three auth paths."""
        from vs_rbac.permissions import TenantSurfaceAllowed

        school = make_school(slug="unchanged-school", name="Unchanged School")
        branch = make_branch(school)
        user = make_school_admin(branch, email="unchanged@test.com")

        # 1. The ordinary path: ?tenant=<own slug> on a view declaring nothing.
        undeclared = _FakeView()
        request = self._authed_request(user, view=undeclared)
        authed_user, _ = self.auth.authenticate(request)
        self.assertEqual(request.tenant, school.tenant)
        request.user = authed_user
        # The surface gate never fires for a live tenant, so a view that
        # declares no membership is still reachable.
        self.assertTrue(
            TenantSurfaceAllowed().has_permission(request, undeclared),
        )

        # 2. The self-scoped path: no ?tenant= on an exempt view.
        exempt = _FakeView(tenant_param_required=False)
        request = self._authed_request(user, with_tenant=False, view=exempt)
        authed_user, _ = self.auth.authenticate(request)
        self.assertEqual(request.tenant, school.tenant)
        request.user = authed_user
        self.assertTrue(TenantSurfaceAllowed().has_permission(request, exempt))

        # 3. The platform cross-tenant path onto an ACTIVE school.
        cx = User.objects.create_user(tenant=_platform_tenant(), 
            email="cx-unchanged@test.com", password="testpass123",
            status="ACTIVE",
            first_name="CX", last_name="Unchanged",
        )
        cross = _FakeView(platform_cross_tenant_param=True)
        request = self._authed_request(cx, tenant_slug=school.slug, view=cross)
        authed_user, _ = self.auth.authenticate(request)
        self.assertEqual(request.tenant, school.tenant)
        request.user = authed_user
        self.assertTrue(TenantSurfaceAllowed().has_permission(request, cross))
