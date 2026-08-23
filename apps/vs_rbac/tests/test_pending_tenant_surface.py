"""FR-012: a tenant that has not gone live reaches onboarding and nothing else.

Authentication admits a tenant whose status is ACTIVE or PENDING, so the first
School Admin of a school that is still being set up can sign in. What that
caller may then reach is decided per view by
``vs_rbac.permissions.TenantSurfaceAllowed``, and membership of the surface is
an explicit attribute that defaults to closed.

The throwaway views at the top of this module are the point of the exercise:
acceptance criterion 5 asks for a view that declares nothing and is proven
closed, rather than argued closed from the settings file.
"""
from django.test import TestCase, override_settings
from django.urls import path
from rest_framework.test import APIClient
from rest_framework.views import APIView

from core.response import success_response
from vs_admin_console.models import ImpersonationSession
from vs_rbac.permissions import IsAuthenticatedAndActive
from vs_tenants.models import Tenant
from vs_user.models import User
from vs_user.tokens import CodeXRefreshToken

from .helpers import make_branch, make_school, make_school_admin


# ── Throwaway views used only by this module ───────────────────────────────

def _platform_tenant():
    """The one PLATFORM tenant, seeded by vs_tenants migration 0002.

    Being platform staff IS being on this tenant - there is no persona column
    standing in for it any more - so a fixture that wants a CX account names
    the tenant, exactly as production code does.
    """
    from vs_tenants.models import Tenant

    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


class BareSurfaceView(APIView):
    """Declares nothing at all: no permission_classes, no surface membership.

    This is the default-closed proof. It falls back to
    DEFAULT_PERMISSION_CLASSES, which is where the gate has to be installed for
    "declares nothing" to mean "closed".
    """

    def get(self, request):
        return success_response("Bare view reached.", {"reached": True})


class GuardedSurfaceView(APIView):
    """The shape almost every authenticated view in the repo uses."""

    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request):
        return success_response("Guarded view reached.", {"reached": True})


class DeclaredSurfaceView(APIView):
    """Stands in for /v1/onboarding/*, which a later wave adds."""

    permission_classes = [IsAuthenticatedAndActive]
    pending_tenant_surface = True

    def get(self, request):
        return success_response("Onboarding surface reached.", {"reached": True})


urlpatterns = [
    path("v1/bare/", BareSurfaceView.as_view()),
    path("v1/guarded/", GuardedSurfaceView.as_view()),
    path("v1/surface/", DeclaredSurfaceView.as_view()),
]


# ── Shared fixtures ────────────────────────────────────────────────────────

class PendingTenantTestBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pending_school = make_school(
            slug="pending-school", name="Pending School", status="PENDING",
        )
        self.pending_branch = make_branch(self.pending_school)
        self.pending_admin = make_school_admin(
            self.pending_branch, email="pending-admin@test.com",
        )
        self.pending_tenant = self.pending_school.tenant
        self.pending_tenant.refresh_from_db()
        self.assertEqual(self.pending_tenant.status, Tenant.Status.PENDING)

    def _as(self, user):
        token = str(CodeXRefreshToken.for_user(user).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def assertTenantNotLive(self, response):
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")


@override_settings(ROOT_URLCONF="vs_rbac.tests.test_pending_tenant_surface")
class PendingTenantSurfaceDeclarationTests(PendingTenantTestBase):
    """The allowlist is the declaration on the view, and nothing else."""

    def test_view_without_a_surface_declaration_is_closed_to_pending(self):
        client = self._as(self.pending_admin)
        response = client.get(f"/v1/bare/?tenant={self.pending_tenant.slug}")
        self.assertTenantNotLive(response)

    def test_iaaa_view_without_a_declaration_is_closed_to_pending(self):
        client = self._as(self.pending_admin)
        response = client.get(f"/v1/guarded/?tenant={self.pending_tenant.slug}")
        self.assertTenantNotLive(response)

    def test_declared_view_is_open_to_pending(self):
        client = self._as(self.pending_admin)
        response = client.get(f"/v1/surface/?tenant={self.pending_tenant.slug}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["reached"])

    def test_active_tenant_reaches_undeclared_views_unchanged(self):
        school = make_school(slug="live-school", name="Live School")
        branch = make_branch(school)
        admin = make_school_admin(branch, email="live-admin@test.com")
        client = self._as(admin)

        for url in ("/v1/bare/", "/v1/guarded/", "/v1/surface/"):
            response = client.get(f"{url}?tenant={school.slug}")
            self.assertEqual(response.status_code, 200, f"{url}: {response.data}")

    def test_activation_opens_the_previously_closed_surface(self):
        client = self._as(self.pending_admin)
        closed = client.get(f"/v1/guarded/?tenant={self.pending_tenant.slug}")
        self.assertTenantNotLive(closed)

        # No role, assignment or permission key changes - only the tenant status.
        Tenant.objects.filter(pk=self.pending_tenant.pk).update(
            status=Tenant.Status.ACTIVE,
        )

        opened = client.get(f"/v1/guarded/?tenant={self.pending_tenant.slug}")
        self.assertEqual(opened.status_code, 200, opened.data)

    def test_pending_tenant_cannot_assert_another_tenants_slug(self):
        """404 either way: a pending peer must not be distinguishable.

        Answering 403 for one and 404 for the other would leak which tenants
        exist and which of them are still onboarding, which is precisely the
        enumeration hole the 404 rule closes.
        """
        other_active = make_school(slug="other-live", name="Other Live School")
        other_pending = make_school(
            slug="other-pending", name="Other Pending School", status="PENDING",
        )
        client = self._as(self.pending_admin)

        active_answer = client.get(f"/v1/surface/?tenant={other_active.slug}")
        pending_answer = client.get(f"/v1/surface/?tenant={other_pending.slug}")

        self.assertEqual(active_answer.status_code, 404, active_answer.data)
        self.assertEqual(pending_answer.status_code, 404, pending_answer.data)
        self.assertEqual(active_answer.data, pending_answer.data)
        self.assertEqual(active_answer.content, pending_answer.content)

    def test_platform_actor_impersonating_into_a_pending_tenant_is_scoped_the_same(self):
        cx = User.objects.create_user(tenant=_platform_tenant(), 
            email="cx-proxy@test.com", password="testpass123",
            status="ACTIVE",
            first_name="CX", last_name="Proxy",
        )
        session = ImpersonationSession.objects.create(
            staff_user=cx,
            tenant=self.pending_tenant,
            target_user=self.pending_admin,
            justification="Helping the school finish onboarding.",
        )
        client = self._as(cx)
        client.credentials(
            HTTP_AUTHORIZATION=client._credentials["HTTP_AUTHORIZATION"],
            HTTP_X_IMPERSONATION_SESSION=str(session.pk),
        )

        closed = client.get(f"/v1/guarded/?tenant={self.pending_tenant.slug}")
        self.assertTenantNotLive(closed)

        open_surface = client.get(f"/v1/surface/?tenant={self.pending_tenant.slug}")
        self.assertEqual(open_surface.status_code, 200, open_surface.data)


class PendingTenantRealEndpointTests(PendingTenantTestBase):
    """The same rule against endpoints that actually ship."""

    def test_pending_tenant_is_refused_outside_the_onboarding_surface(self):
        client = self._as(self.pending_admin)
        response = client.get(
            f"/v1/finance/entities/?tenant={self.pending_tenant.slug}",
        )
        self.assertTenantNotLive(response)

    def test_pending_tenant_reaches_self_scoped_endpoints(self):
        client = self._as(self.pending_admin)
        response = client.get("/v1/user/auth/me/")
        self.assertEqual(response.status_code, 200, response.data)

    def test_pending_tenant_may_file_a_support_ticket_only(self):
        client = self._as(self.pending_admin)
        created = client.post(
            f"/v1/support/tickets/?tenant={self.pending_tenant.slug}",
            {
                "title": "Cannot finish onboarding",
                "description": "The branch step will not complete.",
                "category": "SUPPORT",
                "priority": "HIGH",
            },
            format="json",
        )
        self.assertIn(created.status_code, (200, 201), created.data)

        # The rest of the ticket desk stays closed until go-live.
        listed = client.get(
            f"/v1/support/tickets/?tenant={self.pending_tenant.slug}",
        )
        self.assertTenantNotLive(listed)

    def test_active_tenant_access_is_unchanged_on_a_real_endpoint(self):
        school = make_school(slug="live-real", name="Live Real School")
        branch = make_branch(school)
        admin = make_school_admin(branch, email="live-real@test.com")
        client = self._as(admin)

        response = client.get(f"/v1/finance/entities/?tenant={school.slug}")
        # Whatever RBAC decides, it must not be the pending-tenant refusal.
        if response.status_code == 403:
            self.assertNotEqual(
                response.data.get("error", {}).get("code"), "TENANT_NOT_LIVE",
            )
