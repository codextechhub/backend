"""A photograph is judged by its bytes, never by the type the browser claims.

``check_upload`` guards every photograph: a pupil's passport photograph, a
guardian's, and the ones pupils and parents set from their own apps. The
content type on an upload is whatever the sender writes, so each case here
lies about it.
"""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework import serializers

from core.image_fixtures import image_bytes, photo
from schools.vs_students.serializers import check_upload

from .base import StudentsFixture


def _check(upload):
    check_upload(upload, field="photo", must_be_image=True, subject="A photograph")


class CheckUploadTests(SimpleTestCase):
    def assert_refused(self, upload, says):
        with self.assertRaises(serializers.ValidationError) as caught:
            _check(upload)
        message = str(caught.exception.detail["photo"])
        self.assertIn("must be an image - JPEG, PNG or WebP", message)
        self.assertIn(says, message)

    def test_real_jpeg_png_and_webp_pass(self):
        for name in ("face.jpg", "face.png", "face.webp"):
            with self.subTest(name=name):
                _check(photo(name))

    def test_filler_labelled_as_a_jpeg_is_refused(self):
        self.assert_refused(
            SimpleUploadedFile("face.jpg", b"x" * 64, content_type="image/jpeg"),
            "could not be opened as a picture",
        )

    def test_a_web_page_labelled_as_a_png_is_refused(self):
        self.assert_refused(
            SimpleUploadedFile("face.png", b"<html><script>1</script></html>", content_type="image/png"),
            "could not be opened as a picture",
        )

    def test_a_png_with_a_broken_checksum_is_refused(self):
        good = bytearray(image_bytes("PNG"))
        good[-20] ^= 0xFF  # inside the IDAT chunk
        self.assert_refused(
            SimpleUploadedFile("face.png", bytes(good), content_type="image/png"),
            "could not be opened as a picture",
        )

    def test_a_heic_photo_is_refused_by_name(self):
        heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
        self.assert_refused(
            SimpleUploadedFile("IMG_0001.heic", heic, content_type="image/heic"),
            "HEIC photo",
        )

    def test_a_pdf_names_what_was_sent(self):
        self.assert_refused(
            SimpleUploadedFile("scan.pdf", b"%PDF-1.7 rest", content_type="application/pdf"),
            "This one is a application/pdf.",
        )

    def test_the_upload_takes_the_extension_of_what_it_really_is(self):
        upload = SimpleUploadedFile("face.pdf", image_bytes("JPEG"), content_type="application/pdf")
        _check(upload)
        self.assertEqual(upload.name, "face.jpg")
        self.assertEqual(upload.read(2), b"\xff\xd8", "the upload was not rewound")


class StoredPhotoTests(StudentsFixture):
    def test_a_png_sent_as_a_jpeg_is_stored_as_a_png(self):
        parent = self.guardian(name="Mrs. Adaeze Bello")
        upload = SimpleUploadedFile("adaeze.jpg", image_bytes("PNG"), content_type="image/jpeg")
        url = reverse("guardian-photo", kwargs={"pk": parent.pk})

        response = self.client_for(self.admin).post(
            f"{url}?tenant={self.tenant.slug}", {"photo": upload}, format="multipart",
        )

        self.assertEqual(response.status_code, 200, response.data)
        parent.refresh_from_db()
        self.assertTrue(parent.photo.name.endswith(".png"), parent.photo.name)
