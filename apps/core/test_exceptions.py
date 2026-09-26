"""
Tests for the project-wide DRF exception handler envelope.

`ValidationError("some text")` renders as a bare list rather than a dict, so a
handler calling `.get("detail")` on it unconditionally turns every such 400
anywhere in the API into a 500.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError

from core.exceptions import custom_exception_handler


class CustomExceptionHandlerTests(TestCase):
    def _handle(self, exc):
        return custom_exception_handler(exc, {})

    def test_string_validation_error_returns_400_with_its_message(self):
        response = self._handle(ValidationError("Branch is already ACTIVE."))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["success"])
        self.assertEqual(response.data["message"], "Branch is already ACTIVE.")

    def test_dict_validation_error_uses_detail_key(self):
        response = self._handle(ValidationError({"detail": "No changes detected."}))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["message"], "No changes detected.")

    def test_an_explicit_detail_wins_over_its_sibling_keys(self):
        response = self._handle(ValidationError({
            "detail": "This export is too large to run now.",
            "code": "EXPORT_TOO_LARGE",
        }))

        self.assertEqual(
            response.data["message"], "This export is too large to run now."
        )

    def test_a_single_field_error_is_the_message_and_stays_in_detail(self):
        response = self._handle(
            ValidationError({"logo": "This file is not really a PNG."})
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["message"],
            "This file is not really a PNG.",
        )
        self.assertEqual(
            response.data["error"]["detail"],
            {"logo": "This file is not really a PNG."},
        )

    def test_several_field_errors_each_name_their_field(self):
        response = self._handle(ValidationError({
            "email": ["That address already has an account."],
            "role": ["Pick a role."],
        }))

        self.assertEqual(
            response.data["message"],
            "email: That address already has an account.; role: Pick a role.",
        )

    def test_non_field_errors_are_not_prefixed(self):
        response = self._handle(ValidationError({
            "non_field_errors": ["The end date is before the start date."],
            "name": ["This field is required."],
        }))

        self.assertEqual(
            response.data["message"],
            "The end date is before the start date.; name: This field is required.",
        )

    def test_a_nested_row_error_carries_its_path(self):
        response = self._handle(ValidationError({
            "lines": [{}, {"amount": ["Must be greater than zero."]}],
        }))

        self.assertEqual(response.data["message"], "Must be greater than zero.")

        response = self._handle(ValidationError({
            "lines": [{"amount": ["Required."]}, {"amount": ["Required."]}],
        }))

        self.assertEqual(
            response.data["message"],
            "lines.0.amount: Required.; lines.1.amount: Required.",
        )

    def test_a_long_list_of_errors_is_summarised(self):
        response = self._handle(ValidationError({f"f{n}": ["Bad."] for n in range(8)}))

        self.assertTrue(response.data["message"].endswith("; and 3 more"))

    def test_multi_item_list_error_joins_its_messages(self):
        response = self._handle(ValidationError(["First problem.", "Second problem."]))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["message"], "First problem.; Second problem."
        )

    def test_an_error_with_no_message_falls_back_to_the_generic_one(self):
        response = self._handle(ValidationError({}))

        self.assertIn("Check the error details", response.data["message"])

    def test_standard_api_exception_keeps_its_detail(self):
        response = self._handle(NotFound())

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["error"]["code"], "REQUEST_ERROR")


class DjangoValidationErrorEnvelopeTests(TestCase):
    """A model's own refusal has to say which field it is about.

    This branch read `exc.messages`, which flattens `{"_type": ["This field
    cannot be blank."]}` into `["This field cannot be blank."]`. Every service
    in this codebase that calls `full_clean()` - finance, tickets, todo, user,
    audit, and the branch and school update serializers - reaches the API
    through here, so a required-field failure anywhere told the caller a field
    was blank and never which one.
    """

    def _handle(self, exc):
        return custom_exception_handler(exc, {})

    def test_a_field_error_names_its_field_in_the_message(self):
        response = self._handle(
            DjangoValidationError({"_type": ["This field cannot be blank."]})
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["message"], "_type: This field cannot be blank."
        )

    def test_a_field_error_keeps_its_field_in_the_detail(self):
        response = self._handle(
            DjangoValidationError({"_type": ["This field cannot be blank."]})
        )

        self.assertEqual(
            response.data["error"]["detail"],
            {"_type": ["This field cannot be blank."]},
        )
        self.assertEqual(response.data["error"]["code"], "VALIDATION_ERROR")

    def test_several_fields_are_all_named(self):
        response = self._handle(DjangoValidationError({
            "name": ["This field cannot be blank."],
            "slug": ["This slug is reserved. Choose another."],
        }))

        self.assertIn("name: This field cannot be blank.", response.data["message"])
        self.assertIn(
            "slug: This slug is reserved. Choose another.", response.data["message"]
        )

    def test_a_message_with_no_field_is_not_given_a_fake_one(self):
        """`ValidationError("some text")` from a service has no field, and
        inventing `__all__:` in front of it would read as a field name."""
        response = self._handle(DjangoValidationError("Subscription has expired."))

        self.assertEqual(response.data["message"], "Subscription has expired.")
        self.assertEqual(
            response.data["error"]["detail"], {"__all__": ["Subscription has expired."]}
        )
