"""A CodeX operator configuring a school that has not gone live yet.

The surface gate exists to stop a school using a platform it has not been
activated on. It asked the wrong question of one caller: it read the tenant
being operated on, so a platform operator who asserted a pending school in
order to configure it was refused with the school's own message, telling CodeX
to "complete onboarding and go live".

That is backwards for the console, whose whole job is the school that is not
live yet: entitlements, capabilities and the plan are exactly what an operator
sets before a school goes anywhere.

The exemption is narrow on purpose. It reads the caller's own tenant, and it
does not apply while an impersonation session rides: proxied into a school
account the operator is the school for that request, and a school that has not
gone live reaches only its onboarding surface however senior the person behind
the proxy is. That half is pinned in
``test_pending_tenant_surface.test_platform_actor_impersonating_into_a_pending_tenant_is_scoped_the_same``,
which predates this change and must keep passing.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from vs_tenants.models import Tenant
from vs_user.tokens import CodeXRefreshToken

from ..tests.helpers import make_branch, make_school, make_school_admin, make_vision_user


class PlatformReachesAPendingTenantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="not-live-yet")
        cls.branch = make_branch(cls.school)
        # The state every school arrives in and spends its first days in.
        Tenant.objects.filter(pk=cls.school.tenant_id).update(
            status=Tenant.Status.PENDING
        )
        cls.school.tenant.refresh_from_db()

        cls.operator = make_vision_user(
            email="console@codexng.test", super_admin=True,
        )
        cls.school_admin = make_school_admin(
            cls.branch, email="head@not-live-yet.test",
        )

    def _get(self, user, tenant_slug):
        client = APIClient()
        token = CodeXRefreshToken.for_user(user).access_token
        return client.get(
            f"/v1/config/effective-capabilities/?tenant={tenant_slug}",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def test_an_operator_can_configure_a_school_before_it_goes_live(self):
        response = self._get(self.operator, self.school.slug)
        self.assertNotEqual(
            response.status_code, 403,
            "the console cannot set up the schools it exists to set up",
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_refusal_the_operator_used_to_get_named_the_wrong_problem(self):
        response = self._get(self.operator, self.school.slug)
        code = (response.data or {}).get("error", {}).get("code")
        self.assertNotEqual(
            code, "TENANT_NOT_LIVE",
            "CodeX was told to complete the school's onboarding",
        )

    def test_the_school_itself_is_still_refused(self):
        # The rule the gate exists for is untouched.
        response = self._get(self.school_admin, self.school.slug)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")

    def test_a_live_school_is_not_refused_by_this_gate(self):
        # Whether they then hold the key is a different question, answered by
        # a different class; what matters here is that the surface gate stops
        # objecting once the school is live.
        Tenant.objects.filter(pk=self.school.tenant_id).update(
            status=Tenant.Status.ACTIVE
        )
        response = self._get(self.school_admin, self.school.slug)
        code = (response.data or {}).get("error", {}).get("code")
        self.assertNotEqual(code, "TENANT_NOT_LIVE", response.data)
