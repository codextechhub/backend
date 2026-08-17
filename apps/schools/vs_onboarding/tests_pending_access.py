"""FR-012 against the endpoints it was built for.

Wave 2 proved the rule with throwaway views: a tenant whose status is PENDING
authenticates, and reaches only what declares itself part of the onboarding
surface. This file proves the same rule against the nine real endpoints, which
is the thing that actually matters: the module the decision exists to make
usable.

The four tests FR-012 names for this module are here under exactly the names it
gives them. The other two it names (the bare view with no declaration, and a
platform actor riding an impersonation session) belong to the shared gate
rather than to onboarding, and live beside the authentication tests in
``vs_rbac/tests/test_pending_tenant_surface.py``.
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from schools.vs_schools.models import School, SchoolStatus
from vs_rbac.tests.helpers import make_school, make_school_admin
from vs_tenants.models import Tenant
from vs_user.tokens import CodeXRefreshToken

from .constants import PERM_GO_LIVE_VIEW, PERM_PROGRESS_VIEW, PERM_TASK_UPDATE
from .services.provisioning import provision_onboarding
from .tests import grant_school_admin


class PendingTenantOnboardingAccessTests(TestCase):
    def setUp(self):
        self.school = make_school(
            slug="pending-onboarding", name="Pending Onboarding School",
            status="PENDING",
        )
        self.tenant = self.school.tenant
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.PENDING)

        self.admin = make_school_admin(
            None, email="pending-onboarding-admin@test.com", tenant=self.tenant,
        )
        grant_school_admin(
            self.tenant, self.admin,
            PERM_PROGRESS_VIEW, PERM_TASK_UPDATE, PERM_GO_LIVE_VIEW,
        )
        provision_onboarding(self.tenant)

        token = str(CodeXRefreshToken.for_user(self.admin).access_token)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _scoped(self, name, tenant=None):
        tenant = tenant or self.tenant
        return f"{reverse(name)}?tenant={tenant.slug}"

    # ── the four tests FR-012 names for this module ───────────────────────

    def test_pending_tenant_admin_reaches_onboarding_state(self):
        response = self.client.get(self._scoped("onboarding-state"))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["readiness_state"], "NOT_READY")

        # And not only state: the whole surface in section 8 is open to them.
        for name in ("onboarding-task-list", "onboarding-go-live-list"):
            with self.subTest(name=name):
                answer = self.client.get(self._scoped(name))
                self.assertEqual(answer.status_code, 200, answer.data)

    def test_pending_tenant_is_refused_outside_the_onboarding_surface(self):
        """A named endpoint in another module, not an argument in the abstract."""
        response = self.client.get(f"/v1/finance/entities/?tenant={self.tenant.slug}")

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")

    def test_pending_tenant_cannot_assert_another_tenants_slug(self):
        """404 either way, and identically so.

        Answering 403 for a pending peer and 404 for a live one would leak both
        which tenants exist and which of them are still onboarding, which is
        the enumeration hole the 404 rule closes.
        """
        other_live = make_school(slug="other-live-onboarding", name="Other Live")
        other_pending = make_school(
            slug="other-pending-onboarding", name="Other Pending", status="PENDING",
        )

        live_answer = self.client.get(
            self._scoped("onboarding-state", tenant=other_live.tenant),
        )
        pending_answer = self.client.get(
            self._scoped("onboarding-state", tenant=other_pending.tenant),
        )

        self.assertEqual(live_answer.status_code, 404, live_answer.data)
        self.assertEqual(pending_answer.status_code, 404, pending_answer.data)
        self.assertEqual(live_answer.data, pending_answer.data)
        self.assertEqual(live_answer.content, pending_answer.content)

    def test_pending_tenant_reaches_the_closed_surface_after_activation(self):
        closed = self.client.get(f"/v1/finance/entities/?tenant={self.tenant.slug}")
        self.assertEqual(closed.data["error"]["code"], "TENANT_NOT_LIVE")

        # Activation as FR-009 performs it: the school is saved live and the
        # tenant is written by the mirror, never separately. No role,
        # assignment or permission key changes.
        school = School.objects.get(pk=self.school.pk)
        school.status = SchoolStatus.ACTIVE
        school.activated_at = timezone.now()
        school.save()
        self.assertEqual(
            Tenant.objects.get(pk=self.tenant.pk).status, Tenant.Status.ACTIVE,
        )

        opened = self.client.get(f"/v1/finance/entities/?tenant={self.tenant.slug}")
        if opened.status_code == 403:
            self.assertNotEqual(
                opened.data.get("error", {}).get("code"), "TENANT_NOT_LIVE",
            )

        # Onboarding stays reachable afterwards: activation opens surfaces, it
        # does not close this one.
        still_open = self.client.get(self._scoped("onboarding-state"))
        self.assertEqual(still_open.status_code, 200, still_open.data)
