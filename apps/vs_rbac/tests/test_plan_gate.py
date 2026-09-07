"""The plan gate: the role allows it, and the plan does not.

Two refusals look identical from the outside and mean opposite things. A role
refusal is the school's own business and its admin can fix it. A plan refusal
is ours, and only the proprietor can act on it. These tests pin that they stay
distinguishable, and that the gate cannot lock a school out of a product it is
paying for.

The dangerous direction here is not a missed refusal. It is a refusal that
should not have happened: a school that has bought the module, or a school
nobody has provisioned yet, being told it cannot use its own product. Most of
what follows is about that direction.
"""
from django.test import TestCase, override_settings
from django.urls import path
from rest_framework.response import Response
from rest_framework.test import APIClient
from rest_framework.views import APIView

from vs_config.models import (
    Capability,
    CapabilityEntitlement,
    ConfigurationDefinition,
)
from vs_config.services.capabilities import set_entitlement
from vs_config.services.resolution import set_value

from ..exceptions import PlanUpgradeRequired
from ..models import Permission
from ..permissions import HasRBACPermission
from ..plan_gate import capability_for_permission, plan_refusal
from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)


class _GateFixture(TestCase):
    """A module, its Plus band, and permission keys pointing at each."""

    def setUp(self):
        self.school = make_school(slug="gate-school")
        self.tenant = self.school.tenant
        self.actor = make_vision_user(email="gate-actor@example.com")

        self.enforce = ConfigurationDefinition.objects.create(
            key="platform.entitlements.enforce",
            label="Enforce Plan Entitlements",
            description="Test copy of the enforcement switch.",
            value_type=ConfigurationDefinition.ValueType.BOOLEAN,
            default_value=False,
            allowed_scopes=["platform"],
        )

        self.finance = Capability.objects.create(key="gate-finance", label="Finance")
        self.plus = Capability.objects.create(
            key="gate-finance-plus", label="Finance Plus", parent=self.finance,
            depth=Capability.Depth.PLUS, requires_entitlement=False,
        )

        self.core_key = make_permission(
            "gatefinance.invoice.view", capability=self.finance,
        ).key
        self.plus_key = make_permission(
            "gatefinance.feestructure.generate", capability=self.plus,
        ).key
        self.unmapped_key = make_permission("gatefinance.invoice.generate").key

    def switch_on(self):
        set_value(
            definition=self.enforce, value=True, actor=self.actor,
            tenant=None, branch=None, reason="test",
        )

    def grant(self, depth):
        set_entitlement(
            capability=self.finance, tenant=self.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
            actor=self.actor, depth=depth,
        )


class GateIsOffByDefaultTests(_GateFixture):
    """Nothing changes until somebody switches it on."""

    def test_a_shallow_school_is_not_refused_while_the_switch_is_off(self):
        self.grant(Capability.Depth.CORE)
        self.assertEqual(plan_refusal([self.plus_key], self.tenant), "")

    def test_the_switch_cannot_be_set_by_a_school(self):
        # A school-scoped switch would let a school with config.value.update
        # turn off its own plan gate.
        self.assertEqual(self.enforce.allowed_scopes, ["platform"])


class GateRefusesOnlyWhatThePlanDoesNotReachTests(_GateFixture):
    """With the switch on, depth decides and nothing else."""

    def setUp(self):
        super().setUp()
        self.switch_on()

    def test_a_core_school_is_refused_a_plus_key(self):
        self.grant(Capability.Depth.CORE)
        refusal = plan_refusal([self.plus_key], self.tenant)
        self.assertNotEqual(refusal, "")
        self.assertIn("Plus", refusal)
        self.assertIn("Core", refusal)
        self.assertIn("Finance", refusal)

    def test_a_core_school_keeps_its_core_keys(self):
        self.grant(Capability.Depth.CORE)
        self.assertEqual(plan_refusal([self.core_key], self.tenant), "")

    def test_a_deeper_plan_reaches_the_plus_key(self):
        self.grant(Capability.Depth.ADVANCED)
        self.assertEqual(plan_refusal([self.plus_key], self.tenant), "")

    def test_a_grant_with_no_depth_reaches_everything(self):
        self.grant(None)
        self.assertEqual(plan_refusal([self.plus_key], self.tenant), "")

    def test_an_unmapped_key_is_core_for_everybody(self):
        self.grant(Capability.Depth.CORE)
        self.assertEqual(plan_refusal([self.unmapped_key], self.tenant), "")

    def test_an_unprovisioned_school_is_never_refused(self):
        # No package grants at all means nobody has applied this school's
        # plan, not that it bought nothing.
        self.assertFalse(
            CapabilityEntitlement.all_objects.filter(tenant=self.tenant).exists()
        )
        self.assertEqual(plan_refusal([self.plus_key], self.tenant), "")

    def test_any_one_covered_key_lets_the_request_through(self):
        # View keys are any-of, so the plan has to refuse all of them.
        self.grant(Capability.Depth.CORE)
        self.assertEqual(
            plan_refusal([self.plus_key, self.core_key], self.tenant), "",
        )

    def test_a_module_the_school_does_not_hold_names_the_module(self):
        set_entitlement(
            capability=self.finance, tenant=self.tenant,
            state=CapabilityEntitlement.State.DENIED,
            source=CapabilityEntitlement.Source.PACKAGE, actor=self.actor,
        )
        refusal = plan_refusal([self.core_key], self.tenant)
        self.assertIn("Finance", refusal)
        self.assertIn("plan", refusal)

    def test_no_tenant_means_no_plan_to_refuse_against(self):
        self.assertEqual(plan_refusal([self.plus_key], None), "")


