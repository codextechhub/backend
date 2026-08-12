"""
Tests for the project-wide DRF exception handler envelope.

`ValidationError("some text")` renders as a bare list rather than a dict, and
the handler used to call `.get("detail")` on it unconditionally - turning every
such 400 anywhere in the API into a 500.
"""
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

    def test_field_errors_fall_back_to_the_generic_message_and_keep_detail(self):
        response = self._handle(ValidationError({"to_state": ["Invalid choice."]}))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Check the error details", response.data["message"])
        self.assertEqual(
            response.data["error"]["detail"], {"to_state": ["Invalid choice."]}
        )

    def test_multi_item_list_error_falls_back_to_the_generic_message(self):
        response = self._handle(ValidationError(["First problem.", "Second problem."]))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Check the error details", response.data["message"])

    def test_standard_api_exception_keeps_its_detail(self):
        response = self._handle(NotFound())

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["error"]["code"], "REQUEST_ERROR")
