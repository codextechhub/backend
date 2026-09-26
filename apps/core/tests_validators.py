"""The shared phone rule, and the serializers that take a typed phone number."""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from core.validators import phone_validator

VALID = ["08012345678", "+2348012345678", "+44 20 7946 0958", "(0)1 234-5678", "5550123"]
INVALID = ["abc", "call Tunde", "0801", "++2348012345678", "0801234567890123456789012"]


class PhoneValidatorTests(SimpleTestCase):
    def test_accepts_local_and_international_numbers(self):
        for value in VALID:
            with self.subTest(value=value):
                phone_validator(value)

    def test_rejects_text_and_malformed_numbers(self):
        for value in INVALID:
            with self.subTest(value=value), self.assertRaises(DjangoValidationError):
                phone_validator(value)


class AdminPhoneFieldTests(SimpleTestCase):
    """Every serializer that takes an administrator's phone applies the rule."""

    def _fields(self):
        from schools.vs_schools.serializers import (
            BranchPrimaryAdminWriteSerializer,
            SchoolPrimaryAdminWriteSerializer,
        )
        from vs_user.serializers import UserUpdateSerializer

        return {
            "school primary admin": SchoolPrimaryAdminWriteSerializer().fields["phone"],
            "branch primary admin": BranchPrimaryAdminWriteSerializer().fields["phone"],
            "user update": UserUpdateSerializer().fields["phone"],
        }

    def test_local_number_is_accepted(self):
        for name, field in self._fields().items():
            with self.subTest(serializer=name):
                self.assertEqual(field.run_validation("08012345678"), "08012345678")

    def test_blank_is_accepted(self):
        for name, field in self._fields().items():
            with self.subTest(serializer=name):
                self.assertEqual(field.run_validation(""), "")

    def test_text_is_refused(self):
        for name, field in self._fields().items():
            with self.subTest(serializer=name), self.assertRaises(ValidationError):
                field.run_validation("call Tunde")
