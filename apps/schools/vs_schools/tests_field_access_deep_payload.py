"""No controlled school-profile field reaches a role with Read switched off.

Both the profile shown after sign-in and the serializer used for corrections
are rendered on real schools. Bright Star has two branches and Sunrise has one,
so the school shape cannot accidentally change whether a field is filtered.
"""
from django.test import TestCase

from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample
from vs_rbac.tests.helpers import make_branch, make_school

_SURFACE = "schools.vs_schools.serializers."


class SchoolProfileDeepPayloadTests(DeepPayloadChecks, TestCase):
    resources = frozenset({"school.profile"})
    covers = frozenset({
        _SURFACE + "SchoolProfileSerializer",
        _SURFACE + "SchoolProfileUpdateSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        cls.multi = make_school(slug="bright-star-deep", name="Bright Star Deep")
        make_branch(cls.multi, name="Ikeja Branch", is_main=True)
        make_branch(cls.multi, name="Lekki Branch", is_main=False)
        cls.multi.address = "14 Lantern Road"
        cls.multi.registration_id = "REG-DEEP-MULTI"
        cls.multi.save(update_fields=["address", "registration_id"])

        cls.single = make_school(slug="sunrise-deep", name="Sunrise Deep")
        make_branch(cls.single, name="Main Branch", is_main=True)
        cls.single.address = "3 Sunrise Close"
        cls.single.registration_id = "REG-DEEP-SINGLE"
        cls.single.save(update_fields=["address", "registration_id"])

    def samples(self):
        return [
            Sample(surface, school, tenant=school.tenant)
            for school in (self.multi, self.single)
            for surface in self.covers
        ]
