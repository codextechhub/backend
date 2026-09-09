"""A school reading its own plan, without holding a platform key to do it.

Depth decides what a school reaches, and nothing on the school's own side
could ask what that depth was. ``/config/effective-capabilities/`` answers the
question, but it is gated on ``config.capability.view`` - a PLATFORM-scoped
key that no school role holds and the grant guard would refuse to give one. So
every school screen was left guessing.

The consequence is concrete. Bright Star is on Basic. Its sidebar offers Data
Imports because the reader holds ``import.batches.view``, which is a role
question and always true for an administrator; the plan question, which is
``bulk_import`` and false, was never asked. She clicks it and the screen loads
and the first request refuses her. Nothing on the way in said the school had
not bought it.

``/config/my-capabilities/`` closes that. It is deliberately the narrowest
thing that works:

* **No permission key.** What your own school bought is not a secret kept from
  its staff, and every screen needs the answer, including a teacher's. Gating
  it on any key would mean the nav renders differently depending on a role
  question that has nothing to do with the plan. It answers for the caller's
  own tenant and no other, which is what makes that safe.
* **Open before go-live**, because a school configures itself while PENDING
  and a nav that renders nothing during onboarding is a nav that is broken
  exactly when it is first seen.
* **Read-only, and a strict subset**: it reports states, never the entitlement
  rows, the overrides or the grants behind them. Those stay on the platform
  endpoint where they were.
"""
from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import (
    make_branch,
    make_school,
    make_school_admin,
    make_staff_user,
    make_vision_user,
)
from vs_tenants.models import Tenant
from vs_user.tokens import CodeXRefreshToken

from .models import Capability, CapabilityEntitlement


class MyCapabilitiesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="bright-star", name="Bright Star")
        cls.branch = make_branch(cls.school, name="Main Branch")
        cls.tenant = cls.school.tenant

        cls.finance = Capability.objects.create(key="finance", label="Finance")
        cls.finance_core = Capability.objects.create(
            key="finance_core", label="Finance: Core",
            parent=cls.finance, depth=Capability.Depth.CORE,
        )
        cls.finance_plus = Capability.objects.create(
            key="finance_plus", label="Finance: Plus",
            parent=cls.finance, depth=Capability.Depth.PLUS,
        )
        # Bought Finance, and bought it shallow. The shape every Basic school
        # on the platform is in.
        CapabilityEntitlement.objects.create(
            tenant=cls.tenant, capability=cls.finance,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
            depth=Capability.Depth.CORE,
        )

        cls.admin = make_school_admin(
            None, email="admin@bright-star.test", tenant=cls.tenant,
        )
        # Holds no configuration key, and no school role ever will.
        cls.teacher = make_staff_user(cls.branch, email="teacher@bright-star.test")

        cls.other = make_school(slug="green-field", name="Green Field")
        make_branch(cls.other, name="Main Branch")

    def _client(self, user):
        client = APIClient()
        token = str(CodeXRefreshToken.for_user(user).access_token)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def _get(self, user, tenant=None):
        slug = (tenant or self.tenant).slug
        return self._client(user).get(
            reverse("config-my-capabilities"), {"tenant": slug},
        )

    def _states(self, response):
        return {row["key"]: row["enabled"] for row in response.data["data"]}

    def test_a_teacher_can_read_what_their_own_school_bought(self):
        response = self._get(self.teacher)
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_answer_follows_the_plan_and_not_the_role(self):
        # Two people, two roles, one plan. The depth wall is not personal.
        for user in (self.admin, self.teacher):
            with self.subTest(user=user.email):
                states = self._states(self._get(user))
                self.assertTrue(states["finance_core"])
                self.assertFalse(states["finance_plus"])

    def test_a_school_that_bought_deeper_reaches_deeper(self):
        CapabilityEntitlement.objects.filter(
            tenant=self.tenant, capability=self.finance,
        ).update(depth=Capability.Depth.PLUS)
        self.assertTrue(self._states(self._get(self.admin))["finance_plus"])

    def test_a_school_that_was_never_given_its_plan_reaches_everything(self):
        """"Not provisioned" and "did not buy" are different facts.

        Every school created before grants were written reliably holds no
        PACKAGE entitlement at all. Reading that as "bought nothing" answers
        no to every capability, and the navigation that trusts this endpoint
        then hides Finance, Procurement and the Export Centre from schools
        already using them. The plan gate has always made this distinction;
        this endpoint has to make the same one, or the menu disagrees with the
        product.
        """
        CapabilityEntitlement.objects.filter(tenant=self.tenant).delete()
        states = self._states(self._get(self.admin))
        self.assertTrue(states["finance"])
        self.assertTrue(states["finance_core"])
        self.assertTrue(states["finance_plus"])

    def test_a_school_that_was_given_a_shallow_plan_is_still_held_to_it(self):
        # The rule above must not become a way to reach past a real plan: one
        # PACKAGE row is enough to make the school provisioned.
        self.assertFalse(self._states(self._get(self.admin))["finance_plus"])

    def test_a_pending_school_can_read_it(self):
        # Onboarding is where the nav is first drawn.
        Tenant.objects.filter(pk=self.tenant.pk).update(
            status=Tenant.Status.PENDING,
        )
        response = self._get(self.admin)
        self.assertEqual(response.status_code, 200, response.data)

    def test_it_answers_for_the_caller_and_nobody_else(self):
        # The tenant comes from the assertion the auth layer already validated,
        # so naming a rival's slug is refused before the view is reached.
        response = self._get(self.admin, tenant=self.other.tenant)
        self.assertEqual(response.status_code, 404, response.data)

    def test_it_reports_states_and_not_the_rows_behind_them(self):
        row = self._get(self.admin).data["data"][0]
        self.assertEqual(set(row), {"key", "enabled"})

    def test_the_platform_endpoint_is_still_closed_to_a_school(self):
        # This opens one narrow door, not the configuration surface.
        response = self._client(self.admin).get(
            reverse("config-effective-capabilities"), {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_codex_reads_its_own_platform_scope_here(self):
        # Same view, no special case: a platform operator asking what THEY
        # reach gets the platform layer, which is what the fallback resolves.
        operator = make_vision_user(email="ops@codexng.test", super_admin=True)
        response = self._client(operator).get(reverse("config-my-capabilities"))
        self.assertEqual(response.status_code, 200, response.data)


class MyCapabilitiesSubscriptionTests(TestCase):
    """An expired subscription closes the doors it paid for."""

    def setUp(self):
        self.school = make_school(slug="lapsed", name="Lapsed Academy")
        make_branch(self.school, name="Main Branch")
        self.admin = make_school_admin(
            None, email="admin@lapsed.test", tenant=self.school.tenant,
        )
        self.finance = Capability.objects.create(key="finance", label="Finance")
        Capability.objects.create(
            key="finance_core", label="Finance: Core",
            parent=self.finance, depth=Capability.Depth.CORE,
        )

    def _states(self):
        client = APIClient()
        token = str(CodeXRefreshToken.for_user(self.admin).access_token)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = client.get(
            reverse("config-my-capabilities"),
            {"tenant": self.school.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        return {row["key"]: row["enabled"] for row in response.data["data"]}

    def test_a_lapsed_grant_shuts_the_module_and_its_bands(self):
        CapabilityEntitlement.objects.create(
            tenant=self.school.tenant, capability=self.finance,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
            depth=Capability.Depth.CORE,
            ends_at=date.today() - timedelta(days=1),
        )
        states = self._states()
        self.assertFalse(states["finance"])
        self.assertFalse(states["finance_core"])
