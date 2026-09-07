"""What DatabaseStorage must survive: a stream somebody has already read.

Uploads are stored in the database rather than on disk, so every write goes
through :class:`core.storage.DatabaseStorage`. Django's filesystem storages
read an upload with ``chunks()``, which rewinds first; a bare ``read()`` does
not, and a caller that has already inspected the file leaves the stream at EOF.

That is not a hypothetical shape. The import engine parses a spreadsheet to
build its preview and count its rows BEFORE saving it, so the file arrives at
storage fully consumed. The row was then written with zero bytes while the
model beside it recorded the true size from the upload handler - so the file
looked present in every list and on every detail screen, and was empty only to
somebody who downloaded it.
"""
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.storage import DatabaseStorage


class DatabaseStorageRewindTests(TestCase):
    def setUp(self):
        self.storage = DatabaseStorage()

    def test_a_file_already_read_to_the_end_still_stores_its_bytes(self):
        upload = SimpleUploadedFile(
            "already-read.csv", b"Name,Email\nAda,ada@example.com\n",
        )
        # Whoever validated it got there first.
        self.assertNotEqual(upload.read(), b"")

        name = self.storage.save("probe/already-read.csv", upload)

        self.assertEqual(self.storage.size(name), 31)
        with self.storage.open(name, "rb") as handle:
            self.assertEqual(handle.read(), b"Name,Email\nAda,ada@example.com\n")

    def test_an_untouched_file_is_unaffected(self):
        upload = SimpleUploadedFile("fresh.csv", b"a,b\n1,2\n")

        name = self.storage.save("probe/fresh.csv", upload)

        with self.storage.open(name, "rb") as handle:
            self.assertEqual(handle.read(), b"a,b\n1,2\n")

    def test_a_plain_file_object_is_wrapped_and_stored(self):
        """Storage.save() wraps anything without chunks() in a File before it
        reaches _save, so a caller may hand over a bare file object."""
        import io

        name = self.storage.save("probe/bare.csv", io.BytesIO(b"x,y\n"))

        with self.storage.open(name, "rb") as handle:
            self.assertEqual(handle.read(), b"x,y\n")

    def test_the_recorded_size_matches_what_was_written(self):
        """The size column is what lists and detail screens show. It agreeing
        with the bytes is the invariant that was broken."""
        upload = SimpleUploadedFile("sized.csv", b"1234567890")
        upload.read()

        name = self.storage.save("probe/sized.csv", upload)

        from core.models import StoredFile

        row = StoredFile.objects.get(name=name)
        self.assertEqual(row.size, 10)
        self.assertEqual(len(bytes(row.content)), 10)

    def test_default_storage_is_the_database_one(self):
        """If this ever stops being true the tests above prove nothing about
        what production does."""
        self.assertIsInstance(default_storage, DatabaseStorage)

    def tearDown(self):
        from core.models import StoredFile

        StoredFile.objects.filter(name__startswith="probe/").delete()
