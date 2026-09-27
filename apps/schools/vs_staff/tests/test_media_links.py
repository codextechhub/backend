"""A staff photograph and a staff document are handed out as links that open.

The media view serves a file only against a signature bound to the reader, and
the browser loads it from the API's host rather than the frontend's. A link
built from ``FieldFile.url`` has neither: it is the storage's bare ``/media/``
path, which the media view refuses and which a browser resolves against the
page's own origin. These tests pin the two links the staff screens render.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from django.core.files.base import ContentFile

from core.media import TOKEN_PARAM
from schools.vs_staff.models import StaffDocument

from .base import StaffFixture

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class MediaLinkTests(StaffFixture):
    def assertSigned(self, url):
        self.assertTrue(url, "no link was returned")
        parsed = urlparse(url)
        self.assertIn(parsed.scheme, ("http", "https"), url)
        self.assertIn(TOKEN_PARAM, parse_qs(parsed.query), url)

    def test_a_staff_photograph_is_a_signed_absolute_link(self):
        self.eze.photo.save("eze.png", ContentFile(PNG), save=True)

        response = self.get(self.admin, "staff-detail", pk=self.eze.pk)

        self.assertEqual(response.status_code, 200)
        self.assertSigned(response.data["data"]["photo_url"])

    def test_a_staff_document_is_a_signed_absolute_link(self):
        document = StaffDocument(
            tenant=self.tenant, staff=self.eze, document_type="OTHER",
            title="Appointment letter", uploaded_by=self.admin,
        )
        document.file.save("letter.png", ContentFile(PNG), save=True)

        response = self.get(self.admin, "staff-documents", pk=self.eze.pk)

        self.assertEqual(response.status_code, 200)
        self.assertSigned(response.data["data"][0]["file_url"])

    def test_a_person_with_no_photograph_has_no_link(self):
        response = self.get(self.admin, "staff-detail", pk=self.eze.pk)

        self.assertIsNone(response.data["data"]["photo_url"])
