"""A checklist that only offers steps the school's plan can actually perform.

Bright Star is created on Basic and its administrator opens the control room.
The fourth card says "Upload Initial Datasets". She presses it, picks her
student roll, and the upload is refused: loading data from a file is sold at a
depth her school does not reach. Nothing on the card said so, and the refusal
is the first she hears of it.

``CatalogEntry.applies_to`` exists for exactly this. Its own docstring puts it
better than a new one could: a school that cannot ever perform a step does not
have an optional step it may ignore, it has no such step at all. So the entry
answers the plan, and a school without bulk import is never shown the card.

The direction that matters most here is the safe one. Three separate states
would each read as "no bulk import" if the question were asked carelessly - a
catalogue that has not been seeded, a school whose plan was never applied, and
a school that genuinely did not buy it - and only the last should take a card
away. A school created before entitlements were written reliably has no grants
at all, and hiding its onboarding steps would be the platform quietly
withdrawing a step it had always offered.

Required steps are untouched by all of this. Readiness counts only required
tasks, and every required task is on every plan, so no plan can leave a school
unable to reach go-live.
"""
from django.test import TestCase

from vs_config.models import Capability, CapabilityEntitlement
from vs_rbac.tests.helpers import make_branch, make_school

from .constants import TaskKey
from .models import OnboardingTask
from .services.provisioning import provision_onboarding


class ChecklistFollowsThePlanTests(TestCase):
    def setUp(self):
        self.school = make_school(slug="bright-star", name="Bright Star")
        make_branch(self.school, name="Main Branch")
        self.tenant = self.school.tenant

        self.platform = Capability.objects.create(
            key="platform", label="Platform Services",
        )
        self.bulk_import = Capability.objects.create(
            key="bulk_import", label="Bulk Data Import",
            parent=self.platform, depth=Capability.Depth.PLUS,
        )

    def _grant(self, depth):
        """Give the school the platform module at one depth, as a plan does."""
        CapabilityEntitlement.objects.update_or_create(
            tenant=self.tenant, capability=self.platform,
            defaults={
                "state": CapabilityEntitlement.State.GRANTED,
                "source": CapabilityEntitlement.Source.PACKAGE,
                "depth": depth,
            },
        )

    def _keys(self):
        provision_onboarding(self.tenant)
        return set(
            OnboardingTask.all_objects.filter(tenant=self.tenant)
            .values_list("key", flat=True)
        )

    def test_a_shallow_plan_is_not_offered_the_upload_card(self):
        self._grant(Capability.Depth.CORE)
        self.assertNotIn(TaskKey.INITIAL_DATA, self._keys())

    def test_a_deeper_plan_is(self):
        self._grant(Capability.Depth.PLUS)
        self.assertIn(TaskKey.INITIAL_DATA, self._keys())

    def test_every_required_step_survives_the_shallowest_plan(self):
        # No plan may leave a school unable to reach go-live.
        self._grant(Capability.Depth.CORE)
        keys = self._keys()
        for required in (
            TaskKey.DEFAULT_ROLES,
            TaskKey.SCHOOL_METADATA,
            TaskKey.ACADEMIC_STRUCTURE,
        ):
            with self.subTest(step=required):
                self.assertIn(required, keys)

    def test_a_school_whose_plan_was_never_applied_keeps_the_card(self):
        # No PACKAGE grants at all is "not provisioned", not "did not buy".
        self.assertFalse(
            CapabilityEntitlement.all_objects.filter(tenant=self.tenant).exists()
        )
        self.assertIn(TaskKey.INITIAL_DATA, self._keys())

    def test_an_unseeded_catalogue_keeps_the_card(self):
        # A platform whose capability rows are missing must not silently strip
        # a step from every school on it.
        self._grant(Capability.Depth.CORE)
        Capability.objects.filter(pk=self.bulk_import.pk).delete()
        self.assertIn(TaskKey.INITIAL_DATA, self._keys())

    def test_an_upgrade_opens_the_step(self):
        self._grant(Capability.Depth.CORE)
        self.assertNotIn(TaskKey.INITIAL_DATA, self._keys())
        # Provisioning is idempotent and tops up what is missing, so the school
        # gains the card without losing the steps it has already completed.
        self._grant(Capability.Depth.PLUS)
        self.assertIn(TaskKey.INITIAL_DATA, self._keys())

    def test_a_downgrade_leaves_a_step_the_school_already_has(self):
        # Provisioning adds and never removes. A school that bought the step,
        # used it, and then moved down keeps the record of having done it -
        # taking the card away would rewrite its own history of onboarding.
        self._grant(Capability.Depth.PLUS)
        self.assertIn(TaskKey.INITIAL_DATA, self._keys())
        self._grant(Capability.Depth.CORE)
        self.assertIn(TaskKey.INITIAL_DATA, self._keys())
