"""A client asking for a file's own type gets the file, never a 406.

See ``core.negotiation``.
"""

from django.http import HttpResponse
from django.test import SimpleTestCase
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from core.negotiation import FallbackContentNegotiation


class _PdfView(APIView):
    """A dual-format view: JSON by default, the file with ``?output=pdf``."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        if request.query_params.get("missing"):
            raise NotFound("Payslip not found.")
        if request.query_params.get("output") == "pdf":
            return HttpResponse(b"%PDF-1.4", content_type="application/pdf")
        return Response({"net": 100})


def _get(query="", **meta):
    request = APIRequestFactory().get(f"/payslip/{query}", **meta)
    return _PdfView.as_view()(request)


class FallbackContentNegotiationTests(SimpleTestCase):
    def test_it_is_the_project_default(self):
        self.assertIs(api_settings.DEFAULT_CONTENT_NEGOTIATION_CLASS, FallbackContentNegotiation)

    def test_a_pdf_request_receives_the_pdf(self):
        response = _get("?output=pdf", HTTP_ACCEPT="application/pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response.content, b"%PDF-1.4")

    def test_an_image_request_is_not_refused(self):
        response = _get(HTTP_ACCEPT="image/png")
        self.assertEqual(response.status_code, 200)

    def test_an_error_answers_a_pdf_request_in_json(self):
        response = _get("?output=pdf&missing=1", HTTP_ACCEPT="application/pdf")
        response.render()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_a_json_request_is_unchanged(self):
        response = _get(HTTP_ACCEPT="application/json")
        response.render()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_an_unknown_format_override_is_still_not_found(self):
        response = _get("?format=pdf")
        self.assertEqual(response.status_code, 404)