class RefusalIsDistinguishableTests(_GateFixture):
    """A plan refusal must not read like a role refusal."""

    def test_the_refusal_carries_its_own_error_code(self):
        self.assertEqual(PlanUpgradeRequired.error_code, "PLAN_UPGRADE_REQUIRED")

    def test_the_refusal_is_403_rather_than_a_payment_status(self):
        # Nothing about the request is a payment problem: the caller is
        # authenticated and the school may be fully paid up on a shallow tier.
        self.assertEqual(PlanUpgradeRequired.http_status, 403)

    def test_the_message_names_the_depth_needed_and_the_depth_held(self):
        self.switch_on()
        self.grant(Capability.Depth.CORE)
        message = plan_refusal([self.plus_key], self.tenant)
        self.assertIn("Finance Plus", message)
        self.assertIn("This school reaches Core", message)


class CapabilityLookupTests(_GateFixture):
    """Where a permission's capability comes from."""

    def test_the_row_answers_before_the_hardcoded_map(self):
        self.assertEqual(capability_for_permission(self.plus_key), self.plus)

    def test_an_unknown_key_is_core(self):
        self.assertIsNone(capability_for_permission("nosuch.thing.view"))

    def test_an_archived_capability_stops_governing_its_keys(self):
        # Archiving is how a capability is retired. Its keys must fall back to
        # core rather than becoming permanently unreachable.
        Capability.objects.filter(pk=self.plus.pk).update(is_active=False)
        self.assertIsNone(capability_for_permission(self.plus_key))


class _GatedView(APIView):
    """A stand-in for any of the ~325 routes that declare a permission key."""

    permission_classes = [HasRBACPermission]
    rbac_permission = "gatefinance.feestructure.generate"

    def get(self, request):
        return Response({"reached": True})


urlpatterns = [path("gated/", _GatedView.as_view())]


@override_settings(ROOT_URLCONF=__name__)
class RefusalReachesTheClientTests(_GateFixture):
    """The raise has to survive the trip out through DRF's handler.

    Unit-testing ``plan_refusal`` proves the decision. It does not prove that
    the exception becomes a 403 carrying its own code rather than an uncaught
    500, and that trip is the part with moving pieces in it.
    """

    def setUp(self):
        super().setUp()
        self.switch_on()
        self.user = make_school_admin(
            make_branch(self.school), email="gate-admin@example.com",
        )
        role = make_role(self.school, name="Gate Role")
        make_role_permission(role, Permission.objects.get(key=self.plus_key))
        make_role_permission(role, Permission.objects.get(key=self.core_key))
        make_assignment(self.school, self.user, role)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_a_shallow_school_is_told_which_wall_it_hit(self):
        self.grant(Capability.Depth.CORE)
        response = self.client.get("/gated/")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "PLAN_UPGRADE_REQUIRED")
        self.assertIn("Finance Plus", response.data["message"])

    def test_a_deep_school_reaches_the_view(self):
        self.grant(Capability.Depth.ADVANCED)
        response = self.client.get("/gated/")
        self.assertEqual(response.status_code, 200, response.data)
        # This stand-in view returns a bare payload; the success envelope real
        # views use is applied by their own response helper, not by the gate.
        self.assertTrue(response.data["reached"])

    def test_a_role_refusal_still_reads_as_a_role_refusal(self):
        # The other half of the contract. Someone whose role does not carry
        # the key must not be told to upgrade the school's plan.
        self.grant(Capability.Depth.ADVANCED)
        stranger = make_school_admin(
            make_branch(self.school, name="Other Branch", is_main=False),
            email="gate-stranger@example.com",
        )
        client = APIClient()
        client.force_authenticate(user=stranger)
        response = client.get("/gated/")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertNotEqual(
            response.data.get("error", {}).get("code"), "PLAN_UPGRADE_REQUIRED",
        )
