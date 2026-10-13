"""The window of an uplift on the plan page follows the subscription switches.

An uplift the plan wrote starts and ends with the school's subscription, so its
window is the subscription's start and expiry. An uplift an operator gave by
hand has a window of its own and is open to everyone who reads the plan page.
Bright Star Terms has a plan-written uplift on Finance and a hand-written one
on Procurement.
"""
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.test_utils import TenantAPIClient
from vs_config.models import Capability, CapabilityDepthGrant, CapabilityEntitlement
from vs_rbac.tests.helpers import install_declared_fields

from .tests_platform_schools_field_access import RESOURCE, make_operator, provision


class UpliftWindowOnThePlanPageTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", verbosity=0)
        call_command("seed_prebuilt_role_templates", verbosity=0)
        call_command("seed_config_catalogue", verbosity=0)
        call_command("seed_package", verbosity=0)
        install_declared_fields(RESOURCE)
        cls.school = provision("bright-star-window", ("Ikeja Branch", "Lekki Branch"))
        now = timezone.now()
        for key, source in (
            ("finance", CapabilityEntitlement.Source.PACKAGE),
            ("procurement", CapabilityEntitlement.Source.MANUAL),
        ):
            CapabilityDepthGrant.all_objects.create(
                capability=Capability.objects.get(key=key), tenant=cls.school.tenant,
                depth=Capability.Depth.ADVANCED, source=source, reason="Signed on a promise.",
                starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=90),
            )
        cls.open = make_operator("window-open@codexng.test")
        cls.no_expiry = make_operator("window-no-expiry@codexng.test", closed=("subscription_expires_at",))
        cls.no_start = make_operator("window-no-start@codexng.test", closed=("subscription_starts_at",))

    def uplifts(self, user):
        response = TenantAPIClient(user=user).get(
            reverse("school-plan", kwargs={"slug": self.school.slug}))
        self.assertEqual(response.status_code, 200, response.data)
        modules = {m["key"]: m for m in response.data["data"]["modules"]}
        return modules["finance"]["uplift"], modules["procurement"]["uplift"]

    def test_an_open_role_reads_both_windows(self):
        for uplift in self.uplifts(self.open):
            self.assertIn("starts_at", uplift)
            self.assertIn("ends_at", uplift)

    def test_a_hidden_expiry_removes_the_end_of_a_plan_written_uplift_only(self):
        plan_written, by_hand = self.uplifts(self.no_expiry)

        self.assertNotIn("ends_at", plan_written)
        self.assertIn("starts_at", plan_written)
        self.assertIn("ends_at", by_hand)
        self.assertIn("starts_at", by_hand)

    def test_a_hidden_start_removes_the_start_of_a_plan_written_uplift_only(self):
        plan_written, by_hand = self.uplifts(self.no_start)

        self.assertNotIn("starts_at", plan_written)
        self.assertIn("ends_at", plan_written)
        self.assertIn("starts_at", by_hand)

    def test_the_source_is_not_part_of_the_payload(self):
        for uplift in self.uplifts(self.open):
            self.assertNotIn("source", uplift)
