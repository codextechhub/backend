"""The portal seeder on a server where the fake payment provider is off.

Meridian Crest Academy is filled on staging, which offers Paystack only. The
full pupil's card payment being confirmed and their transfer account both
need the fake provider, so each is skipped and said so, and neither asks the
payment engine for a provider it would refuse.
"""
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from schools.vs_student_portal.management.commands.seed_student_portal_scenarios import Command


def cast():
    return {"tenant": SimpleNamespace(slug="meridian-crest-academy"), "invoice": object(), "full": object()}


@override_settings(PAYMENTS_FAKE_PROVIDER_ENABLED=False)
class WithoutFakeProviderTests(SimpleTestCase):
    def setUp(self):
        self.out = StringIO()
        self.command = Command(stdout=self.out)

    def test_no_card_payment_is_started(self):
        with mock.patch("vs_payments.services.initiate_collection") as start:
            self.command._pending_card(cast())
        start.assert_not_called()
        self.assertIn("no card payment being confirmed", self.out.getvalue())

    def test_no_transfer_account_is_issued(self):
        with mock.patch("vs_payments.services.create_virtual_account") as issue:
            self.command._transfer_account(cast())
        issue.assert_not_called()
        self.assertIn("no transfer account", self.out.getvalue())
