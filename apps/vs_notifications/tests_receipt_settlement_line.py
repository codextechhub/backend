"""The receipt email's settlement line follows the receipt, in the seed and in stored copies.

The seeded email used to print "Applied to invoice: {{ invoice_number }}", which
reads as a dangling label when the payment was held as credit. Migration 0028
swaps that one line for ``{{ settlement_summary }}`` in a stored copy nobody has
written their own HTML for, and leaves the rest of the body as it found it.
"""
from __future__ import annotations

from importlib import import_module

from django.apps import apps as live_apps
from django.test import SimpleTestCase, TestCase

from vs_notifications.constants import ChannelChoices
from vs_notifications.models import NotificationEventType, NotificationTemplate
from vs_notifications.services.seed import _build_default_templates

settlement = import_module("vs_notifications.migrations.0028_receipt_email_settlement_line")


class ReceiptEmailSeedTests(SimpleTestCase):

    def test_the_seed_carries_the_settlement_line(self):
        body = _build_default_templates()[("billing.payment_received", ChannelChoices.EMAIL)]["body"]
        self.assertIn(settlement.NEW_LINE, body)
        self.assertNotIn(settlement.OLD_LINE, body)


class ReceiptEmailMigrationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        event = NotificationEventType.objects.get_or_create(
            key="billing.payment_received", defaults={"name": "Payment received"},
        )[0]
        cls.template, _ = NotificationTemplate.objects.update_or_create(
            event_type=event, channel=ChannelChoices.EMAIL,
            defaults={"subject": "Receipt {{ receipt_number }}", "html_is_custom": False,
                      "body": f"Hello.\n\n{settlement.OLD_LINE}\n\nBye"},
        )

    def test_only_the_one_line_changes(self):
        settlement.adopt_settlement_line(live_apps, None)
        self.template.refresh_from_db()
        self.assertEqual(self.template.body, f"Hello.\n\n{settlement.NEW_LINE}\n\nBye")
        self.assertIn("settlement_summary", self.template.html_body)

    def test_a_body_that_never_had_the_line_is_left_alone(self):
        NotificationTemplate.objects.filter(pk=self.template.pk).update(body="Our own words")
        settlement.adopt_settlement_line(live_apps, None)
        self.template.refresh_from_db()
        self.assertEqual(self.template.body, "Our own words")

    def test_the_change_reverses(self):
        settlement.adopt_settlement_line(live_apps, None)
        settlement.restore_invoice_line(live_apps, None)
        self.template.refresh_from_db()
        self.assertEqual(self.template.body, f"Hello.\n\n{settlement.OLD_LINE}\n\nBye")
