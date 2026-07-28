from django.test import TestCase

from .models import School
from .serializers import SchoolCreateSerializer


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
