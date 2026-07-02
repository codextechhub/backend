"""
Tests for the database-backed media storage (B9) and its serving view.
"""
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import StoredFile
from core.storage import DatabaseStorage


class DatabaseStorageTests(TestCase):
    def setUp(self):
        self.storage = DatabaseStorage()

    def test_default_storage_is_database_backed(self):
        self.assertIsInstance(default_storage, DatabaseStorage)

    def test_save_open_roundtrip(self):
        name = self.storage.save("school_logos/logo.png", ContentFile(b"\x89PNG fake"))
        self.assertTrue(self.storage.exists(name))
        with self.storage.open(name) as fh:
            self.assertEqual(fh.read(), b"\x89PNG fake")
        row = StoredFile.objects.get(name=name)
        self.assertEqual(row.content_type, "image/png")
        self.assertEqual(row.size, 9)
        self.assertEqual(self.storage.url(name), f"/media/{name}")

    def test_csv_accepted_exe_rejected(self):
        self.storage.save("imports/students.csv", ContentFile(b"a,b,c"))
        with self.assertRaises(ValidationError):
            self.storage.save("evil/payload.exe", ContentFile(b"MZ"))
        with self.assertRaises(ValidationError):
            self.storage.save("evil/page.svg", ContentFile(b"<svg/>"))

    def test_size_ceiling_enforced(self):
        with self.settings(MEDIA_DB_MAX_BYTES=10):
            with self.assertRaises(ValidationError):
                self.storage.save("imports/big.csv", ContentFile(b"x" * 11))

    def test_delete(self):
        name = self.storage.save("imports/tmp.csv", ContentFile(b"1"))
        self.storage.delete(name)
        self.assertFalse(self.storage.exists(name))

    def test_path_traversal_blocked(self):
        from django.core.exceptions import SuspiciousOperation

        with self.assertRaises(SuspiciousOperation):
            self.storage.save("../../etc/passwd.csv", ContentFile(b"x"))

    def test_stored_name_is_unguessable(self):
        # The stored name keeps a readable prefix but carries a high-entropy token,
        # so a caller can't fetch a file by guessing a predictable path.
        name = self.storage.save("expense-receipts/receipt.pdf", ContentFile(b"%PDF fake"))
        self.assertNotEqual(name, "expense-receipts/receipt.pdf")
        self.assertTrue(name.startswith("expense-receipts/receipt-"))
        self.assertTrue(name.endswith(".pdf"))
        # Two uploads of the same filename get distinct, unguessable names.
        other = self.storage.save("expense-receipts/receipt.pdf", ContentFile(b"%PDF two"))
        self.assertNotEqual(name, other)


class MediaViewTests(TestCase):
    def setUp(self):
        self.storage = DatabaseStorage()
        self.name = self.storage.save("school_logos/test.png", ContentFile(b"\x89PNG bytes"))

    def _user(self):
        from vs_user.models import User

        return User.objects.create_user(
            email="media@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="Media", last_name="Tester",
        )

    def test_anonymous_denied(self):
        resp = APIClient().get(f"/media/{self.name}")
        self.assertEqual(resp.status_code, 401)

    def test_authenticated_user_gets_file(self):
        client = APIClient()
        client.force_authenticate(user=self._user())
        resp = client.get(f"/media/{self.name}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(resp.content, b"\x89PNG bytes")

    def test_missing_file_404(self):
        client = APIClient()
        client.force_authenticate(user=self._user())
        resp = client.get("/media/none/missing.png")
        self.assertEqual(resp.status_code, 404)
