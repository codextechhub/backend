"""Where a parent's push opens, and which child it names.

Mrs. Nwosu turned notifications on in the parent app. A notice that reaches her
children opens that notice in the parent app. Chiamaka's released result opens
Results with Chiamaka showing, and the confirmation of the payment she made for
Chiamaka's tuition opens Fees with Chiamaka showing. A child who is not hers is
never named: the push then falls back to the address the recipient's own app
answers. What a push says can show on a locked screen, so none carries a figure.
"""
from __future__ import annotations

import json
from unittest import mock

from django.core.management import call_command
from django.test import override_settings

from vs_notifications.constants import ChannelChoices
from vs_notifications.models import Notification, PushSubscription
from vs_notifications.notify import send_notification
from vs_notifications.services import push
from vs_payments import services as payment_services
from vs_payments.models import CollectionIntent

from ..push import destination
from .fees_base import ParentBooksFixture

KEYS = {
    "WEB_PUSH_VAPID_PUBLIC_KEY": "BPublicKeyForTests",
    "WEB_PUSH_VAPID_PRIVATE_KEY": "privateKeyForTests",
    "WEB_PUSH_CONTACT": "mailto:dev@example.com",
}
WEBPUSH = "pywebpush.webpush"
RESULT = {
    "student_first_name": "Chiamaka", "term_name": "First Term", "session_name": "2026/2027",
    "tenant_name": "Brightfield Schools", "branch_name": "", "tenant_phone": "",
}


@override_settings(**KEYS)
class ParentPushTests(ParentBooksFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_notification_event_types", verbosity=0)
        call_command("seed_notification_templates", verbosity=0)
        PushSubscription.all_objects.create(
            tenant=cls.nwosu_user.tenant, user=cls.nwosu_user,
            endpoint="https://push.example.com/nwosu",
            endpoint_hash=push.endpoint_hash("https://push.example.com/nwosu"),
            p256dh="p256dh-key", auth="auth-key",
        )

    def sent(self, **kwargs):
        """Send one notification to Mrs. Nwosu and return the push payload."""
        with mock.patch(WEBPUSH) as webpush, self.captureOnCommitCallbacks(execute=True):
            send_notification(recipients=[self.nwosu_user], tenant=self.tenant, **kwargs)
        self.assertEqual(webpush.call_count, 1)
        return json.loads(webpush.call_args.kwargs["data"])

    def test_a_notice_opens_in_the_parent_app(self):
        payload = self.sent(
            event_key="notices.published_parent",
            context={"notice_title": "Sports day", "child_names": "Chiamaka and Tobi",
                     "tenant_name": "Brightfield Schools", "notice_url": ""},
            metadata={"notice_id": 42},
        )
        self.assertEqual(payload["url"], "/parent/notices/42")
        self.assertNotIn("child", payload)
        self.assertEqual(payload["tag"], "notices.published_parent")

    def test_a_released_result_opens_results_with_the_child(self):
        payload = self.sent(
            event_key="gradebook.report_card_ready", context=RESULT,
            metadata={"student_id": self.chiamaka.pk},
        )
        self.assertEqual(payload["url"], f"/parent/results?child={self.chiamaka.pk}")
        self.assertEqual(payload["child"], self.chiamaka.pk)
        self.assertEqual(payload["title"], "Chiamaka's First Term result is ready")

    def test_a_child_of_another_family_is_never_named(self):
        payload = self.sent(
            event_key="gradebook.report_card_ready", context=RESULT,
            metadata={"student_id": self.zainab.pk},
        )
        self.assertEqual(payload["url"], "/")
        self.assertNotIn("child", payload)

    def test_a_confirmed_payment_opens_fees_with_the_child(self):
        reference = self.pay_invoice(self.chiamaka_tuition, 200_000).data["data"]["reference"]
        intent = CollectionIntent.objects.get(reference=reference)
        self.fake.forced_status[reference] = "SUCCEEDED"
        with mock.patch(WEBPUSH) as webpush, self.captureOnCommitCallbacks(execute=True):
            payment_services.confirm_collection(intent)

        payload = json.loads(webpush.call_args.kwargs["data"])
        self.assertEqual(payload["tag"], "billing.payment_received")
        self.assertEqual(payload["url"], f"/parent/fees?child={self.chiamaka.pk}")
        self.assertEqual(payload["child"], self.chiamaka.pk)
        self.assertNotIn("2,000", json.dumps(payload))

    def test_a_payment_to_somebody_who_is_not_the_childs_parent_says_nothing(self):
        reference = self.pay_invoice(self.chiamaka_tuition, 200_000).data["data"]["reference"]
        intent = CollectionIntent.objects.get(reference=reference)
        self.fake.forced_status[reference] = "SUCCEEDED"
        with self.captureOnCommitCallbacks(execute=True):
            payment_services.confirm_collection(intent)
        notification = Notification.all_objects.filter(
            event_type__key="billing.payment_received", channel=ChannelChoices.IN_APP,
        ).get()
        notification.recipient = self.yusuf_user
        self.assertIsNone(destination(notification))
