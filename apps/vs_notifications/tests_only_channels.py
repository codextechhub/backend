"""One send may be held to some of the channels its event uses, and never more.

A parent the receipt email already reaches is told of their payment in the app
without a second email: the payer notice names the in-app and push channels.
Naming a channel the settings have switched off does not turn it on.
"""
from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from vs_notifications.constants import ChannelChoices
from vs_notifications.models import Notification
from vs_notifications.notify import send_notification
from vs_rbac.tests.helpers import make_branch, make_school, make_school_admin

CONTEXT = {
    "customer_name": "Grace Nwankwo", "receipt_number": "RC-1", "amount_paid": "2,500.00",
    "payment_date": "21 Oct 2026", "invoice_number": "IV-1", "issuer_name": "Lagoon View",
    "note": "",
}


class OnlyChannelsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_notification_event_types", verbosity=0)
        call_command("seed_notification_templates", verbosity=0)
        school = make_school(slug="only-channels", name="Lagoon View")
        cls.user = make_school_admin(make_branch(school, name="Main"), email="parent@only-channels.test")

    def channels(self, **kwargs):
        with self.captureOnCommitCallbacks(execute=False):
            send_notification(
                event_key="billing.payment_received", context=CONTEXT,
                recipients=[self.user], tenant=self.user.tenant, **kwargs,
            )
        return set(Notification.all_objects.filter(recipient=self.user).values_list("channel", flat=True))

    def test_a_send_held_to_the_app_creates_no_email(self):
        channels = self.channels(only_channels=[ChannelChoices.IN_APP, ChannelChoices.PUSH])
        self.assertIn(ChannelChoices.IN_APP, channels)
        self.assertNotIn(ChannelChoices.EMAIL, channels)

    def test_a_send_with_no_limit_uses_every_channel_it_used_before(self):
        self.assertIn(ChannelChoices.EMAIL, self.channels())
