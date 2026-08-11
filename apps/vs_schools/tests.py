from django.test import TestCase

from .models import School
from .serializers import SchoolCreateSerializer
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_rbac.tests.helpers import make_vision_user


class SchoolCodeAllocationTests(TestCase):
    def test_model_allocates_code_when_omitted(self):
        school = School.objects.create(name="Generated School", slug="generated-school")

        self.assertTrue(school.code.startswith(f"SC-{school.tenant_id}"))

    def test_create_serializer_validates_without_code(self):
        serializer = SchoolCreateSerializer(data={
            "name": "Serializer School",
            "ownership_type": "PRIVATE",
            "address": "1 Test Road",
            "term_structure": "3_TERMS",
            "currency": "NGN",
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_create_serializer_uses_platform_defaults_only_for_omitted_fields(self):
        actor = make_vision_user(email="onboarding-defaults@example.com")
        set_value(
            definition=ConfigurationDefinition.objects.get(
                key="platform.onboarding.default_ownership_type"
            ),
            value="NGO",
            actor=actor,
        )
        set_value(
            definition=ConfigurationDefinition.objects.get(
                key="platform.onboarding.default_currency"
            ),
            value="USD",
            actor=actor,
        )

        omitted = SchoolCreateSerializer(data={"name": "Defaults School"})
        explicit = SchoolCreateSerializer(data={
            "name": "Explicit School",
            "ownership_type": "PRIVATE",
            "currency": "NGN",
        })

        self.assertTrue(omitted.is_valid(), omitted.errors)
        self.assertEqual(omitted.validated_data["ownership_type"], "NGO")
        self.assertEqual(omitted.validated_data["currency"], "USD")
        self.assertTrue(explicit.is_valid(), explicit.errors)
        self.assertEqual(explicit.validated_data["ownership_type"], "PRIVATE")
        self.assertEqual(explicit.validated_data["currency"], "NGN")
