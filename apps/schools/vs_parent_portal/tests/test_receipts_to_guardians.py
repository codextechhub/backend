"""A receipt reaches the primary guardian, and nobody is emailed the same receipt twice.

Mrs. Yusuf is Zainab's primary guardian and pays Zainab's tuition in the parent
app. The receipt email reaches her once, with its PDF, and she is told in the
app without a second email. Mrs. Nwosu is Chiamaka's mother but not her primary
guardian; when she pays, Chiamaka's father, the primary guardian, is emailed
the receipt, and Mrs. Nwosu is told as the payer, by email too, because the
receipt email does not reach her.
"""
from __future__ import annotations

from unittest import mock

from django.core.files.storage import default_storage
from django.core.management import call_command

from vs_notifications.constants import ChannelChoices
from vs_notifications.models import Notification
from vs_payments import services as payment_services
from vs_payments.models import CollectionIntent

from .base import HUSBAND_EMAIL
from .fees_base import ParentBooksFixture


class ReceiptsReachGuardiansTests(ParentBooksFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_notification_event_types", verbosity=0)
        call_command("seed_notification_templates", verbosity=0)

    def setUp(self):
        super().setUp()
        mock.patch.object(default_storage, "save", side_effect=lambda name, *a, **k: name).start()
        self.addCleanup(mock.patch.stopall)

    def paid(self, invoice, user):
        """Pay ``invoice`` as ``user`` and return the receipt notifications that followed."""
        reference = self.pay_invoice(invoice, 100_000, user=user).data["data"]["reference"]
        intent = CollectionIntent.objects.get(reference=reference)
        self.fake.forced_status[reference] = "SUCCEEDED"
        before = set(Notification.all_objects.values_list("pk", flat=True))
        with self.captureOnCommitCallbacks(execute=True):
            payment_services.confirm_collection(intent)
        return Notification.all_objects.filter(event_type__key="billing.payment_received").exclude(pk__in=before)

    def emailed(self, rows):
        return sorted(
            (row.unregistered_email or getattr(row.recipient, "email", "")).lower()
            for row in rows.filter(channel=ChannelChoices.EMAIL)
        )

    def test_a_primary_guardian_who_pays_is_emailed_the_receipt_once(self):
        rows = self.paid(self.zainab_tuition, self.yusuf_user)

        self.assertEqual(self.emailed(rows), ["amina.yusuf@example.ng"])
        self.assertTrue(rows.filter(channel=ChannelChoices.IN_APP, recipient=self.yusuf_user).exists())

    def test_a_second_guardian_who_pays_is_told_as_payer_and_the_primary_is_sent_the_receipt(self):
        rows = self.paid(self.chiamaka_tuition, self.nwosu_user)

        self.assertEqual(self.emailed(rows), sorted([HUSBAND_EMAIL.lower(), "adaeze.nwosu@example.ng"]))
