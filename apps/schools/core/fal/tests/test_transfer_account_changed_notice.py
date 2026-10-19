"""A pupil and their primary guardian are told when the pupil's number changes.

Tunde, at Corona's Ikeja branch, pays fees into his own account number. The
provider gives that number to somebody else, the daily check replaces it, and
Tunde hears in his app (and by a push that names no number) while his mother,
Ngozi, is emailed. Neither message carries the new number: the email tells
Ngozi where to see it and that the school never sends one by email.
"""
from __future__ import annotations

import re
from unittest import mock

from django.test import SimpleTestCase

from schools.core.fal.tests.test_transfer_account_on_first_bill import _FirstBillFixture, give_guardian
from schools.core.fal.transfer_accounts import (
    EVENT_GUARDIAN_ACCOUNT_CHANGED,
    EVENT_PUPIL_ACCOUNT_CHANGED,
)
from schools.vs_student_portal.tests.test_fees import give_access
from vs_finance.models import Customer
from vs_notifications.constants import EVENT_TYPE_REGISTRY, ChannelChoices
from vs_notifications.services.render import render_template
from vs_notifications.services.seed import _build_default_templates
from vs_payments import services
from vs_payments.exceptions import ProviderError
from vs_payments.virtual_account_standing import check_virtual_accounts

_SEND = "vs_notifications.notify.send_notification"
_VARIABLE = re.compile(r"\{\{\s*([a-z_0-9]+)|\{%\s*if\s+([a-z_0-9]+)")

#: The only values either notice may be built from: never the new number.
ALLOWED_VARIABLES = {
    "student_first_name", "old_account_number", "has_new_account", "tenant_name",
    "branch_name", "email_brand",
}
CHANNELS = {
    EVENT_PUPIL_ACCOUNT_CHANGED: {ChannelChoices.IN_APP, ChannelChoices.PUSH},
    EVENT_GUARDIAN_ACCOUNT_CHANGED: {ChannelChoices.IN_APP, ChannelChoices.EMAIL},
}


class AccountNumberChangedTests(_FirstBillFixture):
    def setUp(self):
        super().setUp()
        self.fake.reports_virtual_accounts = True
        self.tunde_user = give_access(self.tunde, "COR/101")
        self.ngozi = give_guardian(self.tunde, "ngozi@example.ng")
        self.old = services.create_virtual_account(
            entity=self.tunde_customer.entity, customer=self.tunde_customer, provider="FAKE")

    def withdraw(self, reference=None):
        self.fake.virtual_accounts[reference or self.old.provider_reference] = "FAKE-CUS-a-stranger"
        with mock.patch(_SEND, return_value=["sent"]) as send:
            with self.captureOnCommitCallbacks(execute=True):
                check_virtual_accounts()
        self.old.refresh_from_db()
        return {call.kwargs["event_key"]: call.kwargs for call in send.call_args_list}

    def test_the_pupil_and_the_primary_guardian_are_each_told(self):
        sent = self.withdraw()

        pupil = sent[EVENT_PUPIL_ACCOUNT_CHANGED]
        self.assertEqual(pupil["recipients"], [self.tunde_user])
        self.assertEqual(pupil["branch"], self.ikeja)
        guardian = sent[EVENT_GUARDIAN_ACCOUNT_CHANGED]
        self.assertEqual(guardian["recipients"], [])
        self.assertEqual([r.email for r in guardian["unregistered_recipients"]], ["ngozi@example.ng"])

    def test_the_old_number_is_named_and_the_new_one_never_is(self):
        context = self.withdraw()[EVENT_GUARDIAN_ACCOUNT_CHANGED]["context"]
        new = self.old.replaced_by

        self.assertEqual(context["old_account_number"], self.old.account_number)
        self.assertTrue(context["has_new_account"])
        self.assertEqual(context["student_first_name"], "Tunde")
        self.assertNotIn(new.account_number, repr(context))

    def test_a_refused_replacement_says_there_is_no_new_number_yet(self):
        self.issued.side_effect = ProviderError("down")

        context = self.withdraw()[EVENT_PUPIL_ACCOUNT_CHANGED]["context"]

        self.assertFalse(context["has_new_account"])

    def test_a_guardian_with_no_email_and_no_account_is_skipped_and_the_pupil_still_told(self):
        type(self.ngozi).all_objects.filter(pk=self.ngozi.pk).update(email="")

        sent = self.withdraw()

        self.assertIn(EVENT_PUPIL_ACCOUNT_CHANGED, sent)
        self.assertNotIn(EVENT_GUARDIAN_ACCOUNT_CHANGED, sent)

    def test_a_pupil_notice_that_fails_still_lets_the_guardian_hear(self):
        self.fake.virtual_accounts[self.old.provider_reference] = "FAKE-CUS-a-stranger"

        def refuse_the_pupil(**kwargs):
            if kwargs["event_key"] == EVENT_PUPIL_ACCOUNT_CHANGED:
                raise RuntimeError("push service down")
            return ["sent"]

        with mock.patch(_SEND, side_effect=refuse_the_pupil) as send:
            with self.captureOnCommitCallbacks(execute=True):
                check_virtual_accounts()

        self.assertIn(EVENT_GUARDIAN_ACCOUNT_CHANGED, [c.kwargs["event_key"] for c in send.call_args_list])

    def test_a_customer_who_is_not_a_pupil_is_told_nothing(self):
        family = Customer.objects.create(
            entity=self.tunde_customer.entity, code="FAM1", name="The Okoros",
            billing_email="okoro@example.ng", receivable_account=self.tunde_customer.receivable_account,
        )
        other = services.create_virtual_account(entity=family.entity, customer=family, provider="FAKE")
        sent = self.withdraw(reference=other.provider_reference)

        other.refresh_from_db()
        self.assertIsNotNone(other.replaced_by)
        self.assertEqual(sent, {})

    def test_nothing_is_sent_while_the_number_is_still_theirs(self):
        with mock.patch(_SEND) as send:
            with self.captureOnCommitCallbacks(execute=True):
                check_virtual_accounts()

        send.assert_not_called()


