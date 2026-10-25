"""The finance emails greet whoever they reach, in the seed and in stored copies alike.

Migration 0023 rewrites the stored copy of the invoice, statement, receipt and
note emails where nobody edited it, and composes their HTML again. The copy it
writes is the seed's, so a fresh install and an upgraded one read the same.
Migration 0027 then swaps the invoice email's "Pay online" line for the payment
instruction line, and 0028 the receipt email's "Applied to invoice" line for the
settlement line: the only places the seed and 0023 differ.
"""
from __future__ import annotations

from importlib import import_module

from django.test import SimpleTestCase

from vs_notifications.constants import ChannelChoices
from vs_notifications.services.seed import _build_default_templates

migration = import_module("vs_notifications.migrations.0023_receipt_and_note_emails_greet_their_reader")
payment_lines = import_module("vs_notifications.migrations.0027_invoice_email_payment_instructions")
settlement_line = import_module("vs_notifications.migrations.0028_receipt_email_settlement_line")


class ReaderGreetingCopyTests(SimpleTestCase):
    def test_the_migration_writes_the_seed_copy(self):
        defaults = _build_default_templates()
        for key, definition in migration.TEMPLATES.items():
            with self.subTest(event_key=key):
                body = definition["body"].replace(payment_lines.OLD_LINE, payment_lines.NEW_LINE)
                body = body.replace(settlement_line.OLD_LINE, settlement_line.NEW_LINE)
                self.assertEqual(body, defaults[(key, ChannelChoices.EMAIL)]["body"])

    def test_the_invoice_email_carries_the_payment_instruction_line(self):
        body = _build_default_templates()[("billing.invoice_issued", ChannelChoices.EMAIL)]["body"]
        self.assertIn(payment_lines.NEW_LINE, body)
        self.assertNotIn(payment_lines.OLD_LINE, body)

    def test_every_reworded_email_greets_its_reader(self):
        for key in migration.REWORDED:
            with self.subTest(event_key=key):
                self.assertIn("recipient_name", migration.TEMPLATES[key]["body"])
                self.assertNotIn("recipient_name", migration.TEMPLATES[key]["previous_body"])

    def test_a_note_shipped_with_an_em_dash_reads_as_shipped(self):
        previous = migration.TEMPLATES["billing.debit_note_issued"]["previous_body"]
        self.assertEqual(migration._as_shipped(previous.replace(" - ", " — ", 1)), previous)
