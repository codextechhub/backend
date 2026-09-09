"""One school, created the way a real one is, meeting the wall.

Every other test in this area proves a piece. This proves the whole path with
nothing stubbed: the real seeders, a school created through the real endpoint
on a real plan, a real member of staff holding the key through a real role, and
a real HTTP request that comes back refused with the plan's reason on it.

It is here because a chain of individually correct parts can still not join up.
The gate reads a band off a permission; the band is written by a seeder; the
depth comes from a plan; the plan is applied at school creation. Any one of
those links being absent looks exactly like the gate working, because a gate
that refuses nothing and a gate that is never reached are the same from
outside. That happened once already: the plans carried no depth, so every
school created after the gate went live would have reached everything, and no
test in the suite would have noticed.
"""
from datetime import date, timedelta

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from schools.vs_schools.models import PackagePlan, School, SchoolStatus
from vs_config.models import Capability, CapabilityEntitlement
from vs_config.services.resolution import set_value
from vs_config.models import ConfigurationDefinition

from ..models import Permission, PermissionScope
from ..plan_gate import plan_refusal
from .helpers import (
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    make_vision_user,
)


class OneSchoolMeetsTheWallTests(TestCase):
    """Bright Star on Basic, reaching for something Basic does not include."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", verbosity=0)
        call_command("seed_config_catalogue", verbosity=0)
        call_command("seed_package", verbosity=0)
        call_command("seed_school_permissions", verbosity=0)
        call_command("seed_finance_permissions", verbosity=0)
        call_command("seed_permission_bands", verbosity=0)

        cls.operator = make_vision_user(
            email="operator@codexng.test", super_admin=True,
        )
        cls.basic = PackagePlan.objects.get(code="basic")

    def setUp(self):
        definition = ConfigurationDefinition.objects.get(
            key="platform.entitlements.enforce"
        )
        set_value(
            definition=definition, value=True, actor=self.operator,
            tenant=None, branch=None, reason="Demonstrating the gate.",
        )

    def _operator_client(self):
        client = APIClient()
        client.force_authenticate(user=self.operator)
        return client

    def _create_school(self, slug, plan):
        response = self._operator_client().post(
            reverse("school-create"),
            {
                "name": "Bright Star Academy",
                "slug": slug,
                "package_setup_data": {
                    "package_plan": plan.code,
                    "subscription_expires_at": (
                        date.today() + timedelta(days=365)
                    ).isoformat(),
                },
                "branches": [{
                    "name": "Bright Star Main Branch",
                    "state": "Lagos",
                    "is_main": True,
                    "primary_admin_data": {
                        "full_name": "Bright Star Head",
                        "email": f"head@{slug}.test",
                    },
                }],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return School.objects.get(slug=slug)

    def test_the_plan_a_school_signs_reaches_its_grants(self):
        # The link the plans' missing depth broke once already.
        school = self._create_school("gate-demo-basic", self.basic)
        depths = {
            row.capability.key: row.depth
            for row in CapabilityEntitlement.all_objects.filter(
                tenant=school.tenant
            ).select_related("capability")
        }
        self.assertTrue(depths, "the plan granted nothing")
        self.assertEqual(
            set(depths.values()), {Capability.Depth.CORE},
            "Basic granted a depth other than Core, or none at all",
        )

    def test_a_basic_school_is_refused_a_plus_key_and_told_why(self):
        school = self._create_school("gate-demo-refused", self.basic)
        refusal = plan_refusal(["finance.feestructure.generate"], school.tenant)
        self.assertNotEqual(refusal, "", "a Basic school reached a Plus key")
        self.assertIn("Finance", refusal)
        # The tier is structured data on the refusal, never words in it.
        self.assertNotIn("Plus", refusal)

    def test_a_basic_school_can_still_load_its_own_data(self):
        """The step every school starts on, on the plan most of them start on.

        Every import.* key answers to one band, so pricing that band above the
        cheapest plan took the whole engine away from a Basic school - not just
        the upload. Its onboarding step rendered with an empty template table,
        because the list itself was refused, and the school could see neither
        what it was meant to upload nor why it could not.
        """
        school = self._create_school("gate-demo-import", self.basic)
        for key in (
            "import.templates.view",
            "import.batches.view",
            "import.batches.create",
            "school.students.import",
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    plan_refusal([key], school.tenant), "",
                    "a school on the cheapest plan cannot load its own roll",
                )

    def test_the_same_school_keeps_every_core_key(self):
        school = self._create_school("gate-demo-core", self.basic)
        for key in (
            "finance.invoice.create",
            "finance.payment.create",
            "school.students.create",
            "academics.session.view",
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    plan_refusal([key], school.tenant), "",
                    f"{key} is Core and was refused",
                )

    def test_a_deeper_plan_reaches_what_basic_cannot(self):
        premium = PackagePlan.objects.get(code="premium")
        school = self._create_school("gate-demo-premium", premium)
        for key in (
            "finance.feestructure.generate",
            "finance.payrollrun.create",
        ):
            with self.subTest(key=key):
                self.assertEqual(plan_refusal([key], school.tenant), "")

    def test_the_refusal_reaches_a_real_caller_over_http(self):
        """The whole path, ending at a status code a browser would see."""
        school = self._create_school("gate-demo-http", self.basic)
        # A school arrives from the wizard PENDING, and TenantSurfaceAllowed
        # refuses a pending tenant everything before any other gate is asked.
        # That ordering is right and is why the school is taken live here: the
        # refusal under test is the plan's, not the lifecycle's.
        school.status = SchoolStatus.ACTIVE
        school.save(update_fields=["status"])
        branch = school.tenant.branches.first()

        bursar = make_school_admin(branch, email="bursar@gate-demo-http.test")
        role = make_role(school, name="Bursar")
        for key in ("finance.feestructure.view", "finance.feestructure.generate"):
            make_role_permission(role, Permission.objects.get(key=key))
        make_assignment(school, bursar, role)

        client = APIClient()
        client.force_authenticate(user=bursar)
        response = client.post(
            "/v1/finance/fee-structures/1/generate/",
            {}, format="json", HTTP_X_TENANT=school.slug,
        )
        # Her role carries the key. What refuses her is the plan, and the code
        # says so rather than reading like a role problem.
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(
            response.data.get("error", {}).get("code"), "PLAN_UPGRADE_REQUIRED",
        )