class AccountNumberChangedCopyTests(SimpleTestCase):
    """What the two notices may say, and how they read."""

    def test_both_events_are_registered_always_sent_with_their_channels(self):
        entries = {e["key"]: e for e in EVENT_TYPE_REGISTRY if e["key"] in CHANNELS}
        templates = _build_default_templates()
        for key, channels in CHANNELS.items():
            with self.subTest(event=key):
                self.assertEqual(set(entries[key]["supported_channels"]), channels)
                self.assertTrue(entries[key]["is_transactional"])
                self.assertEqual({c for (k, c) in templates if k == key}, channels)

    def test_no_notice_can_carry_the_new_number(self):
        templates = _build_default_templates()
        for key, channels in CHANNELS.items():
            for channel in channels:
                with self.subTest(event=key, channel=channel):
                    text = templates[(key, channel)]["subject"] + templates[(key, channel)]["body"]
                    used = {a or b for a, b in _VARIABLE.findall(text)}
                    self.assertLessEqual(used, ALLOWED_VARIABLES)

    def test_the_push_names_no_number_at_all(self):
        entry = _build_default_templates()[(EVENT_PUPIL_ACCOUNT_CHANGED, ChannelChoices.PUSH)]
        self.assertNotIn("{{", entry["subject"] + entry["body"])

    def context(self, **changes):
        return {
            "student_first_name": "Amaka", "old_account_number": "1238472659",
            "has_new_account": True, "tenant_name": "Meridian Crest Academy", "branch_name": "",
            "email_brand": "XVS", **changes,
        }

    def test_the_guardian_email_says_where_to_look_and_that_no_number_comes_by_email(self):
        entry = _build_default_templates()[(EVENT_GUARDIAN_ACCOUNT_CHANGED, ChannelChoices.EMAIL)]
        subject = render_template(entry["subject"], self.context())
        body = render_template(entry["body"], self.context())

        self.assertEqual(subject, "Amaka's account number for school fees has changed")
        self.assertIn("Please do not pay into the old number (1238472659) any more.", body)
        self.assertIn("in Amaka's student app, under Fees, or in your own parent app.", body)
        self.assertIn("Meridian Crest Academy will never send you a new account number by email.", body)
        self.assertIn("Meridian Crest Academy via XVS", body)

    def test_the_guardian_email_with_no_new_number_says_how_to_get_one(self):
        entry = _build_default_templates()[(EVENT_GUARDIAN_ACCOUNT_CHANGED, ChannelChoices.EMAIL)]
        body = render_template(entry["body"], self.context(has_new_account=False))

        self.assertIn("Amaka can get a new number in the student app, under Fees", body)
        self.assertNotIn("You can see the new number", body)

    def test_the_pupils_in_app_notice_reads_both_ways(self):
        entry = _build_default_templates()[(EVENT_PUPIL_ACCOUNT_CHANGED, ChannelChoices.IN_APP)]
        self.assertIn("Your new number is under Fees.", render_template(entry["body"], self.context()))
        self.assertIn("Get a new number under Fees.",
                      render_template(entry["body"], self.context(has_new_account=False)))
