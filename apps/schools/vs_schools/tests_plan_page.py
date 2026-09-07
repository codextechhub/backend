"""The school plan page: reading what a school is on, and changing it.

Three journeys, and the failure modes that matter are all on the write side.

The read exists because the console could not answer the first question of any
support call. The effective-capability read says on or off per capability and
nothing about depth, so an operator could see that a school had been refused
without being able to see why, or which of three things to change.

The writes are dangerous in one specific way: a plan change that writes the
plan without re-granting leaves the row saying Standard while the product
behaves like Basic. That state is indistinguishable from the gate misbehaving,
and it is what these tests spend most of their time on.
"""
from datetime import date, timedelta

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from vs_config.models import Capability, CapabilityDepthGrant, CapabilityEntitlement
from vs_rbac.tests.helpers import make_school_admin, make_vision_user

from .models import PackagePlan, School, SchoolPackageSetup


class _PlanPage(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", verbosity=0)
        call_command("seed_config_catalogue", verbosity=0)
        call_command("seed_package", verbosity=0)

        cls.operator = make_vision_user(
            email="plan-operator@codexng.test", super_admin=True,
        )
        cls.basic = PackagePlan.objects.get(code="basic")
        cls.standard = PackagePlan.objects.get(code="standard")
        cls.finance = Capability.objects.get(key="finance")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.operator)
        self.school = self._create("bright-star-plan", self.basic)

    def _create(self, slug, plan):
        response = self.client.post(
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
                    "name": "Bright Star Main Branch", "_type": "Main",
                    "state": "Lagos", "is_main": True,
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

    def _plan_url(self):
        return reverse("school-plan", kwargs={"slug": self.school.slug})

    def _overview(self):
        response = self.client.get(self._plan_url())
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def _module(self, overview, key):
        return next(m for m in overview["modules"] if m["key"] == key)


class ReadingThePlanTests(_PlanPage):
    """The answer to "what is this school on?"."""

    def test_the_plan_and_its_depth_are_reported(self):
        data = self._overview()
        self.assertEqual(data["plan"]["code"], "basic")
        self.assertEqual(data["plan"]["default_depth_label"], "Core")
        self.assertTrue(data["provisioned"])

    def test_every_module_reports_the_depth_it_reaches(self):
        data = self._overview()
        self.assertTrue(data["modules"])
        for module in data["modules"]:
            with self.subTest(module=module["key"]):
                self.assertTrue(module["granted"])
                self.assertEqual(module["depth_label"], "Core")
                self.assertEqual(module["source"], "plan default")

    def test_the_bands_say_what_the_depth_buys_and_what_it_does_not(self):
        # "Plus" means nothing on a phone call; the band labels do.
        finance = self._module(self._overview(), "finance")
        reached = {b["depth_label"]: b["reached"] for b in finance["bands"]}
        self.assertTrue(reached["Core"])
        self.assertFalse(reached["Plus"])
        self.assertFalse(reached["Advanced"])

    def test_a_school_with_no_plan_reads_as_unprovisioned(self):
        from vs_rbac.tests.helpers import make_branch, make_school

        other = make_school(slug="no-plan-school")
        make_branch(other)
        response = self.client.get(
            reverse("school-plan", kwargs={"slug": other.slug})
        )
        self.assertEqual(response.status_code, 200, response.data)
        # Unprovisioned and unentitled are different facts, and the gate
        # refuses only one of them.
        self.assertIsNone(response.data["data"]["plan"])
        self.assertFalse(response.data["data"]["provisioned"])


class ChangingThePlanTests(_PlanPage):
    """The write that must never half-apply."""

    def test_moving_to_a_deeper_plan_regrants_in_the_same_breath(self):
        response = self.client.patch(
            self._plan_url(), {"package_plan": "standard"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        setup = SchoolPackageSetup.objects.get(school=self.school)
        self.assertEqual(setup.package_plan.code, "standard")
        depths = set(
            CapabilityEntitlement.all_objects.filter(
                tenant=self.school.tenant
            ).values_list("depth", flat=True)
        )
        self.assertEqual(
            depths, {Capability.Depth.PLUS},
            "the plan row moved and the grants did not, which reads as the "
            "gate misbehaving rather than as a half-finished change",
        )

    def test_the_response_shows_the_school_at_its_new_depth(self):
        response = self.client.patch(
            self._plan_url(), {"package_plan": "standard"}, format="json",
        )
        finance = next(
            m for m in response.data["data"]["modules"] if m["key"] == "finance"
        )
        self.assertEqual(finance["depth_label"], "Plus")

    def test_moving_to_a_shallower_plan_takes_the_depth_back(self):
        self.client.patch(self._plan_url(), {"package_plan": "standard"}, format="json")
        self.client.patch(self._plan_url(), {"package_plan": "basic"}, format="json")
        self.assertEqual(self._module(self._overview(), "finance")["depth_label"], "Core")

    def test_the_plan_it_is_already_on_is_refused(self):
        # A no-op that re-grants every module and audits each one reads later
        # as a change somebody made.
        response = self.client.patch(
            self._plan_url(), {"package_plan": "basic"}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_an_unknown_plan_is_refused(self):
        response = self.client.patch(
            self._plan_url(), {"package_plan": "platinum"}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_a_school_admin_cannot_change_their_own_plan(self):
        branch = self.school.tenant.branches.first()
        admin = make_school_admin(branch, email="admin@bright-star-plan.test")
        client = APIClient()
        client.force_authenticate(user=admin)
        response = client.patch(
            self._plan_url(), {"package_plan": "standard"}, format="json",
        )
        self.assertIn(response.status_code, (403, 404), response.data)
        self.assertEqual(
            SchoolPackageSetup.objects.get(school=self.school).package_plan.code,
            "basic",
        )


class UpliftTests(_PlanPage):
    """The deal: deeper reach for one school, ending on its own date."""

    def _uplift_url(self):
        return reverse("school-plan-uplift", kwargs={"slug": self.school.slug})

    def test_an_uplift_carries_one_module_deeper(self):
        response = self.client.post(
            self._uplift_url(),
            {
                "capability": "finance",
                "depth": Capability.Depth.ADVANCED,
                "ends_at": (timezone.now() + timedelta(days=365)).isoformat(),
                "reason": "Signed on the promise of payroll.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        finance = next(
            m for m in response.data["data"]["modules"] if m["key"] == "finance"
        )
        self.assertEqual(finance["depth_label"], "Advanced")
        self.assertEqual(finance["source"], "uplift")
        self.assertEqual(finance["plan_depth_label"], "Core")

    def test_the_uplift_leaves_the_other_modules_where_they_were(self):
        self.client.post(
            self._uplift_url(),
            {"capability": "finance", "depth": Capability.Depth.ADVANCED,
             "reason": "Deal."},
            format="json",
        )
        self.assertEqual(
            self._module(self._overview(), "students")["depth_label"], "Core",
        )

    def test_a_reason_is_required(self):
        # Somebody asks about this months later, usually when it expires.
        response = self.client.post(
            self._uplift_url(),
            {"capability": "finance", "depth": Capability.Depth.ADVANCED},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_naming_a_band_is_refused_and_says_what_to_name(self):
        response = self.client.post(
            self._uplift_url(),
            {"capability": "finance_advanced", "depth": Capability.Depth.ADVANCED,
             "reason": "Deal."},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("finance", str(response.data).lower())

    def test_an_expiry_in_the_past_is_refused(self):
        response = self.client.post(
            self._uplift_url(),
            {"capability": "finance", "depth": Capability.Depth.ADVANCED,
             "ends_at": (timezone.now() - timedelta(days=1)).isoformat(),
             "reason": "Deal."},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_a_plan_change_leaves_the_uplift_standing(self):
        # It lives in its own table so a tier change underneath it does not
        # disturb it, and it stops mattering once the tier passes it.
        self.client.post(
            self._uplift_url(),
            {"capability": "finance", "depth": Capability.Depth.ADVANCED,
             "reason": "Deal."},
            format="json",
        )
        self.client.patch(self._plan_url(), {"package_plan": "standard"}, format="json")
        finance = self._module(self._overview(), "finance")
        self.assertEqual(finance["depth_label"], "Advanced")
        self.assertEqual(finance["plan_depth_label"], "Plus")
        self.assertTrue(
            CapabilityDepthGrant.all_objects.filter(
                tenant=self.school.tenant, capability=self.finance
            ).exists()
        )

    def test_withdrawing_an_uplift_returns_the_module_to_its_plan(self):
        self.client.post(
            self._uplift_url(),
            {"capability": "finance", "depth": Capability.Depth.ADVANCED,
             "reason": "Deal."},
            format="json",
        )
        response = self.client.delete(
            reverse(
                "school-plan-uplift-detail",
                kwargs={"slug": self.school.slug, "capability": "finance"},
            )
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self._module(self._overview(), "finance")["depth_label"], "Core")

    def test_withdrawing_one_that_was_never_given_says_so(self):
        response = self.client.delete(
            reverse(
                "school-plan-uplift-detail",
                kwargs={"slug": self.school.slug, "capability": "students"},
            )
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("no uplift", response.data["message"].lower())
