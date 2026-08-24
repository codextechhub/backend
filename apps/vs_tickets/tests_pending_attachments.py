"""A school still onboarding can attach evidence to its own ticket.

Filing a ticket was already open to a pending tenant; attaching to it was not,
so the only way to send a screenshot was to reply to the confirmation email.
That moves the evidence off the platform and out of the ticket it belongs to.

These pin both halves: the surface is open, and it is open no wider than the
caller's own ticket.
"""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.uploads import MAX_TICKET_ATTACHMENT_BYTES, TICKET_EXTENSIONS

# A one-pixel PNG. The validator reads the header and refuses bytes that do not
# match the extension, so a file of zeroes named .png would not do.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


class PendingSchoolAttachmentTests(TestCase):
    def test_the_attachments_action_is_on_the_pending_surface(self):
        """The one-line change this file exists for."""
        from vs_tickets.views import TicketViewSet

        self.assertIn("attachments", TicketViewSet.pending_tenant_surface)
        self.assertIn("create", TicketViewSet.pending_tenant_surface)

    def test_the_rest_of_the_desk_stays_shut(self):
        """Opening one action must not open the desk.

        A pending school files and attaches. Lists, threads and assignment are
        still go-live work, and this is what says so.
        """
        from vs_tickets.views import TicketViewSet

        for action in ("list", "retrieve", "assign", "transition", "audit"):
            self.assertNotIn(
                action,
                TicketViewSet.pending_tenant_surface,
                f"{action} must not be reachable before go-live",
            )


class AttachmentLimitTests(TestCase):
    """The limits that make opening the surface safe.

    They already existed and are stronger than a browser ``accept=`` attribute,
    which is a hint the client can ignore. These assert they are still in force
    now that a wider set of callers can reach them.
    """

    def _validate(self, upload):
        from core.uploads import validate_upload

        return validate_upload(
            upload,
            allowed=TICKET_EXTENSIONS,
            max_bytes=MAX_TICKET_ATTACHMENT_BYTES,
        )

    def test_a_screenshot_is_accepted(self):
        upload = SimpleUploadedFile("screen.png", PNG, content_type="image/png")
        name, _ = self._validate(upload)
        self.assertEqual(name, "screen.png")

    def test_an_executable_is_refused_however_it_is_named(self):
        from rest_framework.exceptions import ValidationError

        upload = SimpleUploadedFile("payload.exe", b"MZ\x90\x00", content_type="application/octet-stream")
        with self.assertRaises(ValidationError):
            self._validate(upload)

    def test_content_must_match_the_extension(self):
        """The check a browser's accept attribute cannot make.

        Renaming payload.exe to payload.png gets it past the file picker and
        past any extension-only check. The header is read instead.
        """
        from rest_framework.exceptions import ValidationError

        upload = SimpleUploadedFile("payload.png", b"MZ\x90\x00", content_type="image/png")
        with self.assertRaises(ValidationError):
            self._validate(upload)

    def test_a_file_over_ten_megabytes_is_refused(self):
        from rest_framework.exceptions import ValidationError

        oversized = PNG + b"\x00" * (MAX_TICKET_ATTACHMENT_BYTES + 1)
        upload = SimpleUploadedFile("huge.png", oversized, content_type="image/png")
        with self.assertRaises(ValidationError):
            self._validate(upload)

    def test_an_empty_file_is_refused(self):
        from rest_framework.exceptions import ValidationError

        upload = SimpleUploadedFile("empty.png", b"", content_type="image/png")
        with self.assertRaises(ValidationError):
            self._validate(upload)

    def test_the_allowlist_covers_what_a_bug_report_carries(self):
        for extension in ("png", "jpg", "jpeg", "webp", "gif", "pdf", "csv", "xls", "xlsx"):
            self.assertIn(extension, TICKET_EXTENSIONS)
